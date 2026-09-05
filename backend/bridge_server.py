"""本机 CNKI 插件桥接服务。

服务本身不访问 CNKI，不保存 Cookie；只负责把 Tool 请求排队，并等待 Chrome
扩展返回用户已授权 CNKI 标签页中的页面数据。

支持两种运行模式（--mode）：

- http（默认，向后兼容）：完整 HTTP 服务监听 127.0.0.1:<port>。
  同时服务 Agent 侧调试用的 `/v1/call`，以及扩展侧轮询用的
  `/v1/extension/next-command` / `/v1/extension/command-result`。
  不依赖 mcp 包，可用系统自带 Python 直接跑，适合 curl 手工调试。

- mcp：作为标准 MCP stdio server 运行，供支持 MCP 协议的 Agent host（如
  WorkBuddy）通过 stdin/stdout 发现和调用 21 个具名 Tool，参数用
  JSON Schema（枚举、范围、长度）在协议层做校验，不用再手搓 curl。
  扩展侧仍然只认 HTTP 长轮询——Chrome MV3 Service Worker 没有 listen()
  能力，这一段传输方式不受协议选型影响，因此本模式会在后台线程里原样
  启一份 HTTP 服务专门伺候扩展（以及保留 `/v1/call` 供调试），主线程跑
  MCP 的 stdio 事件循环。

两种模式共享同一个 CommandBroker：不管 Agent 侧走 HTTP 还是 MCP，最终都是
把动作放进同一个队列，等待同一个已登录 Chrome 扩展轮询取走并执行。
"""

import argparse
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

ALLOWED_ACTIONS = {
    "page.snapshot",
    "page.dom",
    "page.navigate",
    "session.status",
    "session.open_search",
    "search.submit",
    "search.sort",
    "search.results",
    "search.set_field",
    "search.set_library",
    "search.turn_page",
    "search.get_filters",
    "search.apply_filter",
    "search.advanced_submit",
    "article.download_options",
    "article.click_pdf_download",
    "batch.start_pdf_download",
    "batch.get_status",
    "batch.resume_pdf_download",
    "download.recent",
    "flow.run",
}
MAX_CALL_TIMEOUT_SECONDS = 45
# flow.run 的每步上限与单动作一致，但总步数受限，避免一次调用失控。
MAX_FLOW_STEPS = 20
# 扩展每次长轮询都会刷新这个时间戳；/health 用它判断「扩展是否真的连上了本服务」。
EXTENSION_ALIVE_WINDOW_SECONDS = 90
DEFAULT_MCP_CALL_TIMEOUT_SECONDS = 40

# 验证码等待上限：填一次验证码通常几十秒，但用户可能离开，给 15 分钟足够宽松。
MAX_CAPTCHA_WAIT_SECONDS = 900
# 拦截期间的最小探活间隔。扩展受 BRIDGE_ALARM 限制每 30s 才轮询一次，这个下限
# 只是防止 alarm 被手工/启动路径密集触发时把探活打满。
CAPTCHA_PROBE_MIN_INTERVAL_SECONDS = 10
# 反向确认：连续多少次探活都没再看到验证页，才判定解除。设为 1 会在验证页跳转的
# 中间态（比如短暂 about:blank）误判；设 2 更稳，代价是多 30 秒。
CAPTCHA_CLEAR_CONFIRMATIONS = 2

# 判定「被 CNKI 安全验证拦截」的特征。分两级：
#   URL 级——命中即高置信，直接判拦截；
#   文本级——需要出现在标题/错误这类短字段里才算，避免正文里出现"验证"二字误伤。
CAPTCHA_URL_MARKERS = (
    "/verify/",
    "verify/home",
    "captchatype=",
    "captchaverify",
    "safeverify",
)
CAPTCHA_TEXT_MARKERS = (
    "请完成安全验证",
    "安全验证",
    "请依次点击",
    "请输入验证码",
    "拖动滑块",
    "请完成人机验证",
    "请完成验证",
    "滑动验证",
)
# 只有这些顶层键对应的字符串才做文本级匹配。正文（text/preview）不参与，
# 否则一篇讲验证码识别的论文摘要就会把整条链路判成被拦截。
CAPTCHA_TEXT_KEYS = ("title", "error", "message", "reason", "code", "state", "status")
_CAPTCHA_SCAN_DEPTH = 6
# 内置探活命令的 id 前缀。拦截期间服务端向扩展下发的 session.status 探针用此前缀，
# complete() 借此把探针结果和普通命令结果区分开，走不通的拦截判定逻辑。
PROBE_PREFIX = "probe-"

