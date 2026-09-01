"""本机 CNKI 插件桥接服务。

服务仅监听 127.0.0.1。它不访问 CNKI，不保存 Cookie；只负责把本地 Tool 请求
排队，并等待 Chrome 扩展返回用户已授权 CNKI 标签页中的页面数据。
"""

from __future__ import annotations

import argparse
import json
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

# Tool 元数据同时服务于 /health 自描述接口和后续 Agent 的 Tool 注册。
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
        "description": "自动创建或复用 CNKI 检索页，在页面检索框中输入关键词并点击原生检索按钮。",
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
        "description": "查询插件运行期间 Chrome 最近创建或更新的下载任务。",
        "payload": {},
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
    server_version = "CnkiLocalBridge/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 CNKI Chrome 插件本地桥接服务")
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口，默认 8765")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), BridgeRequestHandler)
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
