#!/usr/bin/env python3
"""cnki_client —— CNKI 本地研究助手的 **Agent 侧唯一入口**。

给 Agent / 脚本用的一站式客户端：封装对 `http://127.0.0.1:8765` 的调用、结果解析、
检索、逐个下载、批量下载与下载后的 PDF 文本提取。目的只有一个——**让 Agent 复用，
而不是每次手搓 urllib + 解析嵌套结构**（此前反复生成的 `_cnki_call.py` /
`_cnki_download.py` / `_extract_pdf.py` 以及 `collab_cnki.py` 等都属于这一类，
现已全部合并到这里）。

设计约束：

- 纯标准库，零第三方依赖，任何 Python 都能 import 或直接跑；
  唯一的可选依赖是 `pdfplumber`，只在调用 `extract_pdf_text` 时才惰性 import。
- `call()` 永不抛异常：无论 400 / 409 / 504 还是连接失败，都统一返回一个 dict，
  调用方只需判断 `resp.get("ok")`。
- **只看不下载的用法零配置**：个人路径（`local_config.json`）只在调用下载类函数时
  才惰性读取，检索 / 查看类函数完全不触碰配置。

下载有两种模式，风险差异很大
----------------------------

- **逐个下载（推荐，风控风险低）** — `download_one()` / `download_many()`
  每篇都走「导航 → 浏览摘要 → 点下载 → 等浏览器落盘 → 归位」，并插入随机停顿与
  批次长休息，请求稀疏、接近真人检索行为。
- **批量下载（⚠️ 高风险，慎用）** — `start_batch()`
  先集中存好详情链接、再一次性交给扩展批量点下载。与人类检索行为不符，
  **极易触发 CNKI 风控导致持续验证码拦截**。仅在你明确接受风险、且量很小时使用。

这是「可复用的便利层」，**不是强制 API**。若遇到它覆盖不了的任务（新的动作组合、
特殊的解析逻辑），Agent 仍可自行写脚本——本文件只求覆盖常见场景，不强求完备。

两种用法
---------

1) 作为库 import：:

    from cnki_client import call, health, collect, download_one, download_many

2) 作为命令行工具：:

    python cnki_client.py <action> [json_payload]            # 调用并打印完整 JSON
    python cnki_client.py --list <action> [json_payload] [n]  # 打印论文列表摘要
    python cnki_client.py --url  <action> [json_payload] [n]  # 只打印 index<TAB>title<TAB>articleUrl
    python cnki_client.py --health                            # 自检（扩展是否已连接）
    python cnki_client.py --ensure                            # 桥接服务不在则后台拉起
    python cnki_client.py --search <关键词> [候选输出.json]     # 探索检索（只看不下载）
    python cnki_client.py --one <详情页URL> [标题]              # 单篇逐个下载（推荐）
    python cnki_client.py --papers [候选清单.json] [时限小时]    # 逐个下载主入口（推荐）
    python cnki_client.py --download '<articleUrls JSON 数组>' [intervalSeconds]  # ⚠️高风险
    python cnki_client.py --status                            # 查询批量下载状态
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 45


class CaptchaBlocked(Exception):
    """CNKI 安全验证拦截。服务已暂停下发命令，等待用户在 Chrome 中完成验证。"""



# --------------------------------------------------------------------------
# 底层调用
# --------------------------------------------------------------------------

def call(action: str, payload: dict | None = None, timeout: int = DEFAULT_TIMEOUT,
         wait_for_captcha_seconds: int = 0) -> dict:
    """调用 `/v1/call`，返回统一 dict，永不抛异常。

    返回结构：
      - 成功：``{"ok": True, "commandId": "...", "data": {...}}``
      - 失败：``{"ok": False, "error": "...", ...}``（400 参数错 / 409 执行错 / 503 拦截 / 504 超时）
      - 网络失败：``{"ok": False, "error": "...", "network": True}``

    ``wait_for_captcha_seconds``：若遇到 CNKI 安全验证拦截，让服务端**原地等待**用户填完
    验证码再自动继续执行（上限 900s）。这是「服务感知拦截、等待人工、自动续跑」闭环的客户端入口；
    不传则被拦截时立即返回 ``{"ok": False, "blocked": True, ...}``。
    """
    wait_for_captcha_seconds = max(0, min(int(wait_for_captcha_seconds), 900))
    body = json.dumps({
        "action": action,
        "payload": payload or {},
        "timeoutSeconds": timeout,
        "waitForCaptchaSeconds": wait_for_captcha_seconds,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/call", data=body,
        headers={"Content-Type": "application/json"},
    )
    # HTTP 层超时必须覆盖「等待验证码 + 执行命令」两段，否则连接会被 urllib 提前掐断，
    # 表现为服务端还在等用户、客户端已报网络超时。
    http_timeout = timeout + 5
    if wait_for_captcha_seconds > 0:
        http_timeout = max(http_timeout, wait_for_captcha_seconds + timeout + 10)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 400 / 409 / 504 的 body 仍是 JSON，直接解析；解析不动再兜底。
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}", "network": True}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"无法连接 {BASE}：{exc}", "network": True}


def health() -> dict:
    """GET /health 自检，返回扩展连接状态等。"""
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"无法连接 {BASE}：{exc}", "network": True}


def captcha_status() -> dict:
    """GET /v1/captcha/status：查询服务端验证码拦截闸门状态。

    返回示例：``{"ok": True, "captcha": {"blocked": false, ...}}``；网络失败时
    ``{"ok": False, "network": True}``。
    """
    try:
        with urllib.request.urlopen(f"{BASE}/v1/captcha/status", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"无法连接 {BASE}：{exc}", "network": True}


def wait_for_captcha(timeout_seconds: int = 900) -> dict:
    """POST /v1/captcha/wait：阻塞等待拦截解除（上限 900s）。一次调用等到底，不轮询。

    返回 ``{"ok": True, "cleared": True, "waitedSeconds": 12.3, "captcha": {...}}``；
    超时则为 ``{"ok": False, "cleared": False, ...}``。
    """
    timeout_seconds = max(1, min(int(timeout_seconds), 900))
    body = json.dumps({"timeoutSeconds": timeout_seconds}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/captcha/wait", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds + 10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"等待验证码时连接失败：{exc}", "network": True}


def run_flow(steps: list[dict], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """调用 `flow.run` 一次串起多步基础动作，失败即停返回逐步明细。"""
    return call("flow.run", {"steps": steps}, timeout=timeout)


def ensure_server(wait_seconds: int = 20) -> bool:
    """确保本地桥接服务在跑；不在则后台拉起。

    虚拟环境不存在时（干净 clone 未建 venv）退回当前解释器 ``sys.executable``，
    保证任何环境都能拉起服务。
    """
    try:
        if health().get("ok"):
            return True
    except Exception:  # noqa: BLE001
        pass

    P = _paths()
    venv = str(P.VENV_PY)
    python_exe = venv if os.path.exists(venv) else sys.executable
    server_py = str(P.SERVER_PY)
    tool_dir = str(P.TOOL_DIR)
    print("[bridge] 未连接，尝试启动…")

    creationflags = 0x08000000 if sys.platform.startswith("win") else 0
    try:
        subprocess.Popen([python_exe, server_py, "--mode", "http"],
                         cwd=tool_dir, creationflags=creationflags)
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge] 启动失败：{exc}")
        return False

    for _ in range(max(1, wait_seconds // 2)):
        time.sleep(2)
        try:
            if health().get("ok"):
                print("[bridge] 启动成功")
                return True
        except Exception:  # noqa: BLE001
            pass
    print("[bridge] 启动超时，仍继续（后续调用可能失败）")
    return False


# --------------------------------------------------------------------------
# 结果解析
# --------------------------------------------------------------------------

def extract_results(resp: dict) -> tuple[list, dict]:
    """从单次 call 的返回里抽出论文列表。

    `data.results` 是「含 url/query/分页信息的 dict」，真正的论文数组在
    `data.results.results`。本函数兼容两种形态，返回 ``(papers, data)``。
    """
    data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
    r = data.get("results", [])
    if isinstance(r, dict):
        r = r.get("results", [])
    if not isinstance(r, list):
        r = []
    return r, data


def extract_article_urls(resp: dict) -> list[str]:
    """抽取论文详情页 URL（批量下载唯一可用的 URL 类型）。"""
    papers, _ = extract_results(resp)
    return [p.get("articleUrl", "") for p in papers if p.get("articleUrl")]


def format_paper(p: dict) -> str:
    """把一篇论文压成一行便于扫读。"""
    return (
        f"{p.get('index', '-')} | {p.get('title', '')} | {p.get('source', '')} | "
        f"{str(p.get('publishedAt', ''))[:4]} | 被引{p.get('citations', '-')} | "
        f"下载{p.get('downloads', '-')}"
    )


def print_papers(resp: dict, n: int = 12) -> None:
    """打印论文列表摘要（无结果时打印原始 data 便于排障）。"""
    papers, data = extract_results(resp)
    if not papers:
        print("NO RESULTS. data keys:", list(data.keys()))
        print(json.dumps(data, ensure_ascii=False)[:1500])
        return
    print(f"OK total_returned={len(papers)}")
    for p in papers[:n]:
        print(format_paper(p))


# --------------------------------------------------------------------------
# 批量下载  ⚠️ 高风险，慎用
#
# 本节是「先集中存好详情链接、再一次性交给扩展批量点下载」的模式。
# 与真人检索行为差异很大（短时间内集中请求详情页 + PDF），极易触发 CNKI 风控
# 导致持续验证码拦截。仅在你明确接受风险、且量很小的情况下使用；
# 常规批量需求请改用上面「逐个下载」的 download_many()。
# --------------------------------------------------------------------------

def start_batch(article_urls: list[str], interval_seconds: int = 5) -> dict:
    """⚠️ 高风险：启动批量 PDF 下载（1-10 个详情页 URL）。

    直接提交详情链接批量下载，与人类检索行为不符，**极易触发风控**。
    绝大多数场景请用 :func:`download_many`（拟人化逐个下载）。
    """
    return call("batch.start_pdf_download",
                {"articleUrls": article_urls, "intervalSeconds": interval_seconds},
                timeout=60)


def batch_status() -> dict:
    """查询当前/最近批次的执行状态（配合 ⚠️ 高风险 :func:`start_batch` 使用）。"""
    return call("batch.get_status", {})


def resume_batch() -> dict:
    """从暂停项恢复当前批次（需用户先处理页面阻塞）。

    配合 ⚠️ 高风险 :func:`start_batch` 使用。
    """
    return call("batch.resume_pdf_download", {})


def recent_downloads(limit: int = 20) -> dict:
    """查询 Chrome 下载历史。"""
    return call("download.recent", {"limit": limit})


# --------------------------------------------------------------------------
# 探索·检索（只看不下载，零配置依赖）
# --------------------------------------------------------------------------

def maybe_typo(query: str, p: float = 0.15) -> str | None:
    """约 p 概率构造一个「打错」的关键词（漏字/多打/插错字），用于模拟输入错误。"""
    if random.random() < p and len(query) >= 3:
        i = random.randint(0, len(query) - 1)
        mode = random.choice(["drop", "dup", "swap"])
        if mode == "drop":
            return query[:i] + query[i + 1:]
        if mode == "dup":
            return query[:i] + query[i] + query[i:]
        # swap：插一个常见错字
        return query[:i] + "的" + query[i + 1:] if query[i] != "的" else query
    return None


def collect(keyword: str, out: str | None = None, do_typo: bool = True) -> list[dict]:
    """检索关键词并产出候选清单（**不下载**，纯探索用）。

    拟人化：先打开知网首页停顿、约 15% 概率先打错一次再纠正、检索后浏览结果列表。

    :param out: 候选 JSON 输出路径；为 None 时只返回不落盘
    :return: ``[{"title", "url", "type"}, ...]``
    """
    print(f"[collect] 关键词：{keyword}")
    call("page.navigate", {"url": "https://kns.cnki.net/kns8s/"})
    hp(3, 7, "打开知网，看看页面")

    wrong = maybe_typo(keyword) if do_typo else None
    if wrong:
        print(f"  （手滑打错：{wrong}）")
        r = call("search.submit", {"query": wrong})
        hp(2, 4, "发现打错了，删掉重输")
        if not r.get("ok"):
            time.sleep(2)

    r = call("search.submit", {"query": keyword})
    if not r.get("ok"):
        hp(2, 4)
        r = call("search.submit", {"query": keyword})
    if not r.get("ok"):
        print("SEARCH_FAIL:", r.get("error"))
        return []

    hp(4, 9, "浏览检索结果列表")
    rows = r.get("data", {}).get("results", {}).get("results", [])
    items = [
        {"title": it.get("title", ""), "url": it.get("articleUrl", ""),
         "type": it.get("resourceType", "")}
        for it in rows if it.get("articleUrl")
    ]
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    for i, it in enumerate(items):
        print(f"  {i}|{it['type']}|{it['title']}")
    print(f"\n[TOTAL] {len(items)}" + (f" -> {out}" if out else ""))
    return items


# --------------------------------------------------------------------------
# 逐个下载 ★推荐主路径
#
# 与下方「批量下载」的本质区别：这里每篇都走
#     导航 → 浏览摘要 → 点下载 → 等浏览器落盘 → 归位
# 并插入随机停顿与批次长休息，请求稀疏、接近真人检索行为，风控风险低。
# --------------------------------------------------------------------------

_PATHS = None


def _paths():
    """懒加载本地路径配置。

    只有下载类函数需要；浏览/检索类函数完全不触碰配置，
    保证「只看不下载」的用法零配置依赖。
    """
    global _PATHS
    if _PATHS is None:
        import local_paths  # 与本模块同目录
        _PATHS = local_paths
    return _PATHS


def _cfg(key: str) -> str:
    return _paths().user_path(key)


def _log(msg: str) -> None:
    """写日志文件（配置里的 LOG）+ 打到 stdout。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(_cfg("LOG"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(line, flush=True)


def hp(min_s: float, max_s: float, label: str = "") -> None:
    """拟人化停顿：随机时长，绝不固定间隔。"""
    t = random.uniform(min_s, max_s)
    tag = f"  「{label}」" if label else ""
    print(f"  … 停顿 {t:4.1f}s{tag}")
    time.sleep(t)


VERIFY_MARKERS = ("/verify/", "verify/home", "captchatype")


def is_verify_url(url: str) -> bool:
    """知网验证码页判定。

    ⚠️ 绝不能用 ``captchaid=`` 判断：CNKI 风控时会给**正常可浏览的摘要页**
    也追加该参数，会把普通页误判成验证码页，导致流程永久假死。
    真实验证码页具备 ``/verify/`` + ``captchaType=`` 双重特征，不会被漏判。
    """
    u = (url or "").lower()
    return any(m in u for m in VERIFY_MARKERS)


def page_state() -> tuple[str, str]:
    """返回 ``(state, detail)``：

    - ``ok``      有可用的正常知网页（已排除验证码页），可以下载
    - ``verify``  当前停在验证码页
    - ``no_tab``  没有任何知网标签
    - ``timeout`` 命令超时 / 桥接异常

    超时必须 > 扩展轮询周期：扩展每 30s 才拉一次命令，
    用 12~20s 会产生大量「假性超时」——命令其实在排队等下一次轮询。
    """
    try:
        r = call("session.status", {}, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return "timeout", str(exc)
    if not r.get("ok"):
        return "timeout", r.get("error") or "无响应"
    d = r.get("data") or {}
    tabs = d.get("cnkiTabs") or []
    active = d.get("activeTab") or {}
    if is_verify_url(active.get("url", "")):
        return "verify", f"{active.get('title') or '安全验证'} | {str(active.get('url', ''))[:60]}"
    good = [t for t in tabs if not is_verify_url(t.get("url", ""))]
    if good:
        return "ok", f"{len(good)} 个可用知网标签"
    if tabs:
        return "verify", f"{active.get('title') or '未知'} | 标签均落在验证页"
    return "no_tab", "没有打开的知网标签"


def wait_for_human(tag: str = "", deadline: float | None = None) -> bool:
    """协同等待：告知当前卡在哪，等人工处理（填验证码 / 打开知网页）后自动继续。

    验证码拦截由**服务端 CaptchaGate** 负责感知与等待，所以遇到 ``verify``
    时不再本地轮询，直接放行——后续 :func:`call_aware` 会带
    ``waitForCaptchaSeconds`` 进入服务端等待，填完自动续。
    本地轮询只用于 ``no_tab`` / 桥接异常这类服务端帮不上忙的情况。
    """
    deadline = deadline or (time.time() + 3600)
    _log(f"[等待] {tag} 当前无法继续，进入协同等待。")
    while time.time() < deadline:
        state, detail = page_state()
        if state == "ok":
            _log(f"[恢复] 页面已就绪（{detail}），继续下载。")
            return True
        if state == "verify":
            _log(f"[验证码] 检测到验证页：{detail} —— 请在浏览器完成验证，完成后会自动续下。")
            return True
        if state == "no_tab":
            _log(f"[无标签] {detail} —— 请打开任意知网页面并保持。")
        else:
            _log(f"[命令超时] {detail} —— 扩展每 30s 才拉一次命令，可能只是没赶上轮询。")
        time.sleep(random.uniform(50, 70))
    _log("[超时] 超过时限仍无响应，停止等待。")
    return False


def call_aware(action: str, payload: dict | None = None, timeout: int = 45,
               wait_captcha: int = 600) -> dict:
    """带服务端验证码等待的调用：被拦截时由服务端原地等待人工填码，填完自动继续。

    等待超时（一直没填）返回 ``{"ok": False, "blocked": True, ...}``，
    由调用方决定是否再等一轮。
    """
    st = captcha_status().get("captcha", {})
    if st.get("blocked"):
        _log(f"[验证码] 服务端已检测到拦截（{st.get('blockedSeconds')}s）："
             f"请在浏览器完成验证，我自动等待…")
    r = call(action, payload or {}, timeout=timeout, wait_for_captcha_seconds=wait_captcha)
    if isinstance(r, dict) and r.get("blocked"):
        _log(f"[验证码超时] {r.get('error')} —— 请完成后我会再等一次。")
    return r


def safe_title(title: str) -> str:
    """把标题净化成可安全用作文件名的形式。"""
    bad = '\\/:*?"<>|\r\n\t'
    out = "".join("_" if c in bad else c for c in (title or "")).strip(" .")
    return out or "untitled"


def already_have(title: str) -> bool:
    """该标题是否已归档。兼容三种落盘命名：

    ① ``CN_<标题>.pdf``（本工具写入，精确相等）
    ② ``CN_<GBK原始名>.pdf``（早期产物，解码后比对）
    ③ ``CN_<标题>_<作者>.pdf``（CNKI 实际落盘常带作者后缀，前缀匹配）
    """
    if not title:
        return False
    try:
        names = [fn for fn in os.listdir(_cfg("DST"))
                 if fn.startswith("CN_") and fn.endswith(".pdf")]
    except Exception:  # noqa: BLE001
        return False
    for fn in names:
        raw = fn[3:-4]
        if raw == title:
            return True
        try:
            decoded = raw.encode("latin1").decode("gbk")
        except Exception:  # noqa: BLE001
            decoded = raw
        if decoded == title or decoded.startswith(title + "_") or raw.startswith(title + "_"):
            return True
    return False


def snapshot_src() -> set:
    """对浏览器下载目录拍快照（下载前调用，之后 diff 出新文件）。"""
    try:
        return set(os.listdir(_cfg("SRC")))
    except Exception:  # noqa: BLE001
        return set()


def move_new_files(title: str, wait_rounds: int = 30) -> bool:
    """等浏览器把 PDF 写完，再复制归位到论文目录。

    用「下载前快照 diff」定位新文件，并校验 ``%PDF`` 文件头——
    风控时知网会返回 HTML 验证页冒充 PDF，必须挡掉。
    """
    before = snapshot_src()
    pdfs: list = []
    for _ in range(wait_rounds):  # 最多等 ~90s 让 Chrome 写完
        time.sleep(3)
        new = snapshot_src() - before
        pdfs = [f for f in new if f.lower().endswith(".pdf")]
        if pdfs:
            break
    if not pdfs:
        return False

    moved = 0
    for i, f in enumerate(sorted(pdfs)):
        sp = os.path.join(_cfg("SRC"), f)
        try:
            with open(sp, "rb") as fh:
                head = fh.read(4)
        except Exception:  # noqa: BLE001
            continue
        if head != b"%PDF":
            _log(f"  [跳过] {f} 不是有效PDF（可能是HTML验证页），留待你处理")
            continue
        stem = safe_title(title)
        dst_name = f"CN_{stem}.pdf" if len(pdfs) == 1 else f"CN_{stem}_{i}.pdf"
        dp = os.path.join(_cfg("DST"), dst_name)
        try:
            shutil.copy2(sp, dp)
            moved += 1
            _log(f"  [已归位] -> {dst_name} ({os.path.getsize(dp) // 1024}KB)")
        except Exception as exc:  # noqa: BLE001
            _log(f"  [复制失败] {dst_name}: {exc}")
    return moved > 0


_DOWNLOAD_MAP: dict = {}


def _load_map() -> None:
    global _DOWNLOAD_MAP
    try:
        with open(_cfg("MAP"), encoding="utf-8") as f:
            _DOWNLOAD_MAP = json.load(f)
    except Exception:  # noqa: BLE001
        _DOWNLOAD_MAP = {}


def _save_map() -> None:
    try:
        with open(_cfg("MAP"), "w", encoding="utf-8") as f:
            json.dump(_DOWNLOAD_MAP, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def download_one(item: dict, retries: int = 3, deadline: float | None = None) -> bool:
    """单篇逐个下载（推荐）：导航 → 浏览摘要 → 点下载 → 等落盘 → 归位。

    :param item: ``{"title": ..., "url": ...}``
    :param deadline: 绝对时间戳，到点不再重试
    """
    deadline = deadline or (time.time() + 3600)
    title = item.get("title", "")
    url = item.get("url") or item.get("articleUrl")
    if not url:
        return False

    for _attempt in range(1, retries + 1):
        if time.time() >= deadline:
            return False
        if page_state()[0] != "ok":
            if not wait_for_human(f"下载前[{title}]", deadline):
                return False

        hp(2, 5, "进页面前")           # 拟人化：进页面前的停顿
        r = call_aware("page.navigate", {"url": url}, timeout=45)
        if not r.get("ok"):
            if r.get("blocked"):
                if not wait_for_human(f"导航等待验证码超时[{title}]", deadline):
                    return False
                continue
            _log(f"  [nav失败] {title}: {r.get('error')}")
            if page_state()[0] != "ok":
                if not wait_for_human(f"导航后[{title}]", deadline):
                    return False
            continue

        hp(4, 9, "浏览摘要页")         # 拟人化：像人一样读一会儿
        r2 = call_aware("article.click_pdf_download", {}, timeout=45)
        if not r2.get("ok"):
            if r2.get("blocked"):
                if not wait_for_human(f"下载等待验证码超时[{title}]", deadline):
                    return False
                continue
            _log(f"  [click失败] {title}: {r2.get('error')}")
            if page_state()[0] != "ok":
                if not wait_for_human(f"点击下载[{title}]", deadline):
                    return False
            continue

        if move_new_files(title):
            _DOWNLOAD_MAP[title] = item.get("type")
            _save_map()
            return True
        _log(f"  [未检测到文件] {title}: 可能触发验证码或下载未启动")
        if page_state()[0] != "ok":
            if not wait_for_human(f"下载后[{title}]", deadline):
                return False
        # 否则再试一次
    return False


def download_many(targets: list | None = None, targets_file: str | None = None,
                  hours: float = 8, per_session: int | None = None) -> dict:
    """逐个下载主入口（推荐）：对候选清单逐篇走 :func:`download_one`，
    篇间随机停顿、每 2–3 篇长休息 3–8 分钟降温，遇验证码自动协同等待。

    :param targets: 候选列表；为 None 时从 ``targets_file`` 或配置的 TARGETS 读取
    :param hours: 总时限（小时），到点自动收尾
    :param per_session: 每批几篇；为 None 时随机 2–3（更接近真人）
    :return: ``{"done": 成功数, "todo": 待下数, "remaining": 剩余数}``
    """
    deadline = time.time() + hours * 3600
    _load_map()
    if targets is None:
        path = targets_file or _cfg("TARGETS")
        try:
            with open(path, encoding="utf-8") as f:
                targets = json.load(f)
        except Exception as exc:  # noqa: BLE001
            _log(f"[清单缺失] 读不到候选清单 {path}：{exc}")
            return {"done": 0, "todo": 0, "remaining": 0}

    todo = [t for t in targets if not already_have(t.get("title", ""))]
    _log(f"启动逐个下载。候选 {len(targets)} 篇，已归位 {len(_DOWNLOAD_MAP)} 篇，"
         f"本次待下 {len(todo)} 篇。")

    state, detail = page_state()
    if state != "ok":
        _log(f"启动检查：{detail}，先进入协同等待。")
        if not wait_for_human("启动前", deadline):
            _log("等待超时，退出。")
            return {"done": 0, "todo": len(todo), "remaining": len(todo)}

    done = 0
    idx = 0
    session_size = per_session or random.choice([2, 3])
    while idx < len(todo) and time.time() < deadline:
        item = todo[idx]
        title = item.get("title", "")
        if already_have(title):
            idx += 1
            continue
        if download_one(item, deadline=deadline):
            done += 1
            _log(f"[进度] 成功 {done} 篇，累计已归位 {len(_DOWNLOAD_MAP)} 篇。下一篇…")
        else:
            _log(f"[跳过] {title} 本次未能下载（可能持续风控），留待下次。")
        idx += 1

        hp(5, 11, "篇间停顿")           # 拟人化：每篇之间小停顿
        if idx % session_size == 0:
            session_size = per_session or random.choice([2, 3])
            rest = random.uniform(180, 480)   # 3–8 分钟
            _log(f"[休息] 本批完成，休息 {rest // 60:.0f} 分钟降温…"
                 f"（你可继续填验证码，我不操作）")
            time.sleep(rest)

    _log(f"逐个下载结束。本次成功 {done} 篇，累计已归位 {len(_DOWNLOAD_MAP)} 篇。"
         f"剩余待下 {len(todo) - done} 篇（下次接着续）。")
    return {"done": done, "todo": len(todo), "remaining": len(todo) - done}


# --------------------------------------------------------------------------
# 下载后处理（可选依赖 pdfplumber）
# --------------------------------------------------------------------------

def extract_pdf_text(pdf_path: str) -> str:
    """提取单个 PDF 的全文文本，逐页用 ``===== 第 N 页 =====`` 分隔。

    需要第三方库 `pdfplumber`；未安装时抛出带安装提示的 ImportError。
    """
    try:
        import pdfplumber  # 惰性 import，保持主路径零依赖
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "提取 PDF 文本需要 pdfplumber，请先安装：pip install pdfplumber"
        ) from exc

    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            parts.append(f"\n===== 第 {i + 1} 页 =====\n{text}")
    return "\n".join(parts)


def dump_pdfs_to_txt(pdf_names: list[str], downloads_dir: str, out_dir: str) -> None:
    """批量把下载目录里的 PDF 转成同目录下同名 .txt。

    :param pdf_names: 文件名列表（不含目录）。
    :param downloads_dir: Chrome 下载目录（绝对路径）。
    :param out_dir: 输出 .txt 的目标目录（自动创建）。
    """
    os.makedirs(out_dir, exist_ok=True)
    for name in pdf_names:
        pdf_path = os.path.join(downloads_dir, name)
        txt_path = os.path.join(out_dir, name.replace(".pdf", ".txt"))
        if not os.path.exists(pdf_path):
            print(f"MISSING: {name}")
            continue
        try:
            full = extract_pdf_text(pdf_path)
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(full)
            print(f"OK {name} -> {len(full)} chars")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {name}: {exc}")


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0

    if argv[0] == "--health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
        return 0

    if argv[0] == "--status":
        print(json.dumps(batch_status(), ensure_ascii=False, indent=2))
        return 0

    if argv[0] == "--download":
        urls = json.loads(argv[1]) if len(argv) > 1 else []
        interval = int(argv[2]) if len(argv) > 2 else 5
        print(json.dumps(start_batch(urls, interval), ensure_ascii=False, indent=2))
        return 0

    if argv[0] == "--ensure":
        # 确保桥接服务在跑（不在则后台拉起）
        print(json.dumps({"ok": ensure_server()}, ensure_ascii=False))
        return 0

    if argv[0] == "--search":
        # 探索检索（只看不下载）：--search <关键词> [候选输出.json]
        kw = argv[1] if len(argv) > 1 else ""
        out = argv[2] if len(argv) > 2 else None
        items = collect(kw, out)
        print(json.dumps({"count": len(items)}, ensure_ascii=False))
        return 0

    if argv[0] == "--one":
        # 单篇逐个下载（推荐）：--one <详情页URL> [标题]
        url = argv[1] if len(argv) > 1 else ""
        title = argv[2] if len(argv) > 2 else ""
        ok = download_one({"title": title or url, "url": url})
        print(json.dumps({"ok": ok}, ensure_ascii=False))
        return 0

    if argv[0] == "--papers":
        # 逐个下载主入口（推荐）：--papers [候选清单.json] [总时限小时]
        tf = argv[1] if len(argv) > 1 else None
        hours = float(argv[2]) if len(argv) > 2 else 8.0
        res = download_many(targets_file=tf, hours=hours)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if argv[0] in ("--list", "--url"):
        action = argv[1]
        payload = json.loads(argv[2]) if len(argv) > 2 and argv[2] else {}
        n = int(argv[3]) if len(argv) > 3 else 12
        resp = call(action, payload)
        if argv[0] == "--list":
            print_papers(resp, n)
        else:
            for p in extract_results(resp)[0][:n]:
                print(f"{p.get('index', '')}\t{p.get('title', '')}\t{p.get('articleUrl', '')}")
        return 0

    # 默认：调用单个 action 并打印完整 JSON
    action = argv[0]
    payload = json.loads(argv[1]) if len(argv) > 1 and argv[1] else {}
    print(json.dumps(call(action, payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
