const BRIDGE_BASE_URL = "http://127.0.0.1:8765";
const BRIDGE_ALARM = "cnki-local-bridge-poll";
const TARGET_HOST_SUFFIX = ".cnki.net";
// 仅作为下面“下载是否已开始”检测的兜底超时阈值，不再是一段裸 while+sleep 的忙等时长。
const MAX_DOWNLOAD_WAIT_MS = 30_000;
const MAX_RECENT_DOWNLOADS = 20;
const MAX_BATCH_SIZE = 10;
const DEFAULT_BATCH_INTERVAL_SECONDS = 5;
const MIN_BATCH_INTERVAL_SECONDS = 3;
const MAX_BATCH_INTERVAL_SECONDS = 30;
const BATCH_STORAGE_KEY = "pdfDownloadBatch";
const BATCH_STEP_ALARM = "cnki-pdf-batch-step";
const BATCH_STEP_MIN_DELAY_MS = 30_000;
const BATCH_DOWNLOAD_TIMEOUT_MS = 180_000;
// 单篇/批次点击 PDF 下载按钮后，等待 Chrome 创建下载任务这一步的持久化状态。
// 用 chrome.downloads.onCreated 真实事件 + chrome.alarms 兜底超时推进，
// 不再用 while+sleep 忙等（原因见 triggerPdfDownloadOnTab 内注释）。
const DOWNLOAD_WAIT_STORAGE_KEY = "pendingPdfDownloadWait";
const DOWNLOAD_WAIT_ALARM = "cnki-pdf-download-wait-timeout";
// executeBridgeCommand/processNextPdfBatchItem 用这个哨兵区分“已经有明确结果”
// 和“结果会在下载事件或超时 alarm 触发后异步提交”，避免重复提交。
const PENDING_ASYNC_RESULT = Symbol("pending-async-download-result");
const CONTENT_SCRIPT_VERSION = "0.8.0";
const SEARCH_HOME_URL = "https://kns.cnki.net/kns8s/defaultresult/index";

const CNKI_MESSAGE = {
  GET_ACTIVE_PAGE: "CNKI_GET_ACTIVE_PAGE",
  GET_PAGE_SNAPSHOT: "CNKI_GET_PAGE_SNAPSHOT",
  GET_PAGE_DOM: "CNKI_GET_PAGE_DOM",
  SUBMIT_SEARCH: "CNKI_SUBMIT_SEARCH",
  SORT_SEARCH_RESULTS: "CNKI_SORT_SEARCH_RESULTS",
  GET_SEARCH_RESULTS: "CNKI_GET_SEARCH_RESULTS",
  SET_SEARCH_FIELD: "CNKI_SET_SEARCH_FIELD",
  SET_LIBRARY: "CNKI_SET_LIBRARY",
  TURN_PAGE: "CNKI_TURN_PAGE",
  GET_FILTERS: "CNKI_GET_FILTERS",
  APPLY_FILTER: "CNKI_APPLY_FILTER",
  SUBMIT_ADVANCED_SEARCH: "CNKI_SUBMIT_ADVANCED_SEARCH",
  GET_DOWNLOAD_OPTIONS: "CNKI_GET_DOWNLOAD_OPTIONS",
  CLICK_PDF_DOWNLOAD: "CNKI_CLICK_PDF_DOWNLOAD",
  GET_LOGIN_STATE: "CNKI_GET_LOGIN_STATE"
};

const recentDownloads = [];

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function isCnkiPage(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return url.protocol === "https:" && (url.hostname === "cnki.net" || url.hostname.endsWith(TARGET_HOST_SUFFIX));
  } catch {
    return false;
  }
}

function isCnkiUrl(rawUrl) {
  return isCnkiPage(rawUrl);
}

function createBatchId() {
  return `cnki-pdf-${crypto.randomUUID()}`;
}

function asErrorMessage(error, fallback = "页面操作失败。") {
  return error instanceof Error ? error.message : fallback;
}

function rememberDownload(downloadItem) {
  const summary = {
    id: downloadItem.id,
    url: downloadItem.url,
    filename: downloadItem.filename,
    state: downloadItem.state,
    error: downloadItem.error || null,
    startTime: downloadItem.startTime,
    endTime: downloadItem.endTime || null,
    bytesReceived: downloadItem.bytesReceived,
    totalBytes: downloadItem.totalBytes,
    danger: downloadItem.danger
  };
  const index = recentDownloads.findIndex((item) => item.id === summary.id);
  if (index >= 0) {
    recentDownloads[index] = summary;
  } else {
    recentDownloads.unshift(summary);
    recentDownloads.splice(MAX_RECENT_DOWNLOADS);
  }
}

// download.recent 曾经只读内存里的 recentDownloads；MV3 Service Worker 休眠重启后
// 该数组会被清空，导致明明下载已完成却查询不到（表现为“插件成功、服务不知道状态”）。
// 这里改为直接查询 chrome.downloads.search，其数据来自 Chrome 自身的下载历史，
// 不受 Worker 生命周期影响，与"近期的下载记录"面板看到的结果一致。
function summarizeDownload(item) {
  return {
    id: item.id,
    url: item.url,
    filename: item.filename,
    state: item.state,
    error: item.error || null,
    startTime: item.startTime,
    endTime: item.endTime || null,
    bytesReceived: item.bytesReceived,
    totalBytes: item.totalBytes,
    danger: item.danger
  };
}

