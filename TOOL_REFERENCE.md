# CNKI 本地研究助手：Tool 使用说明

本文件定义本机 Agent 可调用的受限 Tool。`backend/bridge_server.py` 支持两种调用方式，底层是同一个 `CommandBroker` 队列，只是 Agent 侧的协议不同：

**方式一：MCP（推荐，`--mode mcp`）**

```bash
backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp
```

以标准 MCP stdio server 运行，14 个动作逐一注册为具名 Tool（名字与下文一致，如 `search.sort`、`batch.start_pdf_download`），参数用 JSON Schema（枚举、长度、范围）在协议层校验，支持 Tool 发现（`list_tools`），不用再手搓 curl 拼 JSON。同一进程会在后台线程原样启一份 HTTP 服务专门伺候 Chrome 扩展的长轮询（见方式二），因为 MV3 Service Worker 没有 `listen()` 能力，这段传输方式不受协议选型影响。

**方式二：裸 HTTP（`--mode http`，默认，向后兼容 / 手工调试）**

```text
POST http://127.0.0.1:8765/v1/call
Content-Type: application/json

{
  "action": "Tool 名称",
  "payload": { },
  "timeoutSeconds": 45
}
```

响应格式：

```json
{
  "ok": true,
  "commandId": "UUID",
  "data": {}
}
```

不依赖 `mcp` 包，可用系统自带 Python 直接跑，适合 curl 手工调试；扩展侧的轮询端点（`/v1/extension/next-command`、`/v1/extension/command-result`）在两种模式下行为完全一致。

所有 Tool 均只会通过已登录 Chrome 内的 CNKI 页面执行。它们不会导出 Cookie、调用 CNKI 下载接口、启动无头浏览器、绕过验证码或权限控制。

**参数命名对照**：下文各 Tool 的参数名是 HTTP `payload` 里的字段名（驼峰式，如 `sortBy`、`articleUrls`、`intervalSeconds`、`maxChars`）；MCP 模式下同一参数按 Python 习惯改成蛇形命名（`sort_by`、`article_urls`、`interval_seconds`、`max_chars`），语义、默认值、取值范围完全一致，只是命名风格不同。

## 推荐调用流程

```text
session.status
  → session.open_search / search.submit
  → search.results
  → 按业务规则筛选 articleUrl
  → batch.start_pdf_download
  → batch.get_status
  → batch.resume_pdf_download（仅在用户处理了页面阻塞后）
```

---

## 1. 会话与页面 Tool

### `session.status`

**用途**：查看 Chrome 当前活动标签，以及浏览器里已有的 CNKI 标签；不读取论文正文。

**参数**：无。

**返回重点**：

- `activeTab`：当前标签 ID、标题、URL、是否为 CNKI；
- `cnkiTabs`：已打开 CNKI 标签列表；
- `canOpenSearch`：是否允许插件新建 CNKI 检索标签。

**适用场景**：Agent 在调用页面读取或下载前，先判断浏览器是否已有可复用的 CNKI 页面。

---

### `session.open_search`

**用途**：创建或复用 CNKI 检索标签。传入关键词时，会在该页面的检索框内输入文本并点击页面原有检索按钮。

**参数**：

