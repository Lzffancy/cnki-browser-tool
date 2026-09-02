#!/usr/bin/env python3
"""cnki_client —— CNKI 本地研究助手的 Agent 侧内置客户端库（inner tool）。

给 Agent / 脚本用的轻量客户端：封装对 `http://127.0.0.1:8765` 的调用、结果解析、
批量下载与下载后的 PDF 文本提取。目的只有一个——**让 Agent 复用，而不是每次手搓
urllib + 解析嵌套结构**（此前反复生成的 `_cnki_call.py` / `_cnki_download.py` /
`_extract_pdf.py` 都属于这一类，现已合并到这里）。

设计约束：

- 纯标准库（urllib + json + os + sys），零第三方依赖，任何 Python 都能 import 或直接跑；
  唯一的可选依赖是 `pdfplumber`，只在调用 `extract_pdf_text` 时才惰性 import。
- `call()` 永不抛异常：无论 400 / 409 / 504 还是连接失败，都统一返回一个 dict，
  调用方只需判断 `resp.get("ok")`。

这是「可复用的便利层」，**不是强制 API**。若遇到它覆盖不了的任务（新的动作组合、
特殊的解析逻辑），Agent 仍可自行写脚本——本文件只求覆盖常见场景，不强求完备。

两种用法
---------

1) 作为库 import：:

    from cnki_client import call, health, extract_results, start_batch, batch_status

2) 作为命令行工具：:

    python cnki_client.py <action> [json_payload]            # 调用并打印完整 JSON
    python cnki_client.py --list <action> [json_payload] [n]  # 打印论文列表摘要
    python cnki_client.py --url  <action> [json_payload] [n]  # 只打印 index<TAB>title<TAB>articleUrl
    python cnki_client.py --health                            # 自检（扩展是否已连接）
    python cnki_client.py --download '<articleUrls JSON 数组>' [intervalSeconds]
    python cnki_client.py --status                            # 查询批量下载状态
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 45


# --------------------------------------------------------------------------
# 底层调用
# --------------------------------------------------------------------------

def call(action: str, payload: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """调用 `/v1/call`，返回统一 dict，永不抛异常。

    返回结构：
      - 成功：``{"ok": True, "commandId": "...", "data": {...}}``
      - 失败：``{"ok": False, "error": "...", ...}``（400 参数错 / 409 执行错 / 504 超时）
      - 网络失败：``{"ok": False, "error": "...", "network": True}``
    """
    body = json.dumps({
        "action": action,
        "payload": payload or {},
        "timeoutSeconds": timeout,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/v1/call", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
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


def run_flow(steps: list[dict], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """调用 `flow.run` 一次串起多步基础动作，失败即停返回逐步明细。"""
    return call("flow.run", {"steps": steps}, timeout=timeout)


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
# 批量下载
# --------------------------------------------------------------------------

def start_batch(article_urls: list[str], interval_seconds: int = 5) -> dict:
    """启动批量 PDF 下载（1-10 个详情页 URL）。"""
    return call("batch.start_pdf_download",
                {"articleUrls": article_urls, "intervalSeconds": interval_seconds},
                timeout=60)


def batch_status() -> dict:
    """查询当前/最近批次的执行状态。"""
    return call("batch.get_status", {})


def resume_batch() -> dict:
    """从暂停项恢复当前批次（需用户先处理页面阻塞）。"""
    return call("batch.resume_pdf_download", {})


def recent_downloads(limit: int = 20) -> dict:
    """查询 Chrome 下载历史。"""
    return call("download.recent", {"limit": limit})


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
