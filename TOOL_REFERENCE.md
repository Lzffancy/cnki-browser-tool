# CNKI 本地研究助手：Tool 使用说明

本文件定义本机 Agent 可调用的受限 Tool。`backend/bridge_server.py` 支持两种调用方式，底层是同一个 `CommandBroker` 队列，只是 Agent 侧的协议不同：

**方式一：MCP（推荐，`--mode mcp`）**

```bash
backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp
```

以标准 MCP stdio server 运行，19 个动作逐一注册为具名 Tool（名字与下文一致，如 `search.sort`、`batch.start_pdf_download`），参数用 JSON Schema（枚举、长度、范围）在协议层校验，支持 Tool 发现（`list_tools`），不用再手搓 curl 拼 JSON。同一进程会在后台线程原样启一份 HTTP 服务专门伺候 Chrome 扩展的长轮询（见方式二），因为 MV3 Service Worker 没有 `listen()` 能力，这段传输方式不受协议选型影响。

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
  → session.open_search / search.submit          （打开检索页 / 提交关键词）
  → search.set_field                            （可选：切检索字段，如作者 AU）
  → search.set_library                          （可选：切文献库，如学位论文 dissertation）
  → search.get_filters                          （可选：发现年度/文献类型等筛选项）
  → search.apply_filter                         （可选：应用筛选，如年度 2023-2025）
  → search.results / search.sort / search.turn_page
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

### `search.set_field`

**用途**：切换一框式检索框的检索字段（默认是「主题」）。切换后，再调用 `search.submit` 就会在所选字段下检索。

**参数**：

```json
{
  "field": "AU"
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `field` | 是 | 16 个字段代码之一，见下表。 |

**字段代码对照表**（取自页面下拉 `li[data-val]` 与隐藏域 `#selectfield`）：

| 代码 | 中文 | 代码 | 中文 |
|---|---|---|---|
| `SU` | 主题 | `FU` | 基金 |
| `TKA` | 篇关摘 | `AB` | 摘要 |
| `KY` | 关键词 | `CO` | 小标题 |
| `TI` | 篇名 | `RF` | 参考文献 |
| `FT` | 全文 | `CLC` | 分类号 |
| `AU` | 作者 | `LY` | 文献来源 |
| `FI` | 第一作者 | `DOI` | DOI |
| `RP` | 通讯作者 | | |
| `AF` | 作者单位 | | |

**典型用法**：`search.set_field({"field":"AU"})` 后 `search.submit({"query":"李牧南"})` 即在「作者」字段检索李牧南。

**注意**：这是**单字段**检索。要「作者 AND 主题」这种多条件复合，单字段框放不下，需用知网专业检索（`AdvSearch?type=expert`，见本文末「待办」），当前版本暂未封装。

---

### `search.set_library`

**用途**：切换文献库（顶部「文献类型」切换），把检索范围限定到学术期刊、学位论文、博士、硕士等特定库。这是「只看学位论文」最直接的入口。

**参数**：

```json
{
  "library": "master"
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `library` | 是 | 11 个库代码之一，见下表。 |

**库代码对照表**（取自页面 `a[name=classify][classid]`）：

| 代码 | 中文 | classid |
|---|---|---|
| `journal` | 学术期刊 | `YSTT4HG0` |
| `dissertation` | 学位论文（博士+硕士合计） | `LSTPFY1C` |
| `doctor` | 博士 | `RMJLXHZ3` |
| `master` | 硕士 | `JQIRZIYA` |
| `book` | 图书 | `EMRPGLPA` |
| `conference` | 会议 | `JUP3MUPD` |
| `newspaper` | 报纸 | `MPMFIG1A` |
| `almanac` | 年鉴 | `HHCPM1F8` |
| `patent` | 专利 | `VUDIXAIY` |
| `standard` | 标准 | `WQ0UVIAA` |
| `achievement` | 成果 | `BLZOG7CK` |

**典型用法**：先 `search.submit({"query":"人工智能"})` 得到结果，再 `search.set_library({"library":"dissertation"})` 把范围收敛到学位论文；或先切库再检索。页面会按所选库重新加载结果。

**关键提示**：知网的「导师/指导教师」字段**只存在于学位论文库**（博士 CDFD、硕士 CMFD）。所以「某人作为指导老师的论文」必须先限定 `dissertation`/`doctor`/`master`，再去详情页核对导师字段。

---

### `search.turn_page`

**用途**：翻页到指定页码，或上一页/下一页，并等待结果表刷新后返回新一页结果。

**参数**（`page` 与 `direction` 二选一）：

```json
{ "page": 2 }
```

或

```json
{ "direction": "next" }
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `page` | 否 | >=1 的目标页码；仅当该页码在当前可见页码范围内（底部翻页条）时可点。 |
| `direction` | 否 | `next`（下一页）/ `prev`（上一页）。 |