async function getRecentDownloadsLive(limit) {
  const rawLimit = Number(limit);
  const boundedLimit = Number.isFinite(rawLimit)
    ? Math.min(Math.max(Math.floor(rawLimit), 1), 50)
    : MAX_RECENT_DOWNLOADS;
  const items = await chrome.downloads.search({ orderBy: ["-startTime"], limit: boundedLimit });
  return items.map(summarizeDownload);
}

function findDownloadSince(candidates, startedAfter) {
  return candidates.find((item) => {
    const start = new Date(item.startTime).getTime();
    return Number.isFinite(start) && start >= startedAfter - 1_000;
  });
}

async function getDownloadWaitState() {
  const stored = await chrome.storage.local.get(DOWNLOAD_WAIT_STORAGE_KEY);
  return stored[DOWNLOAD_WAIT_STORAGE_KEY] || null;
}

async function saveDownloadWaitState(state) {
  await chrome.storage.local.set({ [DOWNLOAD_WAIT_STORAGE_KEY]: state });
}

async function clearDownloadWaitState() {
  await chrome.storage.local.remove(DOWNLOAD_WAIT_STORAGE_KEY);
  await chrome.alarms.clear(DOWNLOAD_WAIT_ALARM);
}

// 无论是被 chrome.downloads.onCreated 真实事件触发，还是被兜底 alarm 超时触发，
// 都走这一个函数收尾：按等待来源（单次 Tool 调用 / 批次某一篇）分别推进结果，
// 保证两条触发路径不会重复处理同一个等待状态。
async function resolveDownloadWait(wait, download) {
  await clearDownloadWaitState();
  const detected = Boolean(download);
  const summarized = detected ? summarizeDownload(download) : null;

  if (wait.source === "command") {
    await submitBridgeResult(wait.commandId, {
      ...wait.clickResult,
      downloadDetected: detected,
      download: summarized
    });
    return;
  }

  // wait.source === "batch"：继续推进批次里当前这一篇的状态。
  const batch = await getBatchState();
  if (!batch || batch.state !== "running" || batch.currentIndex >= batch.items.length) {
    return;
  }
  const item = batch.items[batch.currentIndex];
  if (item.state !== "waiting_pdf_download") {
    return;
  }
  if (!detected) {
    await pauseBatch(batch, item, "已点击 PDF 下载按钮，但 Chrome 未在 30 秒内创建下载任务；可能需要用户在页面处理权限、登录或验证码。");
    return;
  }
  item.state = "downloading";
  item.download = summarized;
  item.downloadStartedAt = new Date().toISOString();
  batch.updatedAt = item.downloadStartedAt;
  await saveBatchState(batch);
  await scheduleBatchStep(BATCH_STEP_MIN_DELAY_MS);
}

async function handleDownloadWaitTimeout() {
  const wait = await getDownloadWaitState();
  if (!wait) {
    return;
  }
  const candidates = await chrome.downloads.search({ orderBy: ["-startTime"], limit: 10 });
  const found = findDownloadSince(candidates, wait.startedAfter);
  if (found) {
    rememberDownload(found);
  }
  await resolveDownloadWait(wait, found || null);
}

// Service Worker 重启后主动核实一次：如果等待期间下载事件恰好发生在 Worker
// 不在线的瞬间，这里用 chrome.downloads.search 兜底找回，不必等 alarm 到期。
// 没找到也不清理状态——留给已经持久化的 DOWNLOAD_WAIT_ALARM 到期后判定为未检测到。
async function reconcileDownloadWaitOnStartup() {
  const wait = await getDownloadWaitState();
  if (!wait) {
    return;
  }
  const candidates = await chrome.downloads.search({ orderBy: ["-startTime"], limit: 10 });
  const found = findDownloadSince(candidates, wait.startedAfter);
  if (found) {
    rememberDownload(found);
    await resolveDownloadWait(wait, found);
  }
}

chrome.downloads.onCreated.addListener(async (downloadItem) => {
  rememberDownload(downloadItem);
  const wait = await getDownloadWaitState();
  if (wait && findDownloadSince([downloadItem], wait.startedAfter)) {
    await resolveDownloadWait(wait, downloadItem);
  }
});
chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta?.id) {
    return;
  }
  const [download] = await chrome.downloads.search({ id: delta.id });
  if (!download) {
    return;
  }
  rememberDownload(download);
  await advanceBatchFromDownloadEvent(download);
});

// MV3 Service Worker 会在空闲时休眠，不能依赖一个数分钟的 while + sleep 循环。
// 每篇只启动一次“详情页 -> PDF 按钮”动作；下载完成事件和 alarm 从持久化状态继续下一篇。
async function scheduleBatchStep(delayMs = 0) {
  const delay = Math.max(BATCH_STEP_MIN_DELAY_MS, Number(delayMs) || 0);
  await chrome.alarms.create(BATCH_STEP_ALARM, { when: Date.now() + delay });
}