# Tool 元数据同时服务于 /health 自描述接口和 MCP Tool 注册。
# 这里只允许列出受限、可审计的业务动作，禁止扩展为任意 JS 或任意选择器点击。
TOOL_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "session.status": {
        "description": "查看 Chrome 当前活动标签及现有 CNKI 标签，不读取页面正文。",
        "payload": {},
    },
    "session.open_search": {
        "description": "创建或复用 CNKI 检索标签；提供 query 时会在页面输入框内完成一次正常检索。",
        "payload": {"query": "可选，1-100 字检索词"},
    },
    "page.snapshot": {
        "description": "读取当前活动 CNKI 页的标题、URL、加载状态和可见文本预览。",
        "payload": {},
    },
    "page.dom": {
        "description": "读取当前活动 CNKI 页的受长度限制 HTML，用于页面适配诊断。",
        "payload": {"maxChars": "可选，1000-300000"},
    },
    "page.navigate": {
        "description": "在当前或新建的 CNKI 标签中打开指定 cnki.net 页面。",
        "payload": {"url": "必填，https://*.cnki.net/*"},
    },
    "search.submit": {
        "description": "自动创建或复用 CNKI 检索页，在页面检索框中输入关键词并点击页面原生检索按钮。",
        "payload": {"query": "必填，1-100 字检索词"},
    },
    "search.sort": {
        "description": "点击 CNKI 检索页自身的排序控件并等待结果刷新。",
        "payload": {"sortBy": "必填：citations、downloads、relevance、publishedAt、comprehensive", "limit": "可选，1-50"},
    },
    "search.results": {
        "description": "从当前活动 CNKI 检索页读取已渲染的论文列表及页面信息。",
        "payload": {"limit": "可选，1-50，默认 20"},
    },
    "search.set_field": {
        "description": "切换一框式检索框的检索字段（主题/作者/篇名/全文等 16 项），切换后配合 search.submit 在该字段下检索。",
        "payload": {"field": "必填：SU/TKA/KY/TI/FT/AU/FI/RP/AF/FU/AB/CO/RF/CLC/LY/DOI"},
    },
    "search.set_library": {
        "description": "切换文献库（学术期刊/学位论文/博士/硕士/图书/会议等），用于把检索范围限定到学位论文等特定库。",
        "payload": {"library": "必填：journal/dissertation/doctor/master/book/conference/newspaper/almanac/patent/standard/achievement"},
    },
    "search.turn_page": {
        "description": "翻页到指定页码或上一页/下一页，并等待结果表刷新后读取。",
        "payload": {"page": "可选，>=1 的目标页码（仅在当前可见页码内）；与 direction 二选一", "direction": "可选：next/prev，与 page 二选一"},
    },
    "search.get_filters": {
        "description": "读取左侧筛选面板（来源类别/学科/研究层次/年度/文献类型/机构/基金等）的维度和可选值；可先展开指定折叠维度再读取。",
        "payload": {"groups": "可选，字符串或字符串数组：要展开后读取的维度 groupid，如 YE(年度)/WXLX(文献类型)/YJCC(研究层次)"},
    },
    "search.apply_filter": {
        "description": "勾选左侧筛选面板某维度的若干值并提交（例如年度=2023,2024），提交后等待结果表刷新。",
        "payload": {"group": "必填，筛选维度 groupid，如 YE(年度)/WXLX(文献类型)", "values": "必填，字符串或字符串数组：要勾选的筛选值"},
    },
    "search.advanced_submit": {
        "description": (
            "在 CNKI 高级检索页（/kns8s/AdvSearch）填写 1-3 行字段+检索词并提交，支持 AND/OR/NOT 组合多条件精确检索。"
            "字段集比一框式检索多了 TU(导师)/FTU(第一导师)/LY(学位授予单位)/XF(学科专业名称)，"
            "是定位“某导师指导的论文”等一框式检索做不到的多条件场景的关键能力。"
            "使用前须先用 page.navigate 打开形如 https://kns.cnki.net/kns8s/AdvSearch?type=expert&classid=<库代码>&language=CHS 的页面。"
        ),
        "payload": {
            "conditions": (
                "必填，1-3 个条件对象数组，每个对象含 field（SU/TKA/KY/TI/FT/AU/AF/TU/FTU/LY/FU/AB/CO/RF/CLC/XF/DOI）、"
                "value（检索词）；第 2、3 个条件可选 logic（AND/OR/NOT，默认 AND）"
            )
        },
    },
    "article.download_options": {
        "description": "读取当前论文详情页已显示的下载入口，不触发下载。",
        "payload": {},
    },
    "article.click_pdf_download": {
        "description": "点击当前论文详情页中识别到的正常 PDF 下载按钮，并等待 Chrome 创建下载任务。",
        "payload": {},
    },
    "batch.start_pdf_download": {
        "description": "逐篇进入论文详情页并点击页面已有 PDF 下载按钮；遇异常自动暂停。",
        "payload": {
            "articleUrls": "必填，1-10 个 CNKI 论文详情页 URL",
            "intervalSeconds": "可选，3-30 秒，默认 5 秒",
        },
    },
    "batch.get_status": {
        "description": "查看当前或最近一个 PDF 下载批次的进度、下载信息和暂停原因。",
        "payload": {},
    },
    "batch.resume_pdf_download": {
        "description": "用户处理登录、权限或验证码后，从暂停项继续当前下载批次。",
        "payload": {},
    },
    "download.recent": {
        "description": "查询 Chrome 下载历史中最近创建或更新的下载任务（实时查询，不依赖插件内存缓存）。",
        "payload": {"limit": "可选，1-50，默认 20"},
    },
    "flow.run": {
        "description": (
            "按顺序执行一组基础动作（steps），用一次调用完成整段拟人化检索流程（例如开检索页->切文献库->"
            "提交检索->按被引排序->读取结果）。每一步仍是与单独调用完全等价的真实页面操作，"
            "不会新增任何页面行为、不会自动重试或绕过登录/验证码；某一步失败即在当前步停下，"
            "返回已完成步骤的逐步明细，便于人工介入后继续。steps 只能引用已注册的基础动作，不接受脚本或选择器。"
        ),
        "payload": {
            "steps": (
                "必填，1-20 个动作对象数组，每个含 action（已注册基础动作名，不可为 flow.run）"
                "和 payload（该动作的参数对象，缺省 {}）"
            )
        },
    },
}


def _scan_captcha(value: Any, key: str | None = None, depth: int = 0) -> str | None:
    """递归扫描扩展回传的数据，命中拦截特征时返回证据字符串，否则返回 None。

    设计要点：正文类字段（text/preview/abstract）不参与文本级匹配。CNKI 上能搜到
    大量讲"验证码识别"的论文，早期版本因为把整页快照丢进来扫，出现过把正常检索
    结果页误判成拦截、然后整条流水线空转等人工介入的事故。
    """
    if depth > _CAPTCHA_SCAN_DEPTH:
        return None
    if isinstance(value, str):
        lowered = value.lower()
        for marker in CAPTCHA_URL_MARKERS:
            if marker in lowered:
                return f"URL 命中 {marker}：{value[:160]}"
        if key is None or key.lower() in CAPTCHA_TEXT_KEYS:
            for marker in CAPTCHA_TEXT_MARKERS:
                if marker in value:
                    return f"文本命中「{marker}」：{value[:120]}"
        return None
    if isinstance(value, dict):
        for child_key, child in value.items():
            hit = _scan_captcha(child, str(child_key), depth + 1)
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for child in value:
            hit = _scan_captcha(child, key, depth + 1)
            if hit:
                return hit
    return None


