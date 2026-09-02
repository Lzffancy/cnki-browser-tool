#!/usr/bin/env python3
"""crossref_client —— 国际文献（DOI/Crossref）数据源的 Agent 侧客户端库（inner tool）。

给 Agent / 脚本用的轻量客户端：直连 Crossref REST API（检索 + 元数据）与 Unpaywall
API（开放获取检测），补齐 CNKI 覆盖不到的国际文献检索、全球被引数、OA 免费全文定位。

定位：与 `cnki_client.py` 是平级的两个独立数据源，不是它的附属或补充：

- `cnki_client.py` —— 中文文献（CNKI），走本机桥接服务（拟人化浏览器）检索 + PDF 下载；
- 本文件 —— 国际文献（Crossref/DOI），直连官方开放 API 检索 + 元数据 + 全球被引 + OA 定位。

两者互不依赖、各自完整。Agent 拿到「找论文」需求时，按语言/来源选择或两个都查：
英文/DOI 走本文件，中文走 `cnki_client.py`；两者输出字段已基本对齐
（title/authors/source/year/citations/doi），下游无需感知数据源差异。

设计约束：

- 纯标准库（urllib + json + sys），零第三方依赖，任何 Python 都能 import 或直接跑。
- 所有函数永不抛异常：无论 404 / 422 / 429 / 网络失败，都统一返回 dict，
  调用方只需判断 ``resp.get("ok")``。
- 官方 polite pool：Crossref 要求 User-Agent 带项目名 + mailto，Unpaywall 要求
  email 参数；两者都用 MAILTO 常量，也是它们的限流凭据。

两种用法
---------

1) 作为库 import：:

    from crossref_client import search, resolve, check_oa, resolve_with_oa

2) 作为命令行工具：:

    python crossref_client.py --search <query> [rows]   # 关键词检索国际文献
    python crossref_client.py --doi <doi>              # 拉单篇完整元数据
    python crossref_client.py --oa <doi>               # 判 OA，给免费 PDF 链接
    python crossref_client.py --full <doi>             # 元数据 + OA 一次到位

限频提示
--------

- Crossref：约 50 req/s，本地串行使用无需担心。
- Unpaywall：限频较严格（约 100k req/day，但突发限流常见），连续批量查询多个
  DOI 时建议串行并在两次调用之间 sleep 1 秒。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

CROSSREF_API = "https://api.crossref.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
MAILTO = "lizhuofan_998@foxmail.com"
USER_AGENT = f"crossref-client/0.1 (mailto:{MAILTO})"
DEFAULT_TIMEOUT = 30

# Crossref 检索时裁剪的字段，省流量、加快解析。
_SELECT_FIELDS = "DOI,title,author,container-title,published,is-referenced-by-count"


# --------------------------------------------------------------------------
# 底层 HTTP
# --------------------------------------------------------------------------

def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, dict | None, str | None]:
    """GET 一个 JSON 接口，永不抛异常，返回 ``(ok, data, error)``。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        # 404（DOI 不存在）/ 422（缺 email 等参数）/ 429（限频）等。
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        return False, None, f"HTTP {exc.code}" + (f"：{detail}" if detail else "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, None, f"网络错误：{exc}"


def _normalize_doi(doi: str) -> str:
    """把用户可能粘贴的各种 DOI 形态归一成裸 DOI。"""
    for prefix in (
        "https://doi.org/", "http://doi.org/",
        "https://dx.doi.org/", "http://dx.doi.org/",
        "doi:",
    ):
        if doi.startswith(prefix):
            return doi[len(prefix):]
    return doi


# --------------------------------------------------------------------------
# 字段归一化
# --------------------------------------------------------------------------

def _norm_work(item: dict) -> dict:
    """把 Crossref 的 work 对象归一成统一字段名（对标 CNKI 字段习惯）。

    Crossref 的 title / container-title 是数组，author 是对象数组，
    published.date-parts 是嵌套数组，这里统一拍平成标量/字符串列表。
    """
    title = item.get("title") or []
    container = item.get("container-title") or []

    authors: list[str] = []
    for a in item.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part)
        if name:
            authors.append(name)

    pub = item.get("published") or {}
    date_parts = pub.get("date-parts") or [[]]
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    return {
        "doi": item.get("DOI"),
        "title": title[0] if title else "",
        "authors": authors,
        "source": container[0] if container else "",
        "year": year,
        "citations": item.get("is-referenced-by-count"),
    }


# --------------------------------------------------------------------------
# 检索 / 元数据 / OA 检测
# --------------------------------------------------------------------------