async function pauseBatch(batch, item, reason) {
  item.state = "paused";
  item.error = reason;
  batch.state = "paused";
  batch.pauseReason = reason;
  batch.updatedAt = new Date().toISOString();
  await saveBatchState(batch);
  await chrome.alarms.clear(BATCH_STEP_ALARM);
}

async function advanceBatchFromDownloadEvent(download) {
  const batch = await getBatchState();
  if (!batch || batch.state !== "running" || batch.currentIndex >= batch.items.length) {
    return;
  }
  const item = batch.items[batch.currentIndex];
  if (item.state !== "downloading" || item.download?.id !== download.id) {
    return;
  }

  item.download = recentDownloads.find((entry) => entry.id === download.id) || download;
  if (download.state === "complete") {
    item.state = "completed";
    item.error = null;
    batch.currentIndex += 1;
    batch.updatedAt = new Date().toISOString();
    if (batch.currentIndex >= batch.items.length) {
      batch.state = "completed";
      batch.completedAt = batch.updatedAt;
      await saveBatchState(batch);
      await chrome.alarms.clear(BATCH_STEP_ALARM);
      return;
    }
    await saveBatchState(batch);
    await scheduleBatchStep(batch.intervalSeconds * 1_000);
    return;
  }
  if (download.state === "interrupted") {
    await pauseBatch(batch, item, `下载已中断：${download.error || "未知原因"}。`);
  }
}

// 定位一个可用的 CNKI 标签页，不再要求它必须是“当前活动标签”。
// 优先级：指定 tabId（批次流程固定复用同一 tab）> 当前活动标签（若是 CNKI）>
// 任意一个已打开的 CNKI 标签页。content script 的 DOM 操作与消息驱动都不依赖
// 窗口是否可见、标签是否处于活动状态，因此用户切走焦点、切到别的窗口都不应中断流程。
async function getPreferredCnkiTab(preferredTabId = null) {
  if (preferredTabId != null) {
    try {
      const tab = await chrome.tabs.get(preferredTabId);
      if (tab?.id && isCnkiPage(tab.url ?? "")) {
        return tab;
      }
    } catch {
      // 指定的 tab 已关闭或不可访问，继续向下寻找其他 CNKI 标签页。
    }
  }

  const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (activeTab?.id && isCnkiPage(activeTab.url ?? "")) {
    return activeTab;
  }

  const cnkiTabs = await chrome.tabs.query({ url: ["https://cnki.net/*", "https://*.cnki.net/*"] });
  const usable = cnkiTabs.filter((tab) => tab?.id != null);
  if (usable.length > 0) {
    return usable[0];
  }

  throw new Error("未找到可用的 CNKI 页面。请先通过 session.open_search 创建检索页，或在 Chrome 中打开知网。");
}

async function getOrCreateCnkiTab() {
  const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (activeTab?.id && isCnkiPage(activeTab.url ?? "")) {
    return activeTab;
  }

  const createdTab = await chrome.tabs.create({ url: SEARCH_HOME_URL, active: true });
  if (!createdTab.id) {
    throw new Error("Chrome 未能创建 CNKI 检索标签页。");
  }
  await waitForTabComplete(createdTab.id, 25_000);
  await sleep(800);
  return getCnkiTab(createdTab.id);
}

async function getCnkiTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab?.id || !isCnkiPage(tab.url ?? "")) {
    throw new Error("用于下载的 CNKI 标签页已关闭或不再是知网页面。");
  }
  return tab;
}

async function ensureContentScript(tabId) {
  let mustInject = false;
  try {
    const snapshot = await chrome.tabs.sendMessage(tabId, { type: CNKI_MESSAGE.GET_PAGE_SNAPSHOT });
    mustInject = snapshot?.assistantScriptVersion !== CONTENT_SCRIPT_VERSION;
  } catch (error) {
    const message = asErrorMessage(error, "内容脚本连接失败。");
    if (!message.includes("Receiving end does not exist")) {
      throw error;
    }
    mustInject = true;
  }
  if (mustInject) {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content/cnki-page.js"]
    });
  }
}

async function readPage(tab, messageType, options = {}) {
  await ensureContentScript(tab.id);
  const result = await chrome.tabs.sendMessage(tab.id, { type: messageType, options });
  if (result?.ok === false && result.error) {
    throw new Error(result.error);
  }
  return result;
}

async function getActiveCnkiPage() {
  const tab = await getPreferredCnkiTab();
  const snapshot = await readPage(tab, CNKI_MESSAGE.GET_PAGE_SNAPSHOT);
  return { ok: true, data: snapshot };
}

function waitForTabComplete(tabId, timeoutMs = 20_000) {
  return new Promise((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("等待知网页面加载超时。"));
    }, timeoutMs);

    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        clearTimeout(timeoutId);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }

    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function navigateCnkiTab(tabId, rawUrl) {
  if (!isCnkiUrl(rawUrl)) {
    throw new Error("仅允许打开 https://*.cnki.net/ 下的文章或检索页面。");
  }
  const completed = waitForTabComplete(tabId);
  await chrome.tabs.update(tabId, { url: rawUrl });
  await completed;
  const refreshedTab = await getCnkiTab(tabId);
  await sleep(600);
  return refreshedTab;
}

