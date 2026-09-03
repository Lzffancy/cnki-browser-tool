# CNKI 本地研究助手（cnki-browser-tool）

一个「拟人化访问」的本地知网下载辅助工具：Chrome MV3 扩展负责在用户**已登录的真实浏览器页面**上执行读取、导航和点击下载，本机 Python 服务作为 Agent Tool 网关转发指令。全程不导出 Cookie、不直连知网接口、不使用无头浏览器、不绕过验证码或权限控制——所有操作都发生在用户可见、可随时中断的浏览器标签里。

## 为什么这样设计

知网对自动化抓取和接口直连有严格的风控与合规要求。这个项目刻意放弃「后端 requests / 无头浏览器 / Cookie 导出」的常见抓取思路，转而让 Agent 像人一样：打开页面、填检索框、点原生按钮、等浏览器自己完成下载。这样做的代价是速度更慢、需要用户保持登录态，但换来的是完全合规、可解释、可随时人工介入。

## 架构

```
Agent / Tool 调用
      │  HTTP POST /v1/call（推荐，curl）或 MCP stdio
      ▼
backend/bridge_server.py   本机桥接服务；扩展侧的 HTTP 长轮询固定跑在后台线程，仅监听 127.0.0.1
      │  长连接 / 消息转发
      ▼
extension/service-worker.js   MV3 Service Worker，批次状态持久化在 chrome.storage.local
      │  chrome.scripting / chrome.tabs / chrome.downloads
      ▼
extension/content/cnki-page.js   注入到 CNKI 页面的内容脚本，读取 DOM、模拟点击
      ▼
用户已登录的 Chrome 标签页（kns.cnki.net）
```

