(() => {
  const SCRIPT_VERSION = "0.7.0";
  if (globalThis.__cnkiResearchAssistantVersion === SCRIPT_VERSION) {
    return;
  }
  globalThis.__cnkiResearchAssistantVersion = SCRIPT_VERSION;

  const DEFAULT_DOM_LIMIT = 120_000;
  const MAX_DOM_LIMIT = 300_000;
  const DOWNLOAD_TEXT_PATTERN = /(PDF\s*(下载|全文|阅读)?|下载\s*PDF|PDF全文)/i;
  const GENERIC_DOWNLOAD_PATTERN = /(下载|全文)/i;
  const SEARCH_PAGE_PATH = "/kns8s/";

  const normalizeText = (value) => String(value || "").replace(/\s+/g, " ").trim();

  function isVisible(element) {
    if (!(element instanceof HTMLElement)) {
      return false;
    }
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function setNativeInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (!setter) {
      throw new Error("浏览器无法设置检索框内容。");
    }
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function getPageSnapshot() {
    const visibleText = normalizeText(document.body?.innerText ?? "");
    return {
      url: window.location.href,
      title: document.title,
      readyState: document.readyState,
      language: document.documentElement.lang || null,
      assistantScriptVersion: SCRIPT_VERSION,
      textPreview: visibleText.slice(0, 800),
      textLength: visibleText.length,
      capturedAt: new Date().toISOString()
    };
  }

  function getPageDom(options = {}) {
    const parsedLimit = Number(options.maxChars);
    const maxChars = Number.isFinite(parsedLimit)
      ? Math.min(Math.max(Math.floor(parsedLimit), 1_000), MAX_DOM_LIMIT)
      : DEFAULT_DOM_LIMIT;
    const html = document.documentElement?.outerHTML ?? "";

    return {
      url: window.location.href,
      title: document.title,
      capturedAt: new Date().toISOString(),
      html: html.slice(0, maxChars),
      htmlLength: html.length,
      returnedLength: Math.min(html.length, maxChars),
      truncated: html.length > maxChars
    };
  }

  function getSearchInput() {
    const input = document.querySelector("#txt_search.search-input");
    if (!(input instanceof HTMLInputElement)) {
      throw new Error("当前页面未识别到 CNKI 检索框。请先打开知网检索页。");
    }
    return input;
  }

  function getSearchRows() {
    return [...document.querySelectorAll("#gridTable table.result-table-list tbody > tr")]
      .filter((row) => row instanceof HTMLTableRowElement);
  }

  function getCellText(row, selector) {
    return normalizeText(row.querySelector(selector)?.textContent || "");
  }

  function extractSearchResults(options = {}) {
    if (!window.location.pathname.includes(SEARCH_PAGE_PATH)) {
      throw new Error("当前不是 CNKI 检索结果页。请先执行检索或打开 kns8s 结果页。");
    }

    const requestedLimit = Number(options.limit);
    const limit = Number.isFinite(requestedLimit)
      ? Math.min(Math.max(Math.floor(requestedLimit), 1), 50)
      : 20;
    const rows = getSearchRows();
    const results = rows.slice(0, limit).map((row, index) => {
      const titleLink = row.querySelector("td.name a.inline[href]");
      const articleUrl = titleLink instanceof HTMLAnchorElement ? titleLink.href : null;
      const title = normalizeText(titleLink?.textContent || "");
      const authors = [...row.querySelectorAll("td.author a")]
        .map((element) => normalizeText(element.textContent || ""))
        .filter(Boolean);
      const directDownload = row.querySelector("td.operat a.downloadlink[href]");
      const downloadUrl = directDownload instanceof HTMLAnchorElement ? directDownload.href : null;
      const indexText = getCellText(row, "td.seq");

      return {
        index: Number.parseInt(indexText, 10) || index + 1,
        title,
        articleUrl,
        authors,
        source: getCellText(row, "td.source"),
        publishedAt: getCellText(row, "td.date"),
        resourceType: getCellText(row, "td.data"),
        citations: getCellText(row, "td.quote"),
        downloads: getCellText(row, "td.download"),
        hasNormalDownloadEntry: Boolean(downloadUrl)
      };
    }).filter((item) => item.title && item.articleUrl);

    const searchInput = document.querySelector("#txt_search");
    const activePage = document.querySelector(".pagination .active, .pagination .cur, .page-index .active, .page-index .cur");
    const totalText = normalizeText(document.querySelector(".result-count, .search-result-count, .total, .count")?.textContent || "");

    return {
      url: window.location.href,
      query: searchInput instanceof HTMLInputElement ? searchInput.value : null,
      pageTitle: document.title,
      visibleRowCount: rows.length,
      returnedCount: results.length,
      currentPage: normalizeText(activePage?.textContent || "") || null,
      totalText: totalText || null,
      results,
      capturedAt: new Date().toISOString()
    };
  }

  const SEARCH_SORTS = {
    relevance: { selector: "#FFD", label: "相关度" },
    publishedAt: { selector: "#PT", label: "发表时间" },
    citations: { selector: "#CF", label: "被引" },
    downloads: { selector: "#DFR", label: "下载" },
    comprehensive: { selector: "#ZH", label: "综合" }
  };

  function sortSearchResults(options = {}) {
    if (!window.location.pathname.includes(SEARCH_PAGE_PATH)) {
      throw new Error("当前不是 CNKI 检索结果页，不能执行排序。");
    }
    const sortBy = normalizeText(options.sortBy || "");
    const target = SEARCH_SORTS[sortBy];
    if (!target) {
      throw new Error("sortBy 仅支持 relevance、publishedAt、citations、downloads、comprehensive。");
    }
    const element = document.querySelector(target.selector);
    if (!(element instanceof HTMLElement) || !isVisible(element)) {
      throw new Error(`当前页面未识别到“${target.label}”排序控件。`);
    }

    // 始终调用知网页面已有的排序控件，不拼接排序接口或请求地址。
    element.click();
    return {
      sorted: true,
      sortBy,
      sortLabel: target.label,
      clickedAt: new Date().toISOString(),
      note: "已点击 CNKI 检索页自身的排序控件；服务端将等待结果表刷新后读取列表。"
    };
  }

  function submitSearch(options = {}) {
    const query = normalizeText(options.query || "");
    if (query.length < 1 || query.length > 100) {
      throw new Error("检索词长度必须为 1 到 100 个字符。");
    }
    if (!window.location.pathname.includes(SEARCH_PAGE_PATH)) {
      throw new Error("请先打开 CNKI kns8s 检索页，再执行受控检索。");
    }

    const input = getSearchInput();
    const searchButton = document.querySelector("input.search-btn[type='button']");
    if (!(searchButton instanceof HTMLElement)) {
      throw new Error("当前页面未识别到 CNKI 检索按钮。");
    }

    setNativeInputValue(input, query);
    input.focus();
    searchButton.click();

    return {
      submitted: true,
      query,
      submittedAt: new Date().toISOString(),
      note: "已使用当前页面的检索框和检索按钮提交。页面完成加载后，服务会读取当前结果列表。"
    };
  }

  function describeDownloadElement(element, index) {
    const text = normalizeText(element.innerText || element.textContent || element.getAttribute("aria-label") || "");
    const href = element instanceof HTMLAnchorElement ? element.href : null;
    const tagName = element.tagName.toLowerCase();
    const className = typeof element.className === "string" ? element.className.slice(0, 240) : "";
    const score = DOWNLOAD_TEXT_PATTERN.test(text) ? 100 : (GENERIC_DOWNLOAD_PATTERN.test(text) ? 20 : 0);

    return {
      optionId: `download-option-${index}`,
      text: text.slice(0, 200),
      href,
      tagName,
      id: element.id || null,
      className,
      score
    };
  }

  function listDownloadElements() {
    const elements = [...document.querySelectorAll("a, button, [role='button'], input[type='button'], input[type='submit']")]
      .filter(isVisible)
      .filter((element) => {
        const text = normalizeText(element.innerText || element.textContent || element.getAttribute("aria-label") || element.value || "");
        return DOWNLOAD_TEXT_PATTERN.test(text) || GENERIC_DOWNLOAD_PATTERN.test(text);
      })
      .map((element, index) => ({ element, description: describeDownloadElement(element, index) }))
      .sort((left, right) => right.description.score - left.description.score);

    return elements;
  }

  function getDownloadOptions() {
    const candidates = listDownloadElements().slice(0, 30).map(({ description }) => description);
    return {
      url: window.location.href,
      title: document.title,
      candidates,
      detectedAt: new Date().toISOString()
    };
  }

  function clickPdfDownload() {
    const candidates = listDownloadElements();
    const preferred = candidates.find(({ description }) => description.score >= 100);
    if (!preferred) {
      return {
        clicked: false,
        reason: "当前页面未检测到可见的 PDF 下载按钮。",
        ...getDownloadOptions()
      };
    }

    const { element, description } = preferred;
    const beforeUrl = window.location.href;
    element.click();
    return {
      clicked: true,
      clickedOption: description,
      beforeUrl,
      clickedAt: new Date().toISOString(),
      note: "已按当前页面的正常按钮触发下载；若网站跳转至格式选择、登录、权限或验证码页面，插件将不会绕过该页面。"
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    try {
      switch (message?.type) {
        case "CNKI_GET_PAGE_SNAPSHOT":
          sendResponse(getPageSnapshot());
          break;
        case "CNKI_GET_PAGE_DOM":
          sendResponse(getPageDom(message.options));
          break;
        case "CNKI_GET_SEARCH_RESULTS":
          sendResponse(extractSearchResults(message.options));
          break;
        case "CNKI_SUBMIT_SEARCH":
          sendResponse(submitSearch(message.options));
          break;
        case "CNKI_SORT_SEARCH_RESULTS":
          sendResponse(sortSearchResults(message.options));
          break;
        case "CNKI_GET_DOWNLOAD_OPTIONS":
          sendResponse(getDownloadOptions());
          break;
        case "CNKI_CLICK_PDF_DOWNLOAD":
          sendResponse(clickPdfDownload());
          break;
        default:
          return;
      }
    } catch (error) {
      sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : "CNKI 页面动作失败。"
      });
    }
  });
})();