async function navigateActiveCnkiTab(payload) {
  const rawUrl = typeof payload?.url === "string" ? payload.url : "";
  const tab = await getOrCreateCnkiTab();
  const refreshedTab = await navigateCnkiTab(tab.id, rawUrl);
  const snapshot = await readPage(refreshedTab, CNKI_MESSAGE.GET_PAGE_SNAPSHOT);
  return { navigated: true, tabId: refreshedTab.id, ...snapshot };
}

async function getSessionStatus() {
  const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const cnkiTabs = await chrome.tabs.query({ url: ["https://cnki.net/*", "https://*.cnki.net/*"] });
  const status = {
    activeTab: activeTab ? {
      id: activeTab.id,
      url: activeTab.url || null,
      title: activeTab.title || null,
      isCnki: isCnkiPage(activeTab.url || "")
    } : null,
    cnkiTabs: cnkiTabs.map((tab) => ({
      id: tab.id,
      url: tab.url || null,
      title: tab.title || null,
      active: Boolean(tab.active)
    })),
    canOpenSearch: true
  };

  // 登录状态启发式检测：优先活动 CNKI 标签，其次第一个 CNKI 标签；读不到就明确标注，不猜测。
  const loginTab = (activeTab && isCnkiPage(activeTab.url || ""))
    ? activeTab
    : cnkiTabs.find((tab) => tab?.id != null);
  if (loginTab?.id) {
    try {
      status.login = await readPage(loginTab, CNKI_MESSAGE.GET_LOGIN_STATE);
    } catch (error) {
      status.login = { state: "unknown", evidence: `读取登录状态失败：${asErrorMessage(error)}` };
    }
  } else {
    status.login = { state: "no_cnki_tab", evidence: "当前没有打开的 CNKI 标签页，无法检测登录状态" };
  }

  return status;
}

async function openSearchPage(payload) {
  const tab = await getOrCreateCnkiTab();
  const query = typeof payload?.query === "string" ? payload.query.trim() : "";
  if (!query) {
    const refreshedTab = await navigateCnkiTab(tab.id, SEARCH_HOME_URL);
    return { opened: true, tabId: refreshedTab.id, url: refreshedTab.url, query: null };
  }
  return submitCnkiSearchInTab(tab, query);
}

async function submitCnkiSearchInTab(rawTab, query) {
  let tab = rawTab;
  if (!new URL(tab.url).pathname.includes("/kns8s/")) {
    tab = await navigateCnkiTab(tab.id, SEARCH_HOME_URL);
  }

  const navigation = waitForTabComplete(tab.id, 20_000).catch(() => null);
  const submitted = await readPage(tab, CNKI_MESSAGE.SUBMIT_SEARCH, { query });
  await navigation;
  await sleep(900);
  const refreshedTab = await getCnkiTab(tab.id);
  const results = await waitForSearchResults(refreshedTab, query, 15_000);
  return { ...submitted, tabId: refreshedTab.id, results };
}

async function submitCnkiSearch(payload) {
  const query = typeof payload?.query === "string" ? payload.query.trim() : "";
  if (!query) {
    throw new Error("缺少检索词 query。");
  }
  const tab = await getOrCreateCnkiTab();
  return submitCnkiSearchInTab(tab, query);
}

async function waitForSearchResults(tab, query, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let latestError = null;
  while (Date.now() < deadline) {
    try {
      const result = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: 50 });
      if (result.visibleRowCount > 0 && result.query === query) {
        return result;
      }
    } catch (error) {
      latestError = error;
    }
    await sleep(600);
  }
  throw new Error(`检索结果未在页面中就绪。${latestError ? `最后错误：${asErrorMessage(latestError)}` : ""}`);
}

function resultSignature(result) {
  return Array.isArray(result?.results)
    ? result.results.slice(0, 5).map((item) => item.articleUrl || item.title).join("|")
    : "";
}

async function sortCnkiSearchResults(payload) {
  const sortBy = typeof payload?.sortBy === "string" ? payload.sortBy.trim() : "";
  if (!sortBy) {
    throw new Error("缺少排序字段 sortBy。");
  }
  const tab = await getPreferredCnkiTab();
  const before = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: 10 });
  const submitted = await readPage(tab, CNKI_MESSAGE.SORT_SEARCH_RESULTS, { sortBy });
  const beforeSignature = resultSignature(before);
  const deadline = Date.now() + 15_000;
  let latest = before;
  while (Date.now() < deadline) {
    await sleep(600);
    latest = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: payload?.limit || 20 });
    if (latest.visibleRowCount > 0 && resultSignature(latest) !== beforeSignature) {
      return { ...submitted, results: latest };
    }
  }
  throw new Error("排序控件已点击，但检索结果未在 15 秒内刷新。请检查页面是否出现登录、验证码或加载提示。");
}

async function getSearchResults(payload) {
  const tab = await getPreferredCnkiTab();
  return readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: payload?.limit });
}