class CaptchaGate:
    """CNKI 安全验证拦截闸门——服务端的全局拦截状态机。

    为什么必须是服务端状态，而不是让每个调用方自己看返回值：

    1. 拦截是**全局**的。不是一个命令失败，而是接下来所有命令都会失败。调用方
       A 发现了，调用方 B 不知道，B 还会继续撞墙，把风控越惹越凶。
    2. 判定需要**上下文**。单次 probe 可能落在跳转中间态上，只有服务端的连续
       探活序列才能可靠判断"真解除了"。
    3. 恢复需要**有人盯着**。用户填验证码的那一两分钟里，Agent 侧脚本可能已经
       退出、超时、或者根本没在跑。服务端有常驻线程，可以自己等、自己探、
       解除后自己放行。

    状态机：
        clear ──(结果命中特征 / 扩展上报)──> blocked ──(连续 N 次探活干净)──> clear
                                                │
                                                └──(达 MAX_CAPTCHA_WAIT_SECONDS)──> 自动放弃，报错

    拦截期间的行为：
        - 不发任何真实命令给扩展（避免加重风控）
        - 利用扩展每 30s 的固有轮询，自动下发内置 probe 命令探活
        - 调用方带 waitForCaptchaSeconds 时，请求挂起在服务端，解除后自动继续执行
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._blocked = False
        self._detected_at: float | None = None
        self._cleared_at: float | None = None
        self._reason: str = ""
        self._detail: str = ""
        self._source: str = ""
        self._last_probe_at: float = 0.0
        self._clean_streak = 0
        self._probe_count = 0
        self._rounds: list[dict[str, Any]] = []
        self._pending_probes: dict[str, float] = {}
        self._epoch = 0

    # ---------- 状态变更 ----------

    def mark_blocked(self, reason: str, detail: str = "", source: str = "") -> bool:
        """置为拦截态。返回 True 表示这次调用真正触发了状态跃变。"""
        with self._condition:
            now = time.time()
            if self._blocked:
                # 已经在拦截中：只刷新证据，不重复计数，也不打断等待者。
                if detail:
                    self._detail = detail
                if source:
                    self._source = source
                self._clean_streak = 0
                return False
            self._blocked = True
            self._detected_at = now
            self._reason = reason
            self._detail = detail
            self._source = source
            self._clean_streak = 0
            self._probe_count = 0
            self._epoch += 1
            self._log_round_open(now)
            self._condition.notify_all()
            return True

    def mark_cleared(self, source: str = "") -> bool:
        """置为放行态，唤醒所有等待者。返回 True 表示确实从拦截态解除。"""
        with self._condition:
            if not self._blocked:
                self._clean_streak = 0
                return False
            now = time.time()
            self._blocked = False
            self._cleared_at = now
            self._log_round_close(now, source)
            self._reason = ""
            self._detail = ""
            self._source = ""
            self._clean_streak = 0
            self._epoch += 1
            self._condition.notify_all()
            return True

    def force_clear(self, source: str = "manual") -> bool:
        """人工强制解除。用于「页面明明正常了但探活没跟上」的兜底。"""
        with self._condition:
            if not self._blocked:
                return False
            now = time.time()
            self._blocked = False
            self._cleared_at = now
            self._log_round_close(now, source)
            self._reason = ""
            self._detail = ""
            self._source = ""
            self._clean_streak = 0
            self._epoch += 1
            self._condition.notify_all()
            return True

    # ---------- 查询 ----------

    @property
    def blocked(self) -> bool:
        with self._condition:
            return self._blocked

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            now = time.time()
            return {
                "blocked": self._blocked,
                "reason": self._reason or None,
                "detail": self._detail or None,
                "source": self._source or None,
                "blockedSeconds": round(now - self._detected_at, 1) if self._blocked and self._detected_at else 0.0,
                "detectedAt": self._detected_at,
                "clearedAt": self._cleared_at,
                "secondsSinceCleared": round(now - self._cleared_at, 1) if self._cleared_at else None,
                "probeCount": self._probe_count,
                "lastProbeAt": self._last_probe_at or None,
                "cleanStreak": self._clean_streak,
                "epoch": self._epoch,
                "rounds": list(self._rounds[-5:]),
                "hint": self._hint(),
            }

    def _hint(self) -> str | None:
        if not self._blocked:
            return None
        return (
            "CNKI 安全验证拦截中。请在 Chrome 里完成验证（点击文字 / 拖动滑块），"
            "本服务每约 30 秒自动探活一次，验证通过后会自动放行挂起的任务，无需重启脚本。"
        )

    def wait_until_clear(self, timeout_seconds: float) -> tuple[bool, float]:
        """阻塞等待解除。返回 (是否已解除, 实际等待秒数)。

        用 epoch 而不是 blocked 布尔值做等待条件，避免「解除→又被拦截→唤醒」的
        竞态把等待者误放出去。
        """
        deadline = time.monotonic() + max(timeout_seconds, 0)
        started = time.monotonic()
        with self._condition:
            while self._blocked:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, time.monotonic() - started
                self._condition.wait(timeout=remaining)
            return True, time.monotonic() - started

    # ---------- 探活 ----------

    def due_for_probe(self) -> bool:
        """拦截期间是否该下发一次内置探活命令。"""
        with self._condition:
            if not self._blocked:
                return False
            if self._pending_probes:
                return False  # 上一个 probe 还在飞，不重复下发
            return time.time() - self._last_probe_at >= CAPTCHA_PROBE_MIN_INTERVAL_SECONDS

    def register_probe(self, command_id: str) -> None:
        with self._condition:
            self._pending_probes[command_id] = time.time()
            self._last_probe_at = time.time()
            self._probe_count += 1

    def resolve_probe(self, command_id: str, result: Any, error: str | None) -> dict[str, Any]:
        """处理探活结果，返回本次判定摘要。"""
        self._pending_probes.pop(command_id, None)
        hit = _scan_captcha(result) or _scan_captcha(error)
        if hit:
            with self._condition:
                self._clean_streak = 0
                self._detail = hit
                return {"cleared": False, "evidence": hit, "cleanStreak": 0}
        with self._condition:
            self._clean_streak += 1
            streak = self._clean_streak
            cleared = False
            if streak >= CAPTCHA_CLEAR_CONFIRMATIONS:
                cleared = self.mark_cleared(source="auto-probe")
            return {"cleared": cleared, "evidence": None, "cleanStreak": streak}

    def inspect_result(self, result: Any, error: str | None, action: str = "") -> str | None:
        """检查一次普通命令的结果，命中拦截特征则置为拦截态。返回证据。"""
        hit = _scan_captcha(result) or _scan_captcha(error)
        if hit:
            self.mark_blocked(
                reason=f"CNKI 安全验证拦截（触发动作：{action or '未知'}）",
                detail=hit,
                source=f"action:{action or 'unknown'}",
            )
        elif self.blocked:
            # 普通命令在拦截期间不该被下发；若确实收到了干净结果，也计入解除凭据。
            with self._condition:
                self._clean_streak += 1
                if self._clean_streak >= CAPTCHA_CLEAR_CONFIRMATIONS:
                    self.mark_cleared(source=f"action:{action or 'unknown'}")
        return hit

    # ---------- 历史 ----------

    def _log_round_open(self, now: float) -> None:
        self._rounds.append({
            "detectedAt": now,
            "clearedAt": None,
            "durationSeconds": None,
            "probeCount": 0,
            "reason": self._reason,
            "source": self._source,
        })

    def _log_round_close(self, now: float, source: str) -> None:
        if not self._rounds:
            return
        last = self._rounds[-1]
        last["clearedAt"] = now
        last["durationSeconds"] = round(now - (last["detectedAt"] or now), 1)
        last["probeCount"] = self._probe_count
        last["resolvedBy"] = source


@dataclass
class Command:
    command_id: str
    action: str
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    delivered: bool = False
    completed: bool = False
    result: Any = None
    error: str | None = None
    abandoned: bool = False


class CommandBroker:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._queue: list[str] = []
        self._condition = threading.Condition()
        # 扩展最近一次长轮询到达的时间戳（epoch 秒）。0 表示从未连接。
        self.last_extension_seen: float = 0.0

    def enqueue(self, action: str, payload: dict[str, Any]) -> Command:
        command = Command(command_id=str(uuid.uuid4()), action=action, payload=payload)
        with self._condition:
            self._commands[command.command_id] = command
            self._queue.append(command.command_id)
            self._condition.notify_all()
        return command

    def next_command(self, timeout_seconds: int) -> Command | None:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                # 拦截期间绝不下发真实命令给扩展：填验证码的窗口期里任何额外页面
                # 操作都可能加重风控、触发更严的验证。真实命令先排队，等 GATE 解除后
                # 自动放行。探活由 do_GET /v1/extension/next-command 单独注入。
                if GATE.blocked:
                    return None
                while self._queue:
                    command_id = self._queue.pop(0)
                    command = self._commands.get(command_id)
                    if command and not command.completed:
                        command.delivered = True
                        return command

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def complete(self, command_id: str, result: Any = None, error: str | None = None) -> bool:
        with self._condition:
            command = self._commands.get(command_id)
            if not command or command.completed:
                return False
            command.result = result
            command.error = error
            command.completed = True
            is_probe = command_id.startswith(PROBE_PREFIX)
            self._condition.notify_all()
        # 锁外做拦截判定：避免 GATE 内部再次取锁造成的重入，也让判定逻辑独立演进。
        if is_probe:
            GATE.resolve_probe(command_id, result, error)
        else:
            action = command.action if command else ""
            GATE.inspect_result(result, error, action)
        return True

    def abandon(self, command: Command, reason: str = "调用方超时放弃") -> None:
        """调用方超时放弃：把命令移出队列并标记为已结束。

        不做这一步的话，超时命令会永久滞留在队列里——next_command() 只跳过
        completed 的命令，于是僵尸命令仍会被逐个交付给扩展执行，新命令只能排
        在它们后面。而扩展受 BRIDGE_ALARM（periodInMinutes=0.5，即 30s）限制
        每次只拉一个命令，积压一旦形成，所有新调用都会持续超时且无任何报错，
        极难定位。典型诱因：调用方频繁用短超时重试。
        """
        with self._condition:
            if command.command_id in self._queue:
                self._queue.remove(command.command_id)
            if not command.completed:
                command.completed = True
                command.abandoned = True
                command.error = reason
            self._condition.notify_all()

    def purge_stale(self, max_age_seconds: float = 120.0) -> int:
        """清理长时间未被交付的陈旧命令（兜底自愈），返回清理条数。"""
        now = time.time()
        removed = 0
        with self._condition:
            for command_id, command in list(self._commands.items()):
                if command.completed or command.delivered:
                    continue
                if now - command.created_at > max_age_seconds:
                    if command_id in self._queue:
                        self._queue.remove(command_id)
                    command.completed = True
                    command.abandoned = True
                    command.error = "陈旧命令已自动清理"
                    removed += 1
            if removed:
                self._condition.notify_all()
        return removed

    def wait_for_result(self, command: Command, timeout_seconds: int) -> Command:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while not command.completed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return command
                self._condition.wait(timeout=remaining)
            return command


BROKER = CommandBroker()
# 全局唯一的拦截闸门。HTTP 模式与 MCP 模式共享，因此无论 Agent 从哪条链路进来，
# 看到的拦截状态和等待行为都是一致的。
GATE = CaptchaGate()


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    """继承 ThreadingHTTPServer，但关闭地址复用，确保单机只能有一个实例。

    stdlib 的 http.server.HTTPServer 把类属性 allow_reuse_address 默认设为 1，
    ThreadingHTTPServer 未覆盖该属性，因此原样继承。这个设置在 POSIX 下主要是
    放宽 TIME_WAIT 状态下的重新绑定限制，但在 Windows 上语义完全不同：
    SO_REUSEADDR 会允许多个进程同时 bind() 到同一个 127.0.0.1:<port> 而不报错，
    谁都不知道对方存在。

    本项目正是因此翻过车：一次遗留了 3-4 个 `bridge_server.py --mode mcp`
    进程，netstat 里全部显示 LISTENING 在 8765，但每个进程持有自己独立的
    CommandBroker 内存队列——Chrome 扩展的长轮询和 Agent 的调用命令可能落在
    不同进程上，造成「等待插件响应超时」这类偶发甚至持续性故障，且没有任何
    报错提示，非常隐蔽。

    关掉 allow_reuse_address 后，第二个进程尝试 bind 同一端口会立刻收到
    OSError（Windows 上是 WinError 10048 "通常每个套接字地址只允许使用一次"），
    在 main() 里被捕获并打印清晰提示后退出，而不是静默地成为第二个幽灵监听者。
    """

    allow_reuse_address = False


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "CnkiLocalBridge/0.2"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", file=sys.stderr)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("请求体过大。")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象。")
        return value

    def _send_json(self, status: HTTPStatus | int, data: dict[str, Any] | None = None) -> None:
        body = b"" if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _not_found(self) -> None:
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在。"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            now = time.time()
            last_seen = BROKER.last_extension_seen
            connected = last_seen > 0 and (now - last_seen) < EXTENSION_ALIVE_WINDOW_SECONDS
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "service": "cnki-local-bridge",
                "extension": {
                    "connected": connected,
                    "lastSeenSecondsAgo": round(now - last_seen, 1) if last_seen > 0 else None,
                    "hint": (
                        None if connected else
                        "未检测到 Chrome 扩展连接。请确认已在 chrome://extensions 加载 extension/ 目录，"
                        "并保持 Chrome 运行；扩展最长约 30 秒轮询一次。"
                    )
                },
                "captcha": {
                    **GATE.snapshot(),
                    "autoResume": True,
                },
                "allowedActions": sorted(ALLOWED_ACTIONS),
                "tools": {action: TOOL_DESCRIPTIONS[action] for action in sorted(ALLOWED_ACTIONS)}
            })
            return

        if parsed.path == "/v1/captcha/status":
            self._send_json(HTTPStatus.OK, {"ok": True, "captcha": GATE.snapshot()})
            return

        if parsed.path == "/v1/extension/next-command":
            BROKER.last_extension_seen = time.time()
            raw_wait = parse_qs(parsed.query).get("wait", ["20"])[0]
            try:
                wait_seconds = min(max(int(raw_wait), 1), 25)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "wait 必须为整数。"})
                return

            # 拦截期间优先探活：不下发任何真实命令（避免加重风控），改向扩展注入一次
            # 内置 session.status 探针。探针结果回传后由 complete()→resolve_probe() 判定
            # 是否已解除；连续 2 次干净即自动放行后续排队命令。无需任何客户端轮询。
            if GATE.due_for_probe():
                probe_id = PROBE_PREFIX + uuid.uuid4().hex
                with BROKER._condition:
                    probe = Command(command_id=probe_id, action="session.status", payload={})
                    probe.delivered = True
                    BROKER._commands[probe_id] = probe
                GATE.register_probe(probe_id)
                self._send_json(HTTPStatus.OK, {
                    "id": probe_id,
                    "action": "session.status",
                    "payload": {},
                    "isProbe": True,
                })
                return

            command = BROKER.next_command(wait_seconds)
            if command is None:
                self._send_json(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.OK, {
                "id": command.command_id,
                "action": command.action,
                "payload": command.payload
            })
            return

        self._not_found()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            return

        if parsed.path == "/v1/call":
            action = payload.get("action")
            if action not in ALLOWED_ACTIONS:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "error": f"不支持的 Tool：{action}。",
                    "allowedActions": sorted(ALLOWED_ACTIONS)
                })
                return

            command_payload = payload.get("payload", {})
            if not isinstance(command_payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "payload 必须为对象。"})
                return

            timeout = payload.get("timeoutSeconds", 40)
            try:
                timeout = min(max(int(timeout), 1), MAX_CALL_TIMEOUT_SECONDS)
            except (TypeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "timeoutSeconds 必须为整数。"})
                return

            # ---- 验证码拦截闸门 ----
            # 被 CNKI 安全验证挡住时，本服务**自己知道**被拦截了，不会盲目给扩展下命令，
            # 而是：要么立刻返回明确的 503（blocked:true）让调用方知情，要么按
            # waitForCaptchaSeconds 在**服务端原地等待**用户填完验证码，解除后自动继续执行，
            # 全程对调用方透明——这就是「服务感知拦截、等待人工、自动续跑」的闭环。
            wait_captcha = payload.get("waitForCaptchaSeconds", 0)
            if wait_captcha is True:
                wait_captcha = MAX_CAPTCHA_WAIT_SECONDS
            try:
                wait_captcha = max(0, min(int(wait_captcha), MAX_CAPTCHA_WAIT_SECONDS))
            except (TypeError, ValueError):
                wait_captcha = 0

            if GATE.blocked:
                if wait_captcha > 0:
                    cleared, waited = GATE.wait_until_clear(wait_captcha)
                    if not cleared:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                            "ok": False,
                            "blocked": True,
                            "error": "等待验证码超时（已等待约 %d 秒）。请确认已在 Chrome 中完成安全验证后再试。" % int(waited),
                            "captcha": GATE.snapshot(),
                        })
                        return
                    # 已解除，继续往下正常执行本次调用
                else:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                        "ok": False,
                        "blocked": True,
                        "error": "CNKI 安全验证拦截中。请在 Chrome 中完成验证（点击文字 / 拖动滑块），本服务会自动放行后续任务，无需重启脚本。",
                        "captcha": GATE.snapshot(),
                        "howToWait": "带上 waitForCaptchaSeconds 参数，让本服务原地等待验证完成后再继续。",
                    })
                    return

            # flow.run 由本服务拆解为逐步基础动作，在服务端串行执行；
            # 扩展侧对此无感知，每一步仍是与单独调用等价的一次入队+等待。
            if action == "flow.run":
                result, flow_error = execute_flow(command_payload, timeout)
                if flow_error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": flow_error})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "data": result})
                return

            command = BROKER.enqueue(action, command_payload)
            completed = BROKER.wait_for_result(command, timeout)
            if not completed.completed:
                self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {
                    "ok": False,
                    "commandId": command.command_id,
                    "error": "等待插件响应超时。请确认 Chrome 已加载插件、CNKI 页面已授权，并等待下一次轮询。"
                })
                return

            if completed.error:
                self._send_json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "commandId": command.command_id,
                    "error": completed.error
                })
                return

            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "commandId": command.command_id,
                "data": completed.result
            })
            return

        if parsed.path == "/v1/extension/command-result":
            command_id = payload.get("commandId")
            if not isinstance(command_id, str) or not command_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "缺少 commandId。"})
                return
            accepted = BROKER.complete(command_id, result=payload.get("result"), error=payload.get("error"))
            self._send_json(HTTPStatus.OK if accepted else HTTPStatus.NOT_FOUND, {"ok": accepted})
            return

        if parsed.path == "/v1/extension/captcha-report":
            # 扩展侧主动上报：导航到安全验证页时立即 POST blocked=true（秒级），无需等 30s 轮询。
            blocked = bool(payload.get("blocked", False))
            detail = payload.get("detail") or payload.get("url") or ""
            if not isinstance(detail, str):
                detail = str(detail)
            if blocked:
                GATE.mark_blocked(
                    reason="扩展主动上报：检测到安全验证页",
                    detail=detail[:300],
                    source="extension-webNavigation",
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "blocked": True, "captcha": GATE.snapshot()})
                return
            # 上报「已离开验证页」：扩展在 kns 正常页 onCompleted 时调用，作为实时强证据直接解除。
            cleared = GATE.mark_cleared(source="extension-webNavigation")
            self._send_json(HTTPStatus.OK, {"ok": True, "blocked": False, "cleared": cleared, "captcha": GATE.snapshot()})
            return

        if parsed.path == "/v1/captcha/wait":
            # 阻塞等待解除。调用方（Agent / 脚本）不再需要自己轮询，一次请求等到底。
            try:
                wait_seconds = int(payload.get("timeoutSeconds", MAX_CAPTCHA_WAIT_SECONDS))
            except (TypeError, ValueError):
                wait_seconds = MAX_CAPTCHA_WAIT_SECONDS
            wait_seconds = max(1, min(wait_seconds, MAX_CAPTCHA_WAIT_SECONDS))
            if not GATE.blocked:
                self._send_json(HTTPStatus.OK, {"ok": True, "cleared": True, "waitedSeconds": 0.0, "captcha": GATE.snapshot()})
                return
            cleared, waited = GATE.wait_until_clear(wait_seconds)
            self._send_json(
                HTTPStatus.OK if cleared else HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": cleared, "cleared": cleared, "waitedSeconds": round(waited, 1), "captcha": GATE.snapshot()},
            )
            return

        if parsed.path == "/v1/captcha/clear":
            # 人工强制解除：用于「页面已正常但探活没跟上」的兜底，或调试用途。
            source = payload.get("source") or "manual"
            if not isinstance(source, str):
                source = "manual"
            cleared = GATE.force_clear(source=source[:40])
            self._send_json(HTTPStatus.OK, {"ok": True, "cleared": cleared, "captcha": GATE.snapshot()})
            return

        self._not_found()


def run_bridge_action(
    action: str,
    payload: dict[str, Any],
    timeout_seconds: int = DEFAULT_MCP_CALL_TIMEOUT_SECONDS,
    wait_captcha_seconds: int = 0,
) -> Any:
    """把一次 Tool 调用放进队列并同步等待扩展执行结果。

    供 MCP Tool 处理函数复用；成功返回 `completed.result`，失败/超时抛出
    RuntimeError，交由调用方（MCP 框架）转换成协议层的错误响应。

    ``wait_captcha_seconds > 0`` 时，若遇到 CNKI 安全验证拦截，会在**服务端原地等待**
    用户填完验证码，解除后自动重新下发本命令，对调用方透明。
    """
    if action not in ALLOWED_ACTIONS:
        raise RuntimeError(f"不支持的 Tool：{action}。")
    bounded_timeout = min(max(int(timeout_seconds), 1), MAX_CALL_TIMEOUT_SECONDS)
    # 拦截闸门：被安全验证挡住时先不盲目下发命令，而是等待人工或立即报错。
    if GATE.blocked:
        if wait_captcha_seconds and wait_captcha_seconds > 0:
            wait_captcha_seconds = min(int(wait_captcha_seconds), MAX_CAPTCHA_WAIT_SECONDS)
            cleared, waited = GATE.wait_until_clear(wait_captcha_seconds)
            if not cleared:
                raise RuntimeError(
                    f"等待验证码解除超时（约 {int(waited)} 秒）。请在 Chrome 中完成安全验证后重试。"
                )
        else:
            raise RuntimeError(
                "CNKI 安全验证拦截中：本服务已暂停下发命令。请在 Chrome 中完成验证"
                "（点击文字 / 拖动滑块），解除后会自动续跑；也可调用 captcha.wait 等待解除。"
            )
    BROKER.purge_stale()
    command = BROKER.enqueue(action, payload)
    completed = BROKER.wait_for_result(command, bounded_timeout)
    if not completed.completed:
        # 关键：超时后必须放弃该命令，否则它会滞留在队列里被扩展"补执行"，
        # 把后续命令全部挤到后面（扩展 30s 才拉一个），造成持续超时。
        BROKER.abandon(command)
        raise RuntimeError(
            "等待插件响应超时（该命令已移出队列，不会积压）。请确认 Chrome 已加载插件、"
            "CNKI 页面已授权；注意扩展每 30s 才拉取一次命令，超时建议设置为 40s 以上。"
        )
    if completed.error:
        raise RuntimeError(completed.error)
    return completed.result


def execute_flow(payload: dict[str, Any], step_timeout_seconds: int) -> tuple[dict[str, Any] | None, str | None]:
    """拆解并串行执行 flow.run 的 steps。

    返回 (result, error)：成功时 result 为逐步明细 dict、error 为 None；参数非法时
    result 为 None、error 为原因字符串。每一步失败都会停在该步，但整体仍视为「已执行到
    N 步」，通过 result 里的 steps/stopped 表达，不抛异常——因为某一步失败是业务结果，
    不是协议错误，调用方需要拿到逐步明细而不是一个笼统的失败。
    """
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "flow.run 需要非空的 steps 数组。"
    if len(steps) > MAX_FLOW_STEPS:
        return None, f"flow.run 最多支持 {MAX_FLOW_STEPS} 步。"
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return None, f"第 {index + 1} 步必须是对象。"
        step_action = step.get("action")
        if step_action == "flow.run":
            return None, "flow.run 不支持嵌套。"
        if step_action not in ALLOWED_ACTIONS:
            return None, f"第 {index + 1} 步的 action 不受支持：{step_action}。"
        step_payload = step.get("payload", {})
        if not isinstance(step_payload, dict):
            return None, f"第 {index + 1} 步的 payload 必须是对象。"

    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        step_action = step["action"]
        step_payload = step.get("payload", {})
        try:
            data = run_bridge_action(step_action, step_payload, timeout_seconds=step_timeout_seconds)
            results.append({"index": index, "action": step_action, "ok": True, "data": data})
        except RuntimeError as error:
            results.append({"index": index, "action": step_action, "ok": False, "error": str(error)})
            return {
                "completed": index,
                "total": len(steps),
                "failedAt": index,
                "stopped": True,
                "steps": results,
            }, None

    return {
        "completed": len(steps),
        "total": len(steps),
        "failedAt": None,
        "stopped": False,
        "steps": results,
    }, None


def build_mcp_server() -> Any:
    """构建一个 MCP stdio server，把 14 个受限动作注册为具名 Tool。

    每个 Tool 的参数用类型标注 + pydantic Field 表达枚举、长度、范围约束，
    由 MCP 框架在协议层生成 JSON Schema 并校验，出错不用再等扩展报错。
    lazy import：只有 --mode mcp 时才需要 mcp 依赖，--mode http 调试时
    仍可用系统自带 Python 直接跑，不强制安装额外依赖。
    """
    from typing import Annotated, Literal, Optional

    from mcp.server.fastmcp import FastMCP
    from pydantic import BaseModel, Field

    class AdvancedSearchCondition(BaseModel):
        field: Literal[
            "SU", "TKA", "KY", "TI", "FT", "AU", "AF", "TU", "FTU",
            "LY", "FU", "AB", "CO", "RF", "CLC", "XF", "DOI",
        ] = Field(description="检索字段代码：SU=主题 TKA=篇关摘 KY=关键词 TI=题名 FT=全文 AU=作者 AF=作者单位 TU=导师 FTU=第一导师 LY=学位授予单位 FU=基金 AB=摘要 CO=目录 RF=参考文献 CLC=中图分类号 XF=学科专业名称 DOI=DOI。")
        value: str = Field(min_length=1, max_length=120, description="检索词，1-120 字符。")
        logic: Optional[Literal["AND", "OR", "NOT"]] = Field(default="AND", description="与上一行的连接符，仅第 2、3 个条件生效，默认 AND。")

    # flow.run 的 step 只允许引用已注册的基础动作（排除 flow.run 自身）。
    FLOW_STEP_ACTIONS = tuple(sorted(action for action in ALLOWED_ACTIONS if action != "flow.run"))

    class FlowStep(BaseModel):
        action: Literal[FLOW_STEP_ACTIONS] = Field(description="基础动作名，如 search.submit / search.results / batch.start_pdf_download。")
        payload: dict[str, Any] = Field(default_factory=dict, description="该动作的参数对象，缺省为空对象。")

    mcp = FastMCP(
        name="cnki-local-bridge",
        instructions=(
            "通过用户已登录 Chrome 内可见的 CNKI 页面执行受限读取、导航和正常下载点击。"
            "不导出 Cookie、不调用 CNKI 下载或检索接口、不启动无头浏览器、不绕过验证码或权限控制。"
            "推荐调用顺序：session.status -> session.open_search/search.submit -> search.results "
            "-> (可选 search.sort) -> 按业务规则筛选 articleUrl -> batch.start_pdf_download "
            "-> batch.get_status -> 仅在用户处理了页面阻塞后才调用 batch.resume_pdf_download。"
            "注意：本 MCP 只覆盖中文文献（CNKI）。国际文献（Crossref/DOI）检索另有仓库根目录的 "
            "crossref_client.py 直连官方开放 API，不属于本 MCP 范围。"
        ),
    )

    @mcp.tool(name="session.status", description=TOOL_DESCRIPTIONS["session.status"]["description"])
    def session_status() -> Any:
        return run_bridge_action("session.status", {})

    @mcp.tool(name="session.open_search", description=TOOL_DESCRIPTIONS["session.open_search"]["description"])
    def session_open_search(
        query: Annotated[
            str,
            Field(default="", max_length=100, description="检索词；留空则只打开检索页，不执行检索。"),
        ] = "",
    ) -> Any:
        return run_bridge_action("session.open_search", {"query": query})

    @mcp.tool(name="page.snapshot", description=TOOL_DESCRIPTIONS["page.snapshot"]["description"])
    def page_snapshot() -> Any:
        return run_bridge_action("page.snapshot", {})

    @mcp.tool(name="page.dom", description=TOOL_DESCRIPTIONS["page.dom"]["description"])
    def page_dom(
        max_chars: Annotated[
            int,
            Field(default=120000, ge=1000, le=300000, description="返回 HTML 的最大字符数。"),
        ] = 120000,
    ) -> Any:
        return run_bridge_action("page.dom", {"maxChars": max_chars})

    @mcp.tool(name="page.navigate", description=TOOL_DESCRIPTIONS["page.navigate"]["description"])
    def page_navigate(
        url: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1000,
                pattern=r"^https://([A-Za-z0-9-]+\.)*cnki\.net/",
                description="仅接受 https://cnki.net/* 或 https://*.cnki.net/* 页面地址。",
            ),
        ],
    ) -> Any:
        return run_bridge_action("page.navigate", {"url": url})

    @mcp.tool(name="search.submit", description=TOOL_DESCRIPTIONS["search.submit"]["description"])
    def search_submit(
        query: Annotated[
            str,
            Field(min_length=1, max_length=100, description="检索关键词。"),
        ],
    ) -> Any:
        return run_bridge_action("search.submit", {"query": query})

    @mcp.tool(name="search.sort", description=TOOL_DESCRIPTIONS["search.sort"]["description"])
    def search_sort(
        sort_by: Annotated[
            Literal["citations", "downloads", "relevance", "publishedAt", "comprehensive"],
            Field(description="citations=被引，downloads=下载，relevance=相关度，publishedAt=发表时间，comprehensive=综合。"),
        ],
        limit: Annotated[
            int,
            Field(default=20, ge=1, le=50, description="排序刷新后返回多少条结果。"),
        ] = 20,
    ) -> Any:
        return run_bridge_action("search.sort", {"sortBy": sort_by, "limit": limit})

    @mcp.tool(name="search.results", description=TOOL_DESCRIPTIONS["search.results"]["description"])
    def search_results(
        limit: Annotated[
            int,
            Field(default=20, ge=1, le=50, description="返回结果条数。"),
        ] = 20,
    ) -> Any:
        return run_bridge_action("search.results", {"limit": limit})

    @mcp.tool(name="search.set_field", description=TOOL_DESCRIPTIONS["search.set_field"]["description"])
    def search_set_field(
        field: Annotated[
            Literal["SU", "TKA", "KY", "TI", "FT", "AU", "FI", "RP", "AF", "FU", "AB", "CO", "RF", "CLC", "LY", "DOI"],
            Field(description="检索字段代码：SU=主题 TKA=篇关摘 KY=关键词 TI=篇名 FT=全文 AU=作者 FI=第一作者 RP=通讯作者 AF=作者单位 FU=基金 AB=摘要 CO=小标题 RF=参考文献 CLC=分类号 LY=文献来源 DOI=DOI。"),
        ],
    ) -> Any:
        return run_bridge_action("search.set_field", {"field": field})

    @mcp.tool(name="search.set_library", description=TOOL_DESCRIPTIONS["search.set_library"]["description"])
    def search_set_library(
        library: Annotated[
            Literal["journal", "dissertation", "doctor", "master", "book", "conference", "newspaper", "almanac", "patent", "standard", "achievement"],
            Field(description="文献库：journal=学术期刊 dissertation=学位论文 doctor=博士 master=硕士 book=图书 conference=会议 newspaper=报纸 almanac=年鉴 patent=专利 standard=标准 achievement=成果。"),
        ],
    ) -> Any:
        return run_bridge_action("search.set_library", {"library": library})

    @mcp.tool(name="search.turn_page", description=TOOL_DESCRIPTIONS["search.turn_page"]["description"])
    def search_turn_page(
        page: Annotated[
            Optional[int],
            Field(default=None, ge=1, le=9999, description="目标页码（>=1），仅在当前可见页码内可用；与 direction 二选一。"),
        ] = None,
        direction: Annotated[
            Optional[Literal["next", "prev"]],
            Field(default=None, description="next=下一页，prev=上一页；与 page 二选一。"),
        ] = None,
    ) -> Any:
        if page is None and direction is None:
            raise RuntimeError("翻页需要 page 或 direction 之一。")
        return run_bridge_action("search.turn_page", {"page": page, "direction": direction})

    @mcp.tool(name="search.get_filters", description=TOOL_DESCRIPTIONS["search.get_filters"]["description"])
    def search_get_filters(
        groups: Annotated[
            Optional[list[str]],
            Field(default=None, description="要展开后读取的维度 groupid 数组，如 ['YE','WXLX','YJCC']；不传则只读已展开的维度。"),
        ] = None,
    ) -> Any:
        return run_bridge_action("search.get_filters", {"groups": groups})

    @mcp.tool(name="search.apply_filter", description=TOOL_DESCRIPTIONS["search.apply_filter"]["description"])
    def search_apply_filter(
        group: Annotated[
            str,
            Field(min_length=1, max_length=20, description="筛选维度 groupid，如 YE(年度)/WXLX(文献类型)/YJCC(研究层次)/LYBSM(来源类别)/CCL(学科)。"),
        ],
        values: Annotated[
            list[str],
            Field(min_length=1, max_length=20, description="要勾选的筛选值数组，如 ['2023','2024']。"),
        ],
    ) -> Any:
        return run_bridge_action("search.apply_filter", {"group": group, "values": values})

    @mcp.tool(name="search.advanced_submit", description=TOOL_DESCRIPTIONS["search.advanced_submit"]["description"])
    def search_advanced_submit(
        conditions: Annotated[
            list[AdvancedSearchCondition],
            Field(min_length=1, max_length=3, description="1-3 个检索条件，对应高级检索表单默认渲染的 3 行；使用前须先用 page.navigate 打开 /kns8s/AdvSearch 页面。"),
        ],
    ) -> Any:
        return run_bridge_action(
            "search.advanced_submit",
            {"conditions": [condition.model_dump() for condition in conditions]},
        )

    @mcp.tool(name="article.download_options", description=TOOL_DESCRIPTIONS["article.download_options"]["description"])
    def article_download_options() -> Any:
        return run_bridge_action("article.download_options", {})

    @mcp.tool(name="article.click_pdf_download", description=TOOL_DESCRIPTIONS["article.click_pdf_download"]["description"])
    def article_click_pdf_download() -> Any:
        return run_bridge_action("article.click_pdf_download", {})

    @mcp.tool(name="batch.start_pdf_download", description=TOOL_DESCRIPTIONS["batch.start_pdf_download"]["description"])
    def batch_start_pdf_download(
        article_urls: Annotated[
            list[str],
            Field(min_length=1, max_length=10, description="1-10 个 CNKI 论文详情页 URL，不能是下载接口地址；重复会被去重。"),
        ],
        interval_seconds: Annotated[
            int,
            Field(default=5, ge=3, le=30, description="每篇之间的最短间隔秒数。"),
        ] = 5,
    ) -> Any:
        return run_bridge_action(
            "batch.start_pdf_download",
            {"articleUrls": article_urls, "intervalSeconds": interval_seconds},
        )

    @mcp.tool(name="batch.get_status", description=TOOL_DESCRIPTIONS["batch.get_status"]["description"])
    def batch_get_status() -> Any:
        return run_bridge_action("batch.get_status", {})

    @mcp.tool(name="batch.resume_pdf_download", description=TOOL_DESCRIPTIONS["batch.resume_pdf_download"]["description"])
    def batch_resume_pdf_download() -> Any:
        return run_bridge_action("batch.resume_pdf_download", {})

    @mcp.tool(name="download.recent", description=TOOL_DESCRIPTIONS["download.recent"]["description"])
    def download_recent(
        limit: Annotated[
            int,
            Field(default=20, ge=1, le=50, description="返回最近多少条 Chrome 下载历史记录。"),
        ] = 20,
    ) -> Any:
        return run_bridge_action("download.recent", {"limit": limit})

    @mcp.tool(name="flow.run", description=TOOL_DESCRIPTIONS["flow.run"]["description"])
    def flow_run(
        steps: Annotated[
            list[FlowStep],
            Field(min_length=1, max_length=MAX_FLOW_STEPS, description="按顺序执行的基础动作数组；每步失败即停，返回逐步明细。"),
        ],
    ) -> Any:
        result, error = execute_flow(
            {"steps": [step.model_dump() for step in steps]},
            DEFAULT_MCP_CALL_TIMEOUT_SECONDS,
        )
        if error:
            raise RuntimeError(error)
        return result

    @mcp.tool(
        name="captcha.status",
        description="查询 CNKI 安全验证拦截状态：是否被拦截、拦截了多久、证据（命中哪种特征）、"
        "是否会自动续跑，以及最近几轮拦截的历史。拦截发生时任务会在服务端挂起并自动等待人工放行。",
    )
    def captcha_status() -> Any:
        return GATE.snapshot()

    @mcp.tool(
        name="captcha.wait",
        description="阻塞等待验证码拦截解除（上限 900 秒）。解除后返回实际等待秒数；超时则返回 cleared=false。"
        "调用方无需自己轮询，一次调用等到底。",
    )
    def captcha_wait(
        timeout_seconds: Annotated[
            int,
            Field(default=MAX_CAPTCHA_WAIT_SECONDS, ge=1, le=MAX_CAPTCHA_WAIT_SECONDS, description="最长等待秒数。"),
        ] = MAX_CAPTCHA_WAIT_SECONDS,
    ) -> Any:
        if not GATE.blocked:
            return {"cleared": True, "waitedSeconds": 0.0, **GATE.snapshot()}
        cleared, waited = GATE.wait_until_clear(timeout_seconds)
        return {"cleared": cleared, "waitedSeconds": round(waited, 1), **GATE.snapshot()}

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 CNKI Chrome 插件本地桥接服务")
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口，默认 8765")
    parser.add_argument(
        "--mode",
        choices=["http", "mcp"],
        default="http",
        help="http：完整 HTTP 服务（默认，兼容 curl 调试）；mcp：MCP stdio server，同时在后台线程为扩展保留 HTTP 长轮询。",
    )
    args = parser.parse_args()

    try:
        server = SingleInstanceHTTPServer(("127.0.0.1", args.port), BridgeRequestHandler)
    except OSError as error:
        print(
            f"[cnki-local-bridge] 无法监听 127.0.0.1:{args.port}：{error}",
            file=sys.stderr,
        )
        print(
            "[cnki-local-bridge] 这通常意味着已有另一个 bridge_server.py 实例正在运行"
            "（很可能是上一个会话遗留的旧进程）。请先结束命令行中包含"
            " bridge_server.py 的旧 python.exe 进程，再重新启动本进程。",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.mode == "mcp":
        # stdio 模式下 stdout 是 MCP JSON-RPC 帧的专用通道，任何多余的 print
        # 都会打坏协议流，所以这里的日志全部走 stderr。
        print(f"[cnki-local-bridge] HTTP 服务已在后台线程启动：http://127.0.0.1:{args.port}", file=sys.stderr)
        print("[cnki-local-bridge] 仅供 Chrome 扩展轮询及本机调试；服务不访问 CNKI，也不保存 Cookie。", file=sys.stderr)
        http_thread = threading.Thread(target=server.serve_forever, name="bridge-http", daemon=True)
        http_thread.start()
        try:
            mcp_app = build_mcp_server()
            print("[cnki-local-bridge] MCP stdio server 已启动，等待 Agent host 连接…", file=sys.stderr)
            mcp_app.run(transport="stdio")
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return

    print(f"CNKI 本地桥接服务已启动：http://127.0.0.1:{args.port}")
    print("仅接受本机访问；服务不访问 CNKI，也不保存 Cookie。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