**返回重点**：翻页方式、目标页码、当前页码、总页数，以及刷新后的 `results`。

**注意**：底部翻页条只渲染当前页附近的页码（如 1-8、9-16…），不在可见范围内的页码无法直接跳转，需用 `direction=next` 逐步翻。总页数从 `.countPageMark[data-pagenum]` 读取（如 300 页），总数从 `.pagerTitleCell em` 读取。

---

### `search.get_filters`

**用途**：读取左侧筛选面板的维度和可选值，用于 Agent 动态发现「年度」「文献类型」「研究层次」「来源类别」「学科」等维度的具体筛选项，再据此调用 `search.apply_filter`。

**参数**：

```json
{
  "groups": ["YE", "WXLX"]
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `groups` | 否 | 字符串或字符串数组，指定要**先展开再读取**的维度 groupid。不传则只读当前已展开的维度。 |

**返回重点**：每个维度的 `groupid`、`title`（中文名）、`folded`（是否折叠）、`items`（`value` + `text` + 计数）。

**维度 groupid 对照**（取自页面 `dl[groupid]`）：

| groupid | 中文 | 说明 |
|---|---|---|
| `YE` | 年度 | 默认折叠，需 `groups:["YE"]` 展开 |
| `WXLX` | 文献类型 | 默认折叠，需展开 |
| `YJCC` | 研究层次 | 默认折叠，需展开 |
| `LYBSM` | 来源类别 | 北大核心/CSSCI/AMI/CSCD… 通常已展开 |
| `CCL` | 学科 | 通常已展开 |
| `WXLY` | 文献来源 | 默认折叠 |
| `AFC` | 机构 | 默认折叠 |
| `FUC` | 基金 | 默认折叠 |
| `OA` | OA出版 | 默认折叠 |

**典型用法**：`search.get_filters({"groups":["YE"]})` 先拿到年度可选值（2024、2023…），再 `search.apply_filter({"group":"YE","values":["2023","2024","2025"]})` 限定年份。

---

### `search.apply_filter`

**用途**：勾选左侧筛选面板某维度的若干值并提交（相当于人工点 checkbox 再点「确定」），提交后等待结果表刷新。

**参数**：

```json
{
  "group": "YE",
  "values": ["2023", "2024", "2025"]
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `group` | 是 | 筛选维度 groupid（如 `YE`/`WXLX`/`YJCC`/`LYBSM`/`CCL`）。 |
| `values` | 是 | 字符串数组，要勾选的筛选值（需先通过 `search.get_filters` 确认真实 value）。 |

**页面行为**：展开目标维度 → 逐个勾选对应 checkbox → 点击「确定」（`a.btn-submit`，内部调用 `mutiSelectedGroup()`）→ 页面按筛选条件刷新结果。

**注意**：筛选值必须与 `search.get_filters` 返回的 `value` 完全一致（如年度是 `"2023"` 而非 `"2023年"`）。若某值不存在，会返回未找到该值的错误。

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

**实现说明（0.7.3+）**：点击后不再用裸计时器阻塞等待最长 30 秒——那样做会让 MV3 Service Worker 在等待期间可能被 Chrome 判定为空闲并终止，导致下载其实已经开始却永远没有结果回传（表现为桥接服务超时报错，即使 Chrome 里下载已经成功）。现在改为：点击后先做一次即时核实；如果没有立刻查到新下载，就把等待状态持久化，靠 `chrome.downloads.onCreated` 真实事件和 `chrome.alarms` 兜底超时来异步推进并把结果提交回桥接服务，两者都能在 Worker 被系统回收后重新唤醒继续处理。批次下载中“点击后等待下载开始”这一步用的是同一套机制。

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
- **0.7.3**：修复 `article.click_pdf_download`（及批次下载内“点击后等待下载开始”这一步）里长达 30 秒的裸 `while + setTimeout` 轮询：改为点击后先做一次即时核实，未命中则把等待状态持久化到 `chrome.storage.local`，由 `chrome.downloads.onCreated` 真实事件和 `chrome.alarms` 兜底超时异步推进结果，避免 MV3 Service Worker 在裸计时器等待期间被系统终止导致"下载其实成功了，但桥接服务只等到超时"。
- **0.7.4**：把 CNKI 标签定位从「必须是当前活动标签」放宽为「存在任意可用 CNKI 标签即可」。`service-worker.js` 新增 `getPreferredCnkiTab(preferredTabId)`，按「指定 tabId > 当前活动标签（若是 CNKI）> 任意已打开的 CNKI 标签」三级回退，替换原 `getActiveCnkiTab` 的全部调用；批次运行本就复用固定 `tabId`，这里顺带为批次增加该 tab 被关闭后的自动重定位兜底。效果：下载/检索期间切到别的标签或窗口不再中断，只有电脑睡眠/锁屏、或所有 CNKI 标签被关闭时才会暂停。
- **0.2（backend）**：`bridge_server.py` 新增 `--mode mcp`，把 14 个动作注册为标准 MCP Tool（JSON Schema 校验 + Tool 发现），`--mode http` 保持原有行为不变；扩展侧协议与端点均未改动。
- **0.7.5**：新增 5 个检索条件控制 Tool——`search.set_field`（切换检索字段，16 项）、`search.set_library`（切换文献库，11 项，含博士/硕士）、`search.turn_page`（翻页）、`search.get_filters`（读左侧筛选面板）、`search.apply_filter`（应用筛选，如年度区间）。`content/cnki-page.js` 的消息处理改为异步统一分发，以支持 facet 展开后的 AJAX 内容等待。动作总数由 14 增至 19。
- **0.2.1（backend）**：修复 Windows 下的"多实例幽灵监听"问题——`bridge_server.py` 用的 `ThreadingHTTPServer` 默认 `allow_reuse_address = 1`，这个设置在 POSIX 下只放宽 TIME_WAIT 状态复用，但在 Windows 上会让多个进程同时 bind 到同一个 `127.0.0.1:<port>` 而互不报错、互不知情，导致偶发/持续性的"等待插件响应超时"（每个进程有自己独立的内存 `CommandBroker`，扩展轮询和 Agent 调用可能落在不同进程上）。新增 `SingleInstanceHTTPServer` 子类显式关闭 `allow_reuse_address`，`main()` 里捕获 bind 时的 `OSError` 并给出清晰提示后退出，避免静默产生第二个幽灵监听者。
- **0.7.6**：新增 `search.advanced_submit`（AdvSearch 高级检索页，1-3 条件 AND/OR/NOT，字段含 TU导师/FTU第一导师/LY学位授予单位/XF学科专业名称，一框式检索没有这几个字段），动作总数 19→20。顺手修复检索框兼容性 bug：知网学位论文库翻页/切库后 `<input class="search-input">` 会丢失 `id="txt_search"`，`getSearchInput()` 原来要求 `#txt_search.search-input` 同时匹配 id+class 导致识别失败，改成只认 class。
- **0.7.7**：修复 `search.advanced_submit` 切库后可能落在错误 tab 的问题——AdvSearch 同一个 URL 下「高级检索/专业检索/作者发文检索/句子检索」共享 DOM，切换文献库后默认激活的 tab 不一定是"高级检索"（如学术期刊库 classid=YSTT4HG0 默认落在"专业检索"），此时字段下拉尚未按当前库初始化。调用前先探测 `li[name="gradeSearch"]` 是否 active，不是则先 click 切过去。
- **0.7.8**：修复 `search.advanced_submit` 的表单残留 bug——原实现只填 `conditions.length` 行，从不清空多出来的行；同一个标签页连续调用、且这次条件数比上次少时，上次残留的检索词会静默叠加生效（例如先提交 2 条件拿到结果，再在同一 tab 只传 1 条件想验证"去掉某限制后有多少条"，实际上第 2 行的旧值根本没清，结果会一样）。修复：调用时先清空 `rows.slice(conditions.length)` 里残留的输入框。**教训**：验证"改变条件后结果是否变化"这类假设时必须先 `page.navigate` 刷新到干净页面再提交，不要在已提交过的 tab 上连续调用对比，否则可能得出错误结论。

## 8. 已知限制与待办

- ~~多条件 AND/OR 复合检索~~：已在 0.7.6 通过 `search.advanced_submit` 实现（AdvSearch 高级检索表单，1-3 条件 AND/OR/NOT）。
- ~~导师字段归因~~：已确认「导师」是高级检索表单本身支持的字段（`TU`），学位论文库直接可用 `TU='某人'` 精确检索，不需要逐篇进详情页抓取。
- **跨页聚合**：`search.turn_page` 只能翻到可见页码，大批量场景需要「逐页读取并聚合」，尚未封装。