async function setSearchField(payload) {
  const field = typeof payload?.field === "string" ? payload.field.trim() : "";
  if (!field) {
    throw new Error("缺少检索字段 field。");
  }
  const tab = await getPreferredCnkiTab();
  return readPage(tab, CNKI_MESSAGE.SET_SEARCH_FIELD, { field });
}

async function setLibrary(payload) {
  const library = typeof payload?.library === "string" ? payload.library.trim() : "";
  if (!library) {
    throw new Error("缺少文献库 library。");
  }
  const tab = await getPreferredCnkiTab();
  const navigation = waitForTabComplete(tab.id, 20_000).catch(() => null);
  const applied = await readPage(tab, CNKI_MESSAGE.SET_LIBRARY, { library });
  await navigation;
  await sleep(900);
  const refreshedTab = await getCnkiTab(tab.id);
  const snapshot = await readPage(refreshedTab, CNKI_MESSAGE.GET_PAGE_SNAPSHOT);
  return { ...applied, url: snapshot.url, title: snapshot.title };
}

// 翻页/筛选后结果表会异步刷新，用「结果签名 + 可见行数」变化判定刷新完成，
// 与 sortCnkiSearchResults 同一套思路；这里额外允许结果变空也能正确返回。
async function waitForResultChange(tab, before, limit = 20, timeoutMs = 15_000) {
  const beforeSignature = resultSignature(before);
  const beforeRows = before?.visibleRowCount ?? -1;
  const deadline = Date.now() + timeoutMs;
  let latest = before;
  while (Date.now() < deadline) {
    await sleep(600);
    latest = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit });
    if (resultSignature(latest) !== beforeSignature || latest.visibleRowCount !== beforeRows) {
      return latest;
    }
  }
  return latest;
}

async function turnPage(payload) {
  const tab = await getPreferredCnkiTab();
  const before = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: 10 });
  const applied = await readPage(tab, CNKI_MESSAGE.TURN_PAGE, payload);
  const latest = await waitForResultChange(tab, before, payload?.limit || 20);
  return { ...applied, results: latest };
}

async function getFilters(payload) {
  const tab = await getPreferredCnkiTab();
  return readPage(tab, CNKI_MESSAGE.GET_FILTERS, { groups: payload?.groups });
}

async function applyFilter(payload) {
  const tab = await getPreferredCnkiTab();
  const before = await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: 10 });
  const applied = await readPage(tab, CNKI_MESSAGE.APPLY_FILTER, payload);
  const latest = await waitForResultChange(tab, before, payload?.limit || 20);
  return { ...applied, results: latest };
}

// 高级检索表单提交后页面会整页跳转到全新的结果页（AdvSearch -> defaultresult），
// 不能像 applyFilter 那样用"结果签名变化"判定完成（提交前那页压根没有结果表可读）。
// 也不能复用 waitForSearchResults 的 query 字符串比对（多字段组合检索没有单一
// query 值对应一框式检索框）。这里只要求"能成功读到结果表"（哪怕 0 条也算数），
// 与 setLibrary 的 waitForTabComplete + sleep 兜底思路一致。
async function waitForAdvancedSearchResults(tab, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let latestError = null;
  while (Date.now() < deadline) {
    try {
      return await readPage(tab, CNKI_MESSAGE.GET_SEARCH_RESULTS, { limit: 50 });
    } catch (error) {
      latestError = error;
    }
    await sleep(600);
  }
  throw new Error(`检索结果未在页面中就绪。${latestError ? `最后错误：${asErrorMessage(latestError)}` : ""}`);
}

async function submitAdvancedSearch(payload) {
  const conditions = Array.isArray(payload?.conditions) ? payload.conditions : [];
  if (conditions.length < 1) {
    throw new Error("缺少检索条件 conditions。");
  }
  const tab = await getPreferredCnkiTab();
  if (!new URL(tab.url).pathname.includes("/kns8s/AdvSearch")) {
    throw new Error("当前标签不是 CNKI 高级检索页（AdvSearch），请先用 page.navigate 打开该页面。");
  }
  const navigation = waitForTabComplete(tab.id, 20_000).catch(() => null);
  const submitted = await readPage(tab, CNKI_MESSAGE.SUBMIT_ADVANCED_SEARCH, { conditions });
  await navigation;
  await sleep(900);
  const refreshedTab = await getCnkiTab(tab.id);
  const results = await waitForAdvancedSearchResults(refreshedTab, 15_000);
  return { ...submitted, tabId: refreshedTab.id, results };
}

