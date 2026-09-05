# 更新日志

本项目所有值得记录的变更都写在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.9.0] - 2026-09-06

### Fixed — 桥接层命令队列被僵尸命令堵死

此前 `run_bridge_action()` 在等待插件响应超时后，只是抛错返回，**从不把命令移出队列**。
由于扩展每 30 秒才拉取一个命令，这些滞留命令会被逐个"补执行"，把后续命令全部挤到队尾，
造成持续性超时；而短超时重试又会不断产生新的滞留命令，形成自我恶化的死循环。
这也是此前"插件失明 / 页面内容取不到"等一系列假象的根本原因。

- 新增 `Command.abandoned` 字段与 `CommandBroker.abandon()`：超时即刻移出队列并标记完成
- 新增 `CommandBroker.purge_stale(120s)`：入队前清理遗留的陈旧命令
- 提示信息补充"扩展每 30s 才拉一次命令，超时建议设置 40s 以上"

### Added — CaptchaGate 验证码拦截状态机（服务端一等公民）

此前验证码只能靠客户端脚本每 60 秒轮询猜测，容易误判且无法协同。
改为服务端维护全局拦截状态，做到「服务自己知道被拦截、自己暂停、自己等人、解除后自动放行」。

- 扩展侧 `chrome.webNavigation` 监听：导航到验证页**秒级主动上报**（`manifest.json` 新增 `webNavigation` 权限）
- 服务端自动探活：拦截期间利用扩展 30s 固有轮询自动注入 `session.status` 探针，
  **连续 2 次检测页面干净即自动解除**（上报未跟上时也能自愈，双保险）
- 拦截期间**不下发任何真实命令**给扩展，避免加重风控
- `/v1/call` 支持 `waitForCaptchaSeconds`（上限 900s）：被拦截时服务端原地等待人工完成验证码，
  解除后自动继续执行，对调用方完全透明
- 新增接口：`GET /v1/captcha/status`、`POST /v1/captcha/wait`、
  `POST /v1/extension/captcha-report`、`POST /v1/captcha/clear`；`/health` 增加 `captcha` 字段
- MCP 新增 `captcha.status` / `captcha.wait` 两个 Tool
- 客户端 `cnki_client.py`：`call()` 支持 `wait_for_captcha_seconds`；新增 `CaptchaBlocked` 异常、
  `captcha_status()`、`wait_for_captcha()`

### Fixed — 验证码误判导致闸门永久假死

CNKI 触发风控时，会给**正常可浏览的摘要页 URL** 也追加 `captchaid=` 参数。
原实现把 `captchaid=` 当作"被拦截"特征，导致普通摘要页被误判为验证码页，
闸门 `blocked=True` 后再也不解除，连续 11 次探活全部判为拦截，下载流程永久卡死。

真正的点击式验证码页为 `kns.cnki.net/verify/home?captchaType=...`，具备 `/verify/` 与
`captchaType=` 双重特征，不会被漏判。三处同步移除误判标记 `captchaid=` / `verifycode`，
只保留 `/verify/`、`verify/home`、`captchaType=`：

- `backend/bridge_server.py` 的 `CAPTCHA_URL_MARKERS`
- `extension/service-worker.js` 的 `isCaptchaUrl()`
- `collab_cnki.py` 的 `VERIFY_MARKERS`

### Changed — 下载工作流并入 `cnki_client.py`，确立「唯一入口」

下载逻辑原本散落在 4 个逐代演化出来的脚本里（`humanlike_cnki.py` →
`continue_cnki.py` → `run_remaining.py` → `collab_cnki.py`），同一份逻辑存在 4 份拷贝，
其中 `already_have()` 更是重复 3 份、`hp()` / `snapshot` / `save_map` 各重复 2 份。
现全部合并进 `cnki_client.py`，按风险明确区分两种下载模式：

- **逐个下载（推荐主路径）**：新增 `download_one()` / `download_many()`。
  每篇都走「导航 → 浏览摘要 → 点下载 → 等浏览器落盘 → 归位」，并插入随机停顿与
  批次长休息（每 2–3 篇休息 3–8 分钟），请求稀疏、接近真人检索行为，风控风险低。
- **探索检索（只看不下载）**：从 `humanlike_cnki.py` 并入 `collect()` 与
  `maybe_typo()`（约 15% 概率先打错一次再纠正，模拟真人输入）。
- **服务自启**：从 `run_remaining.py` 并入 `ensure_server()`，venv 不存在时退回
  `sys.executable`，保证干净 clone 也能拉起服务。
- **批量下载（⚠️ 高风险，慎用）**：保留 `start_batch()` / `batch_status()` / `resume_batch()`，
  但 docstring 明确标注「先集中存好详情链接再批量下载，与人类检索行为不符，
  **极易触发 CNKI 风控导致持续验证码拦截**」，并引导改用 `download_many()`。
- `collab_cnki.py` 删除，CLI 并入 `cnki_client.py`
  （新增 `--papers` / `--one` / `--search` / `--ensure`）。
- 3 个被取代的历史脚本本机保留作参考，但加入 .gitignore 不入库。

### Changed — 路径配置化，消除硬编码个人路径

此前 `collab_cnki.py` 等脚本内联了 `C:/Users/<用户名>/...` 形式的绝对路径。
脚本要进公开仓库，这类路径会泄露系统用户名与本机目录结构，且 clone 到别处即失效。
按两类分别处理：

- **仓库自身路径**（`TOOL_DIR` / `VENV` / `SERVER`）：改由 `__file__` 相对推导，零配置，
  新增 `local_paths.py` 统一管理
- **本机个人目录**（论文归档目录、浏览器下载目录、待下清单等）：位于仓库之外，
  相对路径无法表达，改为读取 `local_config.json`（已加入 .gitignore），
  并支持环境变量 `CNKI_<KEY>` 覆盖；仓库内提供 `local_config.example.json` 模板

  路径配置在 `cnki_client` 中是**惰性加载**的：只有下载类函数会去读，
  检索 / 查看类函数完全不触碰配置，保证「只看不下载」的用法零配置依赖。

[0.9.0]: https://github.com/Lzffancy/cnki-browser-tool/compare/0c9d251...HEAD