def search(query: str, rows: int = 10, sort: str = "relevance") -> dict:
    """关键词检索国际文献，返回 DOI 列表 + 元数据 + 被引数。

    :param query: 检索词（必填）。
    :param rows: 返回条数，1-100，默认 10。
    :param sort: 排序方式，relevance（默认）/ score / is-referenced-by-count / published。
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query 不能为空。"}
    try:
        rows = min(max(int(rows), 1), 100)
    except (TypeError, ValueError):
        rows = 10

    params = {
        "query": query,
        "rows": str(rows),
        "sort": sort,
        "select": _SELECT_FIELDS,
        "mailto": MAILTO,
    }
    url = f"{CROSSREF_API}/works?{urllib.parse.urlencode(params)}"
    ok, data, error = _get_json(url)
    if not ok:
        return {"ok": False, "error": error}

    msg = data.get("message") or {}
    items = msg.get("items") or []
    return {
        "ok": True,
        "total": msg.get("total-results"),
        "results": [_norm_work(it) for it in items],
    }


def resolve(doi: str) -> dict:
    """拉单篇 DOI 的完整元数据（标题/作者/期刊/年份/被引/摘要/参考文献数）。"""
    doi = _normalize_doi((doi or "").strip())
    if not doi:
        return {"ok": False, "error": "doi 不能为空。"}

    url = f"{CROSSREF_API}/works/{urllib.parse.quote(doi)}"
    ok, data, error = _get_json(url)
    if not ok:
        return {"ok": False, "error": error, "doi": doi}

    msg = data.get("message") or {}
    out = _norm_work(msg)
    out["publisher"] = msg.get("publisher")
    out["type"] = msg.get("type")
    out["abstract"] = msg.get("abstract")
    out["reference_count"] = msg.get("reference-count")
    out["url"] = msg.get("URL") or f"https://doi.org/{doi}"
    return {"ok": True, **out}


def check_oa(doi: str) -> dict:
    """用 Unpaywall 判一篇论文是否开放获取，有则返回免费 PDF 链接。"""
    doi = _normalize_doi((doi or "").strip())
    if not doi:
        return {"ok": False, "error": "doi 不能为空。"}

    url = f"{UNPAYWALL_API}/{urllib.parse.quote(doi)}?email={urllib.parse.quote(MAILTO)}"
    ok, data, error = _get_json(url)
    if not ok:
        return {"ok": False, "error": error, "doi": doi}

    loc = data.get("best_oa_location") or {}
    return {
        "ok": True,
        "doi": data.get("doi") or doi,
        "title": data.get("title"),
        "year": data.get("year"),
        "publisher": data.get("publisher"),
        "is_oa": bool(data.get("is_oa")),
        "oa_status": data.get("oa_status"),
        # 优先 url_for_pdf；有些条目只有落地页 url，此时用 url 兜底。
        "pdf_url": loc.get("url_for_pdf") or loc.get("url"),
        "landing_url": loc.get("url_for_landing_page") or loc.get("url"),
        "host_type": loc.get("host_type"),
        "version": loc.get("version"),
    }


def resolve_with_oa(doi: str) -> dict:
    """元数据 + OA 一次到位：先 Crossref 拉元数据，再 Unpaywall 判 OA。

    返回结构 = resolve 的字段 + 一个额外的 ``oa`` 子对象（check_oa 的结果）。
    """
    result = resolve(doi)
    if not result.get("ok"):
        return result
    oa = check_oa(doi)
    result["oa"] = oa if oa.get("ok") else {"ok": False, "error": oa.get("error")}
    return result


# --------------------------------------------------------------------------
# 格式化输出
# --------------------------------------------------------------------------

def _format_work(w: dict) -> str:
    authors = ", ".join(w.get("authors") or [])
    if len(authors) > 60:
        authors = authors[:60] + "…"
    return (
        f"{w.get('doi', '-')} | {w.get('title', '')} | {w.get('source', '')} | "
        f"{w.get('year', '-')} | 被引{w.get('citations', '-')} | {authors}"
    )


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0

    cmd = argv[0]

    if cmd == "--search":
        query = argv[1] if len(argv) > 1 else ""
        rows = argv[2] if len(argv) > 2 else 10
        resp = search(query, rows)
        if not resp.get("ok"):
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            return 1
        print(f"OK total={resp.get('total')} returned={len(resp.get('results', []))}")
        for w in resp.get("results", []):
            print(_format_work(w))
        return 0

    if cmd == "--doi":
        doi = argv[1] if len(argv) > 1 else ""
        print(json.dumps(resolve(doi), ensure_ascii=False, indent=2))
        return 0

    if cmd == "--oa":
        doi = argv[1] if len(argv) > 1 else ""
        print(json.dumps(check_oa(doi), ensure_ascii=False, indent=2))
        return 0

    if cmd == "--full":
        doi = argv[1] if len(argv) > 1 else ""
        print(json.dumps(resolve_with_oa(doi), ensure_ascii=False, indent=2))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