// options.commandId 存在时表示这是单次 article.click_pdf_download Tool 调用
// （source 固定为 "command"，结果要回传给对应的桥接 commandId）；
// 批次流程调用时不传 commandId，source 为 "batch"。
async function triggerPdfDownloadOnTab(tab, options = {}) {
  const commandId = options.commandId || null;
  const source = commandId ? "command" : "batch";
  const startedAt = Date.now();
  const clickResult = await readPage(tab, CNKI_MESSAGE.CLICK_PDF_DOWNLOAD);
  if (!clickResult.clicked) {
    return { ...clickResult, downloadDetected: false, download: null };
  }

  // 点击到 Chrome 创建下载任务通常在几百毫秒内完成，先同步核实一次，
  // 多数情况可以直接返回，不需要进入下面的异步等待路径。
  const quick = await chrome.downloads.search({ orderBy: ["-startTime"], limit: 5 });
  const immediate = findDownloadSince(quick, startedAt);
  if (immediate) {
    rememberDownload(immediate);
    return { ...clickResult, downloadDetected: true, download: summarizeDownload(immediate) };
  }

  // 不再用 while + sleep 阻塞等待最长 30 秒——MV3 Service Worker 在这段纯计时器
  // 等待期间可能被 Chrome 判定为空闲并直接终止：一旦被杀，点击可能已经成功、
  // 下载可能已经真的开始了，但提交结果的代码永远不会被执行到，桥接服务只能等到
  // 自己的超时报错，表现为"插件其实成功了，服务却说不知道"（与之前修复的
  // download.recent 内存缓存问题同类，也是批次下载早前踩过的坑）。
  // 改为把等待状态持久化到 chrome.storage.local，靠 chrome.downloads.onCreated
  // 真实事件和 chrome.alarms 兜底超时来推进——这两者都能在 Worker 被终止后
  // 重新唤醒并继续把结果提交出去。
  await saveDownloadWaitState({ source, commandId, startedAfter: startedAt, clickResult });
  await chrome.alarms.create(DOWNLOAD_WAIT_ALARM, { when: Date.now() + MAX_DOWNLOAD_WAIT_MS });
  return PENDING_ASYNC_RESULT;
}

function normalizeBatchInput(payload) {
  if (!Array.isArray(payload?.articleUrls)) {
    throw new Error("articleUrls 必须是论文详情页 URL 数组。");
  }
  const deduplicated = [...new Set(payload.articleUrls.filter((url) => typeof url === "string"))];
  if (deduplicated.length < 1) {
    throw new Error("下载批次至少需要 1 篇论文。");
  }
  if (deduplicated.length > MAX_BATCH_SIZE) {
    throw new Error(`单批最多 ${MAX_BATCH_SIZE} 篇，请分批处理。`);
  }
  if (deduplicated.some((url) => !isCnkiUrl(url) || !new URL(url).pathname.includes("/article/"))) {
    throw new Error("批次中只能包含 CNKI 论文详情页 URL，不能传入下载接口或其他站点链接。");
  }
  const rawInterval = Number(payload?.intervalSeconds);
  const intervalSeconds = Number.isFinite(rawInterval)
    ? Math.min(Math.max(Math.floor(rawInterval), MIN_BATCH_INTERVAL_SECONDS), MAX_BATCH_INTERVAL_SECONDS)
    : DEFAULT_BATCH_INTERVAL_SECONDS;
  return { urls: deduplicated, intervalSeconds };
}

async function getBatchState() {
  const stored = await chrome.storage.local.get(BATCH_STORAGE_KEY);
  return stored[BATCH_STORAGE_KEY] || null;
}

async function saveBatchState(batch) {
  await chrome.storage.local.set({ [BATCH_STORAGE_KEY]: batch });
}

async function startPdfBatch(payload) {
  const { urls, intervalSeconds } = normalizeBatchInput(payload);
  const current = await getBatchState();
  if (current?.state === "running") {
    throw new Error(`已有下载批次正在执行：${current.batchId}。请等待完成或暂停后再创建新批次。`);
  }
  const tab = await getPreferredCnkiTab();
  const batch = {
    batchId: createBatchId(),
    state: "running",
    tabId: tab.id,
    intervalSeconds,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    currentIndex: 0,
    items: urls.map((articleUrl, index) => ({
      index: index + 1,
      articleUrl,
      state: "queued",
      title: null,
      download: null,
      downloadStartedAt: null,
      error: null
    }))
  };
  await chrome.alarms.clear(BATCH_STEP_ALARM);
  await saveBatchState(batch);
  void processNextPdfBatchItem();
  return {
    batchId: batch.batchId,
    state: batch.state,
    total: batch.items.length,
    intervalSeconds: batch.intervalSeconds,
    note: "批次通过 Chrome 下载事件和持久化状态逐篇推进；每篇均在详情页点击已有 PDF 下载按钮。遇到登录、权限、验证码、页面改版或下载异常会自动暂停。"
  };
}

