"""本机 CNKI 插件桥接服务。

服务本身不访问 CNKI，不保存 Cookie；只负责把 Tool 请求排队，并等待 Chrome
扩展返回用户已授权 CNKI 标签页中的页面数据。

支持两种运行模式（--mode）：

- http（默认，向后兼容）：完整 HTTP 服务监听 127.0.0.1:<port>。
  同时服务 Agent 侧调试用的 `/v1/call`，以及扩展侧轮询用的
  `/v1/extension/next-command` / `/v1/extension/command-result`。
  不依赖 mcp 包，可用系统自带 Python 直接跑，适合 curl 手工调试。

- mcp：作为标准 MCP stdio server 运行，供支持 MCP 协议的 Agent host（如
  WorkBuddy）通过 stdin/stdout 发现和调用 14 个具名 Tool，参数用
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
    "article.download_options",
    "article.click_pdf_download",
    "batch.start_pdf_download",
    "batch.get_status",
    "batch.resume_pdf_download",
    "download.recent",
}
MAX_CALL_TIMEOUT_SECONDS = 45
DEFAULT_MCP_CALL_TIMEOUT_SECONDS = 40

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
}


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


class CommandBroker:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._queue: list[str] = []
        self._condition = threading.Condition()

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
            self._condition.notify_all()
            return True

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
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "service": "cnki-local-bridge",
                "allowedActions": sorted(ALLOWED_ACTIONS),
                "tools": {action: TOOL_DESCRIPTIONS[action] for action in sorted(ALLOWED_ACTIONS)}
            })
            return

        if parsed.path == "/v1/extension/next-command":
            raw_wait = parse_qs(parsed.query).get("wait", ["20"])[0]
            try:
                wait_seconds = min(max(int(raw_wait), 1), 25)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "wait 必须为整数。"})
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

        self._not_found()


def run_bridge_action(action: str, payload: dict[str, Any], timeout_seconds: int = DEFAULT_MCP_CALL_TIMEOUT_SECONDS) -> Any:
    """把一次 Tool 调用放进队列并同步等待扩展执行结果。

    供 MCP Tool 处理函数复用；成功返回 `completed.result`，失败/超时抛出
    RuntimeError，交由调用方（MCP 框架）转换成协议层的错误响应。
    """
    if action not in ALLOWED_ACTIONS:
        raise RuntimeError(f"不支持的 Tool：{action}。")
    bounded_timeout = min(max(int(timeout_seconds), 1), MAX_CALL_TIMEOUT_SECONDS)
    command = BROKER.enqueue(action, payload)
    completed = BROKER.wait_for_result(command, bounded_timeout)
    if not completed.completed:
        raise RuntimeError(
            "等待插件响应超时。请确认 Chrome 已加载插件、CNKI 页面已授权，并等待下一次轮询。"
        )
    if completed.error:
        raise RuntimeError(completed.error)
    return completed.result


def build_mcp_server() -> Any:
    """构建一个 MCP stdio server，把 14 个受限动作注册为具名 Tool。

    每个 Tool 的参数用类型标注 + pydantic Field 表达枚举、长度、范围约束，
    由 MCP 框架在协议层生成 JSON Schema 并校验，出错不用再等扩展报错。
    lazy import：只有 --mode mcp 时才需要 mcp 依赖，--mode http 调试时
    仍可用系统自带 Python 直接跑，不强制安装额外依赖。
    """
    from typing import Annotated, Literal

    from mcp.server.fastmcp import FastMCP
    from pydantic import Field

    mcp = FastMCP(
        name="cnki-local-bridge",
        instructions=(
            "通过用户已登录 Chrome 内可见的 CNKI 页面执行受限读取、导航和正常下载点击。"
            "不导出 Cookie、不调用 CNKI 下载或检索接口、不启动无头浏览器、不绕过验证码或权限控制。"
            "推荐调用顺序：session.status -> session.open_search/search.submit -> search.results "
            "-> (可选 search.sort) -> 按业务规则筛选 articleUrl -> batch.start_pdf_download "
            "-> batch.get_status -> 仅在用户处理了页面阻塞后才调用 batch.resume_pdf_download。"
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

    server = ThreadingHTTPServer(("127.0.0.1", args.port), BridgeRequestHandler)

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