```json
{
  "query": "人工智能"
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `query` | 否 | 1-100 字符。未传入时仅打开检索页；传入时立即执行检索。 |

**页面行为**：

1. 如果当前活动标签是 CNKI，则复用它；否则创建并聚焦 CNKI `kns8s` 检索页；
2. 使用页面输入控件填入关键词；
3. 触发输入、变更事件；
4. 点击 CNKI 页面已有的检索按钮；
5. 等待结果表已渲染。

**注意**：这是后续自动检索的入口，用户无需手动先切换到检索页。

---

### `page.snapshot`

**用途**：读取当前活动 CNKI 页的轻量页面快照。

**参数**：无。

**返回重点**：`url`、`title`、`readyState`、`textPreview`（最多 800 字）、`textLength`、`capturedAt`。

**前置条件**：当前活动标签必须是 CNKI 页面。此 Tool 不会新建标签，以避免误读用户未预期的页面。

---

### `page.dom`

**用途**：读取当前活动 CNKI 页的 HTML，用于开发页面适配、排障或字段识别。

**参数**：

```json
{
  "maxChars": 30000
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `maxChars` | 否 | 1000-300000；默认 120000。 |

**返回重点**：`html`、`htmlLength`、`returnedLength`、`truncated`、`url`、`title`。

**限制**：不应将此 Tool 用于读取 Cookie、Local Storage 或执行任意页面脚本；仅获取当前 DOM 字符串。

---

### `page.navigate`

**用途**：在当前 CNKI 标签或新建 CNKI 标签中打开一个指定的 CNKI 页面。

**参数**：

```json
{
  "url": "https://kns.cnki.net/kcms2/article/abstract?..."
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `url` | 是 | 仅接受 `https://cnki.net/*` 或 `https://*.cnki.net/*`。 |

**适用场景**：打开论文详情页或指定检索页。禁止传入下载直链、非 CNKI 地址或任意第三方站点。

---

## 2. 检索与结果 Tool

### `search.submit`

**用途**：自动检索指定关键词。

**参数**：

```json
{
  "query": "人工智能"
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `query` | 是 | 1-100 字符。 |

**页面行为**：自动创建/复用 CNKI 标签，打开 `kns8s` 检索页，再用页面的正常输入和点击操作检索。

**返回重点**：提交状态、所在 `tabId`、以及初始 `results`。

**适用场景**：默认优先用它，而不是要求用户自己先打开检索页面。

---

### `search.sort`

**用途**：点击 CNKI 检索结果页面自身的排序控件，并等待列表刷新。

**参数**：

```json
{
  "sortBy": "citations",
  "limit": 10
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `sortBy` | 是 | `citations`（被引）、`downloads`（下载）、`relevance`（相关度）、`publishedAt`（发表时间）、`comprehensive`（综合）。 |
| `limit` | 否 | 1-50，指定返回多少条刷新后的结果。 |

**页面行为**：只点击结果页现有排序项，例如 `citations` 对应“被引”。不拼接 CNKI 排序参数、不调用排序接口。排序结果由 CNKI 页面本身生成。

**使用建议**：

```text
search.submit("人工智能")
→ search.sort({"sortBy":"citations","limit":10})
→ batch.start_pdf_download(返回结果中的 articleUrl)
```

---

### `search.results`

**用途**：读取当前活动 CNKI 检索结果页中已经显示的论文列表。

**参数**：

```json
{
  "limit": 20
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `limit` | 否 | 1-50，默认 20。 |

**每篇论文返回字段**：

| 字段 | 含义 |
|---|---|
| `index` | 当前结果页序号 |
| `title` | 论文标题 |
| `articleUrl` | 论文详情页链接，批量下载唯一可用的 URL 类型 |
| `authors` | 作者列表 |
| `source` | 来源/刊物/学校等 |
| `publishedAt` | 页面展示的发表时间 |
| `resourceType` | 文献类型 |
| `citations` | 被引量文本 |
| `downloads` | 下载量文本 |
| `hasNormalDownloadEntry` | 页面列表是否显示正常下载入口 |

**前置条件**：当前活动标签必须是 `kns8s` 结果页。一般应紧跟在 `search.submit` 后调用。

---

## 3. 单篇下载 Tool

### `article.download_options`

**用途**：从当前论文详情页读取页面上可见的下载入口，不下载。

**参数**：无。

**前置条件**：当前活动标签是论文详情页。

**适用场景**：下载前确认该论文是否有 PDF 入口，以及页面是否出现登录、权限、验证码或改版提示。

---

### `article.click_pdf_download`

**用途**：点击当前论文详情页中检测到的页面原生 PDF 下载按钮，并等待 Chrome 创建下载任务。

**参数**：无。

**返回重点**：是否识别并点击按钮、是否创建 Chrome 下载任务、下载任务 ID 和初始状态。

**前置条件**：

- 当前活动标签为已加载的 CNKI 论文详情页；
- 用户已在该 Chrome Profile 中完成正常登录；
- 页面本身显示 PDF 下载入口且账号有权限。

**暂停边界**：若页面提示登录、验证码、权限不足、下载弹窗要求人工确认，Tool 只返回状态，不处理或绕过。

---

## 4. 批量 PDF 下载 Tool

### `batch.start_pdf_download`

**用途**：针对已经筛选好的论文详情页 URL，顺序执行“详情页 → 页面 PDF 下载按钮 → 等待浏览器完成下载”。

**参数**：

```json
{
  "articleUrls": [
    "https://kns.cnki.net/kcms2/article/abstract?...",
    "https://kns.cnki.net/kcms2/article/abstract?..."
  ],
  "intervalSeconds": 5
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `articleUrls` | 是 | 1-10 个、去重后的 CNKI 论文详情页 URL；不能是下载接口地址。 |
| `intervalSeconds` | 否 | 3-30 秒；默认 5 秒。 |

**固定行为**：

```text
打开详情页
→ 等待页面加载
→ 读取标题
→ 识别 PDF 下载按钮
→ 正常点击
→ 由 Chrome 下载完成事件确认落盘
→ 通过持久化批次状态与浏览器 alarm 调度下一篇

> MV3 Service Worker 会休眠，批次不会依赖长时间循环；Chrome 重启/扩展被唤醒后会从未完成项续跑。Chrome 的 alarm 最小调度间隔约 30 秒，因此 `intervalSeconds` 是下限意图，实际下一篇可能更保守。
```

**批次状态**：

| 状态 | 含义 |
|---|---|
| `running` | 正在执行 |
| `completed` | 全部文件下载完成 |
| `paused` | 遇到阻塞，需用户处理 |

**自动暂停条件**：登录失效、权限不足、验证码、PDF 按钮不可识别、未创建下载任务、下载中断、页面结构变化、等待超时。

---

### `batch.get_status`

**用途**：查看当前或最近批次的执行状态。

**参数**：无。

**返回重点**：`batchId`、`state`、`currentIndex`、每篇的 `title`、`articleUrl`、下载信息、错误信息、`pauseReason`。

**建议使用时机**：每次启动批次后轮询；如果返回 `paused`，先由用户在浏览器解决页面提示。

---

### `batch.resume_pdf_download`

**用途**：从暂停项恢复当前批次。

**参数**：无。

**前置条件**：

1. `batch.get_status` 显示 `state: "paused"`；
2. 用户已在当前 Chrome 页面完成登录、验证码或权限确认；
3. 当前下载标签仍存在且是 CNKI 页面。

**限制**：不重试已完成项目；不会跳过当前出错论文；继续后仍遇到异常会再次暂停。

---

### `download.recent`

**用途**：直接查询 Chrome 下载历史（`chrome.downloads.search`），返回最近的下载任务。

**参数**：`limit`（可选，1-50，默认 20）。

**返回重点**：下载任务 ID、文件名、状态、错误原因、下载 URL、起止时间、字节数。

**限制**：这不是磁盘扫描工具，只反映 Chrome 下载历史中的记录（文件被用户手动删除或从历史中清除后不会再出现）。

**版本说明（0.7.1 修复）**：0.7.0 版本此接口读取的是 Service Worker 内存里的 `recentDownloads` 缓存；MV3 Worker 空闲时会被 Chrome 卸载重启，重启后该内存数组清空为空，导致明明下载已成功却查询到空列表（现象：插件执行成功、`batch.get_status` 显示 completed、磁盘上文件也存在，但 `download.recent` 返回空）。0.7.1 改为直接查询 `chrome.downloads.search`，结果来自 Chrome 自身的下载历史，不受 Worker 重启影响，与浏览器"近期的下载记录"面板一致。

---

## 5. 调用约束

1. 一期批量最多 10 篇，必须顺序下载；
2. Agent 只能使用上述命名 Tool，不可调用任意 JavaScript、CSS Selector、下载 URL 或浏览器 Cookie；
3. 任何需要人类校验的登录、验证码、付费和权限提示，均由用户在 Chrome 中自行处理；
4. 所有下载文件由 Chrome 的默认下载设置落盘；后续将单独增加“下载完成后的本地归档和元数据入库”模块。

---

## 6. MCP 模式环境准备

`--mode mcp` 需要 `mcp` 包（本项目锁定 `mcp<2`，因为 2.x 把 `FastMCP` 改名成 `MCPServer` 且 API 结构变了，社区文档还没跟上）；`--mode http` 不需要任何第三方依赖，可直接用系统 Python 跑。

```bash
# 只需一次：为 MCP 模式建一个独立虚拟环境
python -m venv backend/.venv
backend/.venv/Scripts/pip install "mcp<2"

# 之后每次启动 MCP 模式
backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp
```

把它注册进 Agent host 的 MCP 配置（例如 WorkBuddy 的 `~/.workbuddy/mcp.json`）后，由 host 负责拉起这个进程；不需要手动跑上面这条命令。

---

## 7. 版本变更记录

- **0.7.1**：`download.recent` 从读取会被 MV3 Worker 重启清空的内存缓存，改为直接查询 `chrome.downloads.search`（见上文 `download.recent` 小节）。
- **0.7.2**：删除 `service-worker.js` 中未被调用的死代码 `waitForDownloadCompletion`（及配套常量 `MAX_DOWNLOAD_COMPLETE_WAIT_MS`），不影响任何已有行为。
- **0.2（backend）**：`bridge_server.py` 新增 `--mode mcp`，把 14 个动作注册为标准 MCP Tool（JSON Schema 校验 + Tool 发现），`--mode http` 保持原有行为不变；扩展侧协议与端点均未改动。