async function processNextPdfBatchItem() {
  const batch = await getBatchState();
  if (!batch || batch.state !== "running") {
    return;
  }
  if (batch.currentIndex >= batch.items.length) {
    batch.state = "completed";
    batch.completedAt = new Date().toISOString();
    batch.updatedAt = batch.completedAt;
    await saveBatchState(batch);
    await chrome.alarms.clear(BATCH_STEP_ALARM);
    return;
  }

  const item = batch.items[batch.currentIndex];
  if (item.state === "downloading" && item.download?.id) {
    const [download] = await chrome.downloads.search({ id: item.download.id });
    if (download?.state === "complete" || download?.state === "interrupted") {
      await advanceBatchFromDownloadEvent(download);
      return;
    }
    const startedAt = new Date(item.downloadStartedAt || batch.updatedAt).getTime();
    if (Number.isFinite(startedAt) && Date.now() - startedAt > BATCH_DOWNLOAD_TIMEOUT_MS) {
      await pauseBatch(batch, item, "等待浏览器 PDF 下载完成超时。");
      return;
    }
    await scheduleBatchStep(BATCH_STEP_MIN_DELAY_MS);
    return;
  }

  if (item.state === "waiting_pdf_download") {
    // 这一篇“点击后等待下载开始”的检测已经交给持久化的 DOWNLOAD_WAIT_ALARM 和
    // chrome.downloads.onCreated 事件（见 resolveDownloadWait）独立推进，这里
    // 什么都不做，避免和它们竞态触发重复点击。
    return;
  }

  // Worker 在页面动作完成前休眠时，下一次 alarm 从该状态重新处理当前篇；
  // 尚未记录下载任务时不会跳到下一篇，避免遗漏。
  item.state = "opening_detail";
  item.error = null;
  batch.updatedAt = new Date().toISOString();
  await saveBatchState(batch);

  try {
    let tab;
    try {
      tab = await navigateCnkiTab(batch.tabId, item.articleUrl);
    } catch (navError) {
      // 批次启动时记录的 tab 可能已被用户关闭；重新定位一个可用的 CNKI 标签页
      // 继续当前篇，并回写 batch.tabId，避免后续每一篇都重试同一个失效的 tab。
      tab = await getPreferredCnkiTab();
      batch.tabId = tab.id;
      await saveBatchState(batch);
      tab = await navigateCnkiTab(tab.id, item.articleUrl);
    }
    const snapshot = await readPage(tab, CNKI_MESSAGE.GET_PAGE_SNAPSHOT);
    item.title = snapshot.title.replace(/\s*-\s*中国知网\s*$/, "").trim() || null;
    item.state = "waiting_pdf_download";
    batch.updatedAt = new Date().toISOString();
    await saveBatchState(batch);

    const result = await triggerPdfDownloadOnTab(tab);
    if (result === PENDING_ASYNC_RESULT) {
      // 下载是否已开始会由 chrome.downloads.onCreated 事件或 DOWNLOAD_WAIT_ALARM
      // 超时兜底异步判定（见 resolveDownloadWait），这里不再往下走。
      return;
    }
    if (!result.clicked) {
      throw new Error(result.reason || "未找到 PDF 下载按钮。");
    }
    if (!result.downloadDetected || !result.download?.id) {
      throw new Error("已点击 PDF 下载按钮，但 Chrome 未创建下载任务；可能需要用户在页面处理权限、登录或验证码。");
    }

    item.state = "downloading";
    item.download = result.download;
    item.downloadStartedAt = new Date().toISOString();
    batch.updatedAt = item.downloadStartedAt;
    await saveBatchState(batch);
    await scheduleBatchStep(BATCH_STEP_MIN_DELAY_MS);
  } catch (error) {
    await pauseBatch(batch, item, asErrorMessage(error, "批次下载失败。"));
  }
}

async function resumePdfBatch() {
  const batch = await getBatchState();
  if (!batch) {
    throw new Error("当前没有可恢复的下载批次。");
  }
  if (batch.state !== "paused") {
    throw new Error(`当前批次状态为 ${batch.state}，不能恢复。`);
  }
  const item = batch.items[batch.currentIndex];
  if (item?.state === "paused") {
    item.state = "queued";
    item.error = null;
  }
  batch.state = "running";
  batch.pauseReason = null;
  batch.updatedAt = new Date().toISOString();
  await saveBatchState(batch);
  void processNextPdfBatchItem();
  return { batchId: batch.batchId, state: batch.state, currentIndex: batch.currentIndex };
}

// 本机桥接唯一的 Tool 路由点。每个分支均映射到具名业务行为；
// 不接受任意 JavaScript、任意 CSS Selector 或直接下载地址。
async function executeBridgeCommand(command) {
  switch (command.action) {
    case "page.snapshot": {
      const tab = await getPreferredCnkiTab();
      return readPage(tab, CNKI_MESSAGE.GET_PAGE_SNAPSHOT);
    }
    case "page.dom": {
      const tab = await getPreferredCnkiTab();
      return readPage(tab, CNKI_MESSAGE.GET_PAGE_DOM, { maxChars: command.payload?.maxChars });
    }
    case "page.navigate":
      return navigateActiveCnkiTab(command.payload);
    case "session.status":
      return getSessionStatus();
    case "session.open_search":
      return openSearchPage(command.payload);
    case "search.submit":
      return submitCnkiSearch(command.payload);
    case "search.sort":
      return sortCnkiSearchResults(command.payload);
    case "search.results":
      return getSearchResults(command.payload);
    case "search.set_field":
      return setSearchField(command.payload);
    case "search.set_library":
      return setLibrary(command.payload);
    case "search.turn_page":
      return turnPage(command.payload);
    case "search.get_filters":
      return getFilters(command.payload);
    case "search.apply_filter":
      return applyFilter(command.payload);
    case "search.advanced_submit":
      return submitAdvancedSearch(command.payload);
    case "article.download_options": {
      const tab = await getPreferredCnkiTab();
      return readPage(tab, CNKI_MESSAGE.GET_DOWNLOAD_OPTIONS);
    }
    case "article.click_pdf_download": {
      const tab = await getPreferredCnkiTab();
      return triggerPdfDownloadOnTab(tab, { commandId: command.id });
    }
    case "batch.start_pdf_download":
      return startPdfBatch(command.payload);
    case "batch.get_status": {
      const batch = await getBatchState();
      return { batch };
    }
    case "batch.resume_pdf_download":
      return resumePdfBatch();
    case "download.recent":
      return { downloads: await getRecentDownloadsLive(command.payload?.limit) };
    default:
      throw new Error(`不支持的本地 Tool：${command.action}`);
  }
}