- **backend/bridge_server.py**：本机桥接服务，把 Agent 的 Tool 调用转发给扩展，是唯一暴露给 Agent 的入口。支持两种模式：`--mode http`（默认，裸 HTTP，curl 直接调、零第三方依赖、token 与往返开销最低，推荐）和 `--mode mcp`（标准 MCP stdio server，21 个动作注册为具名 Tool，JSON Schema 校验参数，供希望原生发现工具、带权限 UI 的 host）。两种模式下，扩展侧看到的都是同一套 HTTP 长轮询端点——Chrome MV3 Service Worker 没有 `listen()` 能力，这段传输方式不受 Agent 侧协议选型影响。`GET /health` 返回扩展连接状态（`extension.connected`）供一键自检。
- **extension/**：Chrome MV3 扩展本体。
  - `service-worker.js`：调度中心，处理会话状态、检索、批量下载队列，用 `chrome.alarms` + `chrome.storage.local` 解决 Service Worker 休眠导致的批次中断问题；单篇/批次里"点击后等待下载开始"这一步同样用 `chrome.downloads.onCreated` 真实事件 + `chrome.alarms` 兜底超时异步完成，不用裸计时器阻塞等待。
  - `content/cnki-page.js`：注入到知网页面的内容脚本，负责解析检索结果、点击详情页下载按钮等页面级操作。
  - `popup/`：扩展弹窗界面，用于查看状态和手动触发操作。
- **cnki_client.py**：Agent 侧内置客户端库（inner tool），纯标准库封装 `/v1/call` 调用、结果解析、批量下载与 PDF 文本提取。Agent 优先 `import cnki_client` 复用，而不是每次手搓 urllib 或另写 `_*.py` 临时脚本；它是便利层、不是强制 API，覆盖不了的任务仍可自行写脚本。
- **TOOL_REFERENCE.md**：完整的 Tool 调用说明（每个 action 的参数、返回值、限制条件），Agent 接入前必读。

## 已实现能力

- 读取当前 Chrome 活动标签与已打开的 CNKI 标签（`session.status`）
- 打开检索页 / 提交检索（`session.open_search` / `search.submit`）
- 解析检索结果列表，支持按被引数排序（`search.results` / `search.sort`）
- 切换一框式检索字段（主题/作者/篇名/全文等 16 项）（`search.set_field`）
- 切换文献库（学术期刊/学位论文/博士/硕士/图书/会议等 11 项）（`search.set_library`）
- 翻页（指定页码或上一页/下一页）（`search.turn_page`）
- 读取 / 应用左侧筛选面板（年度/文献类型/研究层次/来源类别/学科/机构/基金等）（`search.get_filters` / `search.apply_filter`）
- 单篇详情页 PDF 下载
- 批量 PDF 下载，支持持久化续跑、下载状态实时查询（`batch.start_pdf_download` / `batch.get_status` / `batch.resume_pdf_download`）
- 基于 `chrome.downloads.search()` 的实时下载历史查询（`download.recent`）
- 组合流程编排（`flow.run`）：一次调用串起多步基础动作，失败即停返回逐步明细
- 一键自检（`GET /health` 返回 `extension.connected`）与登录状态检测（`session.status` 的 `login` 字段）

## 快速开始（一键安装）

**Windows**：双击 `setup.bat`。
**macOS / Linux**：运行 `bash setup.sh`。

脚本会自动完成：定位 Python → 创建 `backend/.venv` → 安装依赖 → 后台启动桥接服务（http 模式，监听 `http://127.0.0.1:8765`）→ `curl /health` 自检。

脚本跑完后，剩下两步**必须人工完成**（Agent 无法代劳，这是拟人化架构的硬约束）：

1. Chrome 打开 `chrome://extensions` → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择 `extension/` 目录。
2. 登录知网（`https://kns.cnki.net`），并保持一个 CNKI 标签页打开。

然后自查一条命令：

```bash
curl -s http://127.0.0.1:8765/health
```

看到 `"extension": { "connected": true }` 即就绪（加载扩展后约 30 秒内会变成 `true`）。`session.status` 还会返回 `login` 字段，帮你确认知网是否已登录。

### 不想用脚本时，手动启动

```bash
# HTTP 模式（推荐，curl 直接调，无需任何第三方依赖）
python backend/bridge_server.py

# 或 MCP 模式（供希望原生发现工具、带权限 UI 的 Agent host）
python -m venv backend/.venv && backend/.venv/Scripts/pip install -r backend/requirements.txt
backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp
```

## Agent 接入（粘贴即用）

直接把下面这段贴给 WorkBuddy / dsh trace 等 Agent 客户端，它就能用本地工具了：

```text
本机已运行一个论文研究工具集，包含两个平级的数据源，按需选择或都查。

一、CNKI（中文文献）—— 走本机桥接服务，拟人化浏览器检索 + PDF 下载
（不导出 Cookie、不直连接口、不绕过验证码）。

- 服务地址：http://127.0.0.1:8765
- 自检：curl -s http://127.0.0.1:8765/health  → 看 extension.connected 是否为 true
- 调用：curl -s -X POST http://127.0.0.1:8765/v1/call -H "Content-Type: application/json" \
        -d '{"action":"<工具名>","payload":{...},"timeoutSeconds":45}'
- 可用工具名：见 http://127.0.0.1:8765/health 的 allowedActions，或读仓库 TOOL_REFERENCE.md
- 常用工具：session.status（看标签+登录态）、search.submit（检索）、search.set_library（切文献库）、
  search.results（读结果）、search.sort（排序）、batch.start_pdf_download（批量下载）、batch.get_status
- 组合流程：用 flow.run 一次完成多步（如 开检索页→切硕士库→检索→按被引排序→读结果），
  steps 传基础动作数组，某步失败即停并返回逐步明细
- 约束：只驱动真实页面原生交互；遇登录/验证码/权限提示就停下让用户处理；下载 URL 只接受详情页地址
- 复用：仓库根目录的 `cnki_client.py` 是现成的客户端库，`from cnki_client import call, extract_results, start_batch, batch_status` 即可用；
  优先复用它解析结果、批量下载，不要每次另写 `_*.py`。它覆盖不了的特殊需求，才允许你现场写一次性脚本

二、Crossref/DOI（国际文献）—— 直连官方开放 API，零浏览器、零 VPN、零 Cookie，
检索 + 元数据 + 全球被引 + OA 免费版定位。

- 用途：英文关键词、国际期刊/会议论文；输入 DOI 拉元数据、查全球被引、找免费 PDF
- 复用：仓库根目录的 `crossref_client.py` 是现成的客户端库，
  `from crossref_client import search, resolve, check_oa, resolve_with_oa` 即可用
- 说明：真正付费墙的论文，check_oa 能先判有没有免费合法版本（有则直接给 PDF 链接）；
  没有免费版的，才需要用户回到学校 VPN 的浏览器贴 DOI 手动下全文

判断规则：用户明确指定语言/来源 → 走对应源；没指定 → 两个源都查，结果按来源合并。
英文/DOI 场景优先 Crossref，中文场景优先 CNKI。两个源的输出字段已基本对齐
（title/authors/source/year/citations/doi），下游无需感知数据源差异。
```

## 安全边界

- 服务仅监听 `127.0.0.1`，不对外暴露。
- 不读取、不导出、不存储 CNKI 登录 Cookie。
- 不直接调用知网检索或下载接口，只驱动真实页面上的原生交互。
- 不使用无头浏览器，不规避验证码或访问频率限制。

## 赞助

如果这个工具帮你省了事，欢迎请作者喝杯咖啡。

仓库主页右上角的 **Sponsor** 按钮提供微信赞助入口。收款码图片放在 `sponsor/wechat.png`，配置见 `.github/FUNDING.yml`。
