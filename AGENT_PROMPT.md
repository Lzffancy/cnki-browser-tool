# CNKI / Crossref 本地文献工具 — Agent 接入提示词

> 一份可直接复用、粘贴即用的接入说明。整理自仓库 README，已补充本机安装信息与工具速查表。

---

## 〇、本机环境速查

| 项 | 值 |
|---|---|
| 安装路径 | `<本仓库所在目录>`（各脚本已用 `__file__` 相对推导，clone 到任何位置都能跑） |
| 个人目录配置 | 复制 `local_config.example.json` 为 `local_config.json` 并填入你的论文目录 / 浏览器下载目录（该含个人信息的文件已加入 .gitignore，不会入库） |
| 桥接服务地址 | `http://127.0.0.1:8765`（仅监听本机） |
| 启动服务 | 双击 `start-server.bat`（首次先跑 `setup.bat`） |
| 停止服务 | 关闭「cnki-local-bridge」窗口，或 `taskkill /F /FI "WINDOWTITLE eq cnki-local-bridge*"` |
| 自检 | `curl -s http://127.0.0.1:8765/health` → 看 `extension.connected` 是否为 `true` |
| 客户端库 | `cnki_client.py`、`crossref_client.py`（均纯标准库，可直接 import） |

---

## 一、粘贴即用（整段复制给 WorkBuddy / dsh trace 等 Agent 客户端）

```
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

---

## 二、工具速查表

### CNKI（桥接服务，21 个 action）

| 类别 | action | 说明 |
|---|---|---|
| 会话 | `session.status` | 查看活动标签 + 现有 CNKI 标签 + 登录态（`login` 字段） |
| 会话 | `session.open_search` | 打开/复用检索标签，可顺带检索 |
| 检索 | `search.submit` | 一框式检索（填词 + 点原生按钮） |
| 检索 | `search.set_field` | 切检索字段（主题/作者/篇名/全文等 16 项） |
| 检索 | `search.set_library` | 切文献库（期刊/学位论文/博士/硕士/图书/会议等 11 项） |
| 检索 | `search.advanced_submit` | 高级检索（多条件 AND/OR/NOT，含导师/学位授予单位等字段） |
| 结果 | `search.results` | 读结果列表 |
| 结果 | `search.sort` | 按被引/下载/相关度/发表时间/综合排序 |
| 结果 | `search.turn_page` | 翻页 |
| 筛选 | `search.get_filters` | 读左侧筛选面板（年度/类型/层次/机构/基金等） |
| 筛选 | `search.apply_filter` | 勾选并应用筛选值 |
| 页面 | `page.navigate` | 打开指定 CNKI 页面 |
| 页面 | `page.snapshot` | 读页面标题/URL/加载态/可见文本 |
| 页面 | `page.dom` | 读页面 HTML（诊断用） |
| 下载 | `article.download_options` | 读详情页下载入口（不触发下载） |
| 下载 | `article.click_pdf_download` | 点击详情页 PDF 下载按钮 |
| 下载 | `batch.start_pdf_download` | 批量下载（1-10 篇详情页 URL） |
| 下载 | `batch.get_status` | 查批次进度 |
| 下载 | `batch.resume_pdf_download` | 处理登录/验证码后续跑 |
| 下载 | `download.recent` | 查 Chrome 下载历史 |
| 编排 | `flow.run` | 组合多步基础动作，失败即停返回明细 |

### Crossref / DOI（客户端库，4 个函数）

| 函数 | 说明 |
|---|---|
| `search(query, ...)` | 英文关键词检索 |
| `resolve(doi)` | 按 DOI 拉元数据 |
| `check_oa(doi)` | 判是否有免费合法版本（OA） |
| `resolve_with_oa(doi)` | 拉元数据 + 定位免费 PDF |

---

## 三、前置条件（首次一次性，必须人工完成）

1. Chrome 打开 `chrome://extensions` → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择本仓库下的 `extension` 目录
2. 登录知网 `https://kns.cnki.net`，并保持一个 CNKI 标签页打开
3. 验证：`curl -s http://127.0.0.1:8765/health`，看到 `"extension": {"connected": true}` 即就绪（加载扩展后约 30 秒内变 true）

## 四、两种服务模式

- **HTTP 模式（默认推荐）**：`python backend/bridge_server.py`，裸 HTTP，curl 直调，零第三方依赖
- **MCP 模式**：`backend/.venv/Scripts/python.exe backend/bridge_server.py --mode mcp`，标准 MCP stdio，21 个 action 注册为具名 Tool，供带权限 UI 的 host 原生发现