async function submitBridgeResult(commandId, result) {
  await fetch(`${BRIDGE_BASE_URL}/v1/extension/command-result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commandId, result })
  });
}

async function submitBridgeError(commandId, error) {
  await fetch(`${BRIDGE_BASE_URL}/v1/extension/command-result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      commandId,
      error: asErrorMessage(error, "插件执行失败。")
    })
  });
}

async function pollLocalBridge() {
  try {
    const response = await fetch(`${BRIDGE_BASE_URL}/v1/extension/next-command?wait=20`, { cache: "no-store" });
    if (response.status === 204 || !response.ok) {
      return;
    }
    const command = await response.json();
    try {
      const result = await executeBridgeCommand(command);
      if (result === PENDING_ASYNC_RESULT) {
        // 该 Tool 调用的结果会在下载事件或等待超时触发后异步提交（见
        // resolveDownloadWait），这里不重复提交，避免竞态。
        return;
      }
      await submitBridgeResult(command.id, result);
    } catch (error) {
      await submitBridgeError(command.id, error);
    }
  } catch {
    // 本机 Python 服务未启动时静默等待下一次轮询。
  }
}

async function resumeStoredBatch() {
  const batch = await getBatchState();
  if (batch?.state === "running") {
    void processNextPdfBatchItem();
  }
}

function scheduleBridgePolling() {
  chrome.alarms.create(BRIDGE_ALARM, { periodInMinutes: 0.5 });
  void pollLocalBridge();
  void resumeStoredBatch();
  void reconcileDownloadWaitOnStartup();
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({
    extensionState: {
      version: chrome.runtime.getManifest().version,
      initializedAt: new Date().toISOString()
    }
  });
  scheduleBridgePolling();
});

chrome.runtime.onStartup.addListener(scheduleBridgePolling);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BRIDGE_ALARM) {
    void pollLocalBridge();
    void resumeStoredBatch();
    return;
  }
  if (alarm.name === BATCH_STEP_ALARM) {
    void processNextPdfBatchItem();
    return;
  }
  if (alarm.name === DOWNLOAD_WAIT_ALARM) {
    void handleDownloadWaitTimeout();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== CNKI_MESSAGE.GET_ACTIVE_PAGE) {
    return;
  }
  getActiveCnkiPage()
    .then(sendResponse)
    .catch((error) => {
      sendResponse({ ok: false, code: "PAGE_OPERATION_FAILED", message: asErrorMessage(error) });
    });
  return true;
});

// ---- 安全验证页主动上报 ----
// 服务端有独立的验证码拦截闸门（CaptchaGate）。它最可靠、最及时的拦截情报来源，
// 不是等 30s 轮询探活，而是扩展在 Chrome 真正导航到验证页的那一刻直接上报。这样
// 用户刚弹出验证码，服务端就进入等待态并暂停下发命令，用户填完、页面跳回 kns，
// 再一次上报即解除、自动续跑。配合 manifest 的 webNavigation 权限。
let lastCaptchaReport = null; // "blocked" | "clear" | null，防止重复上报刷屏

function isCaptchaUrl(url) {
  if (typeof url !== "string") return false;
  const lower = url.toLowerCase();
  return (
    lower.includes("/verify/") ||
    lower.includes("verify/home") ||
    lower.includes("captchatype=") ||
    lower.includes("captchaverify") ||
    lower.includes("safeverify")
  );
}

function isCnkiUrlSafe(url) {
  return typeof url === "string" && /^https:\/\/([^/]+\.)?cnki\.net\//.test(url);
}

async function reportCaptchaState(blocked, url) {
  const state = blocked ? "blocked" : "clear";
  if (state === lastCaptchaReport) return;
  lastCaptchaReport = state;
  try {
    await fetch(`${BRIDGE_BASE_URL}/v1/extension/captcha-report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ blocked, url: url || "" })
    });
  } catch {
    // 本机 Python 服务未启动时静默，等下一次导航再试。
  }
}

if (chrome.webNavigation) {
  const handleNavigation = (details) => {
    // 只看顶级 frame，子 frame（iframe 里的验证组件）不计，避免重复/误报。
    if (details.frameId !== 0) return;
    const url = details.url;
    if (isCaptchaUrl(url)) {
      void reportCaptchaState(true, url);
    } else if (isCnkiUrlSafe(url)) {
      // 进入正常 CNKI 页 → 视为解除，立即上报 clear。
      void reportCaptchaState(false, url);
    }
  };
  chrome.webNavigation.onCommitted.addListener(handleNavigation);
  chrome.webNavigation.onCompleted.addListener(handleNavigation);
}
