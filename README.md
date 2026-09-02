# CNKI 本地研究助手（cnki-browser-tool）

一个「拟人化访问」的本地知网下载辅助工具：Chrome MV3 扩展负责在用户**已登录的真实浏览器页面**上执行读取、导航和点击下载，本机 Python 服务作为 Agent Tool 网关转发指令。全程不导出 Cookie、不直连知网接口、不使用无头浏览器、不绕过验证码或权限控制——所有操作都发生在用户可见、可随时中断的浏览器标签里。

## 为什么这样设计

知网对自动化抓取和接口直连有严格的风控与合规要求。这个项目刻意放弃「后端 requests / 无头浏览器 / Cookie 导出」的常见抓取思路，转而让 Agent 像人一样：打开页面、填检索框、点原生按钮、等浏览器自己完成下载。这样做的代价是速度更慢、需要用户保持登录态，但换来的是完全合规、可解释、可随时人工介入。

## 架构

```
Agent / Tool 调用
      │  MCP stdio（推荐）或 HTTP POST /v1/call（调试）
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

- **backend/bridge_server.py**：本机桥接服务，把 Agent 的 Tool 调用转发给扩展，是唯一暴露给 Agent 的入口。支持两种模式：`--mode mcp`（标准 MCP stdio server，19 个动作注册为具名 Tool，JSON Schema 校验参数）和 `--mode http`（默认，裸 HTTP，兼容 curl 手工调试）。两种模式下，扩展侧看到的都是同一套 HTTP 长轮询端点——Chrome MV3 Service Worker 没有 `listen()` 能力，这段传输方式不受 Agent 侧协议选型影响。
- **extension/**：Chrome MV3 扩展本体。
  - `service-worker.js`：调度中心，处理会话状态、检索、批量下载队列，用 `chrome.alarms` + `chrome.storage.local` 解决 Service Worker 休眠导致的批次中断问题；单篇/批次里"点击后等待下载开始"这一步同样用 `chrome.downloads.onCreated` 真实事件 + `chrome.alarms` 兜底超时异步完成，不用裸计时器阻塞等待。
  - `content/cnki-page.js`：注入到知网页面的内容脚本，负责解析检索结果、点击详情页下载按钮等页面级操作。
  - `popup/`：扩展弹窗界面，用于查看状态和手动触发操作。
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

## 快速开始

1. Chrome 打开 `chrome://extensions`，开启开发者模式，「加载已解压的扩展程序」选择 `extension/` 目录。
2. 启动本机桥接服务：
   - **MCP 模式（推荐给 Agent host，如 WorkBuddy）**：
     ```bash
     python -m venv backend/.venv          # 只需一次
     backend/.venv/Scripts/pip install "mcp<2"
     backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp
     ```
     或直接把它注册进 Agent host 的 MCP 配置，由 host 自动拉起（无需手动执行上面这条命令）。
   - **HTTP 模式（手工调试）**：
     ```bash
     python backend/bridge_server.py
     ```
     不依赖 `mcp` 包，可用系统自带 Python 直接跑。
3. Agent 端按 `TOOL_REFERENCE.md` 的说明调用 Tool（MCP 模式走标准 `list_tools`/`call_tool`；HTTP 模式向 `http://127.0.0.1:8765/v1/call` 发起调用）。
4. 保持 Chrome 内知网账号已登录；后续所有操作都在该浏览器可见标签中完成。

## 安全边界

- 服务仅监听 `127.0.0.1`，不对外暴露。
- 不读取、不导出、不存储 CNKI 登录 Cookie。
- 不直接调用知网检索或下载接口，只驱动真实页面上的原生交互。
- 不使用无头浏览器，不规避验证码或访问频率限制。
