(() => {
  const SCRIPT_VERSION = "0.7.8";
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
    // 兼容两种页面渲染态：首次进入检索页时输入框同时带 id="txt_search" 和
    // class="search-input"；但切换文献库 / 翻页后知网会重新渲染这段 DOM，
    // 此时 id 属性会丢失，只剩 class="search-input"（已用真实 DOM 快照核实
    // 全页面唯一一处匹配，不会误选到脚本字符串或其他输入框）。selector 放宽
    // 为「id 匹配 或 class 匹配」，避免因为 id 缺失就报错"未识别到检索框"。
    const input = document.querySelector("#txt_search.search-input")
      || document.querySelector("input.search-input")
      || document.querySelector("#txt_search");
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

    const searchInput = document.querySelector("#txt_search.search-input, input.search-input, #txt_search");
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

  // 主检索字段映射（一框式检索框左侧下拉，`#selectfield` 存当前值）。
  const SEARCH_FIELDS = {
    SU: "主题", TKA: "篇关摘", KY: "关键词", TI: "篇名", FT: "全文",
    AU: "作者", FI: "第一作者", RP: "通讯作者", AF: "作者单位", FU: "基金",
    AB: "摘要", CO: "小标题", RF: "参考文献", CLC: "分类号", LY: "文献来源", DOI: "DOI"
  };

  // 文献库映射（顶部「文献类型」切换，classid 取自页面 a[name=classify]）。
  const LIBRARIES = {
    journal: { classid: "YSTT4HG0", label: "学术期刊" },
    dissertation: { classid: "LSTPFY1C", label: "学位论文" },
    doctor: { classid: "RMJLXHZ3", label: "博士" },
    master: { classid: "JQIRZIYA", label: "硕士" },
    book: { classid: "EMRPGLPA", label: "图书" },
    conference: { classid: "JUP3MUPD", label: "会议" },
    newspaper: { classid: "MPMFIG1A", label: "报纸" },
    almanac: { classid: "HHCPM1F8", label: "年鉴" },
    patent: { classid: "VUDIXAIY", label: "专利" },
    standard: { classid: "WQ0UVIAA", label: "标准" },
    achievement: { classid: "BLZOG7CK", label: "成果" }
  };

  function setSearchField(options = {}) {
    const field = normalizeText(options.field || "").toUpperCase();
    if (!SEARCH_FIELDS[field]) {
      throw new Error(`field 仅支持 ${Object.keys(SEARCH_FIELDS).join("/")}。`);
    }
    const trigger = document.querySelector("div.sort-default");
    if (!(trigger instanceof HTMLElement)) {
      throw new Error("当前页面未识别到检索字段下拉控件。");
    }
    trigger.click();
    const item = document.querySelector(`div.sort-list ul li[data-val="${field}"]`);
    if (!(item instanceof HTMLElement)) {
      throw new Error(`未识别到检索字段 ${field} 的选项。`);
    }
    (item.querySelector("a") || item).click();
    const selectfield = document.querySelector("#selectfield");
    return {
      setField: true,
      field,
      label: SEARCH_FIELDS[field],
      currentValue: selectfield instanceof HTMLInputElement ? selectfield.value : null,
      setAt: new Date().toISOString()
    };
  }

  // 高级检索表单专用字段映射（/kns8s/AdvSearch?type=expert 页面的 dl#gradetxt 多行
  // 字段下拉），比一框式检索多了 TU(导师)/FTU(第一导师)/LY(学位授予单位)/XF(学科专业名称)，
  // 少了 FI(第一作者)/RP(通讯作者)。这是定位"某导师指导的论文"的关键字段，一框式检索框
  // 不支持。
  const ADVANCED_SEARCH_FIELDS = {
    SU: "主题", TKA: "篇关摘", KY: "关键词", TI: "题名", FT: "全文",
    AU: "作者", AF: "作者单位", TU: "导师", FTU: "第一导师", LY: "学位授予单位",
    FU: "基金", AB: "摘要", CO: "目录", RF: "参考文献", CLC: "中图分类号",
    XF: "学科专业名称", DOI: "DOI"
  };
  const ADVANCED_SEARCH_LOGICS = new Set(["AND", "OR", "NOT"]);

  function submitAdvancedSearch(options = {}) {
    const conditions = Array.isArray(options.conditions) ? options.conditions : [];
    if (conditions.length < 1 || conditions.length > 3) {
      throw new Error("conditions 需要 1-3 个检索条件（对应高级检索表单默认渲染的 3 行）。");
    }
    if (!window.location.pathname.includes("/kns8s/AdvSearch")) {
      throw new Error("请先打开 CNKI 高级检索页（AdvSearch）。");
    }

    // AdvSearch 页内部有「高级检索/专业检索/作者发文检索/句子检索」多个 tab 共用同一个 URL，
    // 切换文献库（如从学位论文切到学术期刊）后默认激活的 tab 可能不是「高级检索」（例如学术期刊库
    // 默认落在「专业检索」），此时 dl.inputs-list 的下拉选项尚未按当前库初始化，会导致按 data-val
    // 找字段入口失败。这里先确保「高级检索」tab 处于激活状态。
    const gradeTab = document.querySelector('li[name="gradeSearch"]');
    if (gradeTab instanceof HTMLElement && !gradeTab.classList.contains("active")) {
      gradeTab.click();
    }

    const dl = document.querySelector("dl.inputs-list");
    if (!(dl instanceof HTMLElement)) {
      throw new Error("当前页面未识别到高级检索表单。");
    }
    const rows = [...dl.querySelectorAll(":scope > dd")].filter((dd) => dd.querySelector(".input-box"));
    if (rows.length < conditions.length) {
      throw new Error(`当前表单只有 ${rows.length} 行检索条件，无法容纳 ${conditions.length} 个条件；如需更多行，请先在页面上手动点击「+」增加。`);
    }

    // 表单是页面级持久状态：同一个 AdvSearch 标签页里连续多次调用本工具时，上一次调用未用到的
    // 行（例如上次填了 2 行，这次只传 1 个条件）会残留旧的检索词，跟着这次提交一起生效，
    // 导致结果被静默追加一个调用方不知道的过滤条件。这里先清空所有"本次不会填写"的行。
    rows.forEach((row, index) => {
      if (index < conditions.length) {
        return;
      }
      const staleInput = row.querySelector('input[type="text"][data-tipid]');
      if (staleInput instanceof HTMLInputElement && staleInput.value) {
        setNativeInputValue(staleInput, "");
      }
    });

    const applied = conditions.map((cond, index) => {
      const field = normalizeText(cond?.field || "").toUpperCase();
      const value = normalizeText(cond?.value || "");
      if (!ADVANCED_SEARCH_FIELDS[field]) {
        throw new Error(`第 ${index + 1} 个条件的 field 仅支持 ${Object.keys(ADVANCED_SEARCH_FIELDS).join("/")}。`);
      }
      if (!value) {
        throw new Error(`第 ${index + 1} 个条件缺少检索词 value。`);
      }

      const row = rows[index];
      const fieldTrigger = row.querySelector(".input-box .sort.reopt .sort-default");
      const fieldItem = row.querySelector(`.input-box .sort-list ul li[data-val="${field}"]`);
      if (!(fieldTrigger instanceof HTMLElement) || !(fieldItem instanceof HTMLElement)) {
        throw new Error(`第 ${index + 1} 行未识别到字段「${field}」的切换入口。`);
      }
      fieldTrigger.click();
      (fieldItem.querySelector("a") || fieldItem).click();

      const input = row.querySelector('input[type="text"][data-tipid]');
      if (!(input instanceof HTMLInputElement)) {
        throw new Error(`第 ${index + 1} 行未识别到检索词输入框。`);
      }
      setNativeInputValue(input, value);

      let logic = null;
      if (index > 0) {
        logic = normalizeText(cond?.logic || "AND").toUpperCase();
        if (!ADVANCED_SEARCH_LOGICS.has(logic)) {
          throw new Error(`第 ${index + 1} 个条件的 logic 仅支持 AND/OR/NOT。`);
        }
        const logicTrigger = row.querySelector(".sort.logical .sort-default");
        const logicOption = row.querySelector(`.sort.logical .sort-list a[value="${logic}"]`);
        if (logicTrigger instanceof HTMLElement && logicOption instanceof HTMLElement) {
          logicTrigger.click();
          logicOption.click();
        }
      }

      return { field, label: ADVANCED_SEARCH_FIELDS[field], value, logic };
    });

    const searchButton = document.querySelector("input.btn-search[type='button']");
    if (!(searchButton instanceof HTMLElement)) {
      throw new Error("当前页面未识别到高级检索的检索按钮。");
    }
    searchButton.click();

    return {
      submitted: true,
      conditions: applied,
      submittedAt: new Date().toISOString(),
      note: "已通过高级检索表单填写字段/检索词/AND-OR-NOT 连接符并点击检索按钮提交。"
    };
  }

  function setLibrary(options = {}) {
    const key = normalizeText(options.library || "").toLowerCase();
    const lib = LIBRARIES[key];
    if (!lib) {
      throw new Error(`library 仅支持 ${Object.keys(LIBRARIES).join("/")}。`);
    }
    const anchor = document.querySelector(`ul.doctype-menus a[name="classify"][classid="${lib.classid}"]`);
    if (!(anchor instanceof HTMLAnchorElement)) {
      throw new Error(`当前页面未识别到文献库「${lib.label}」的切换入口。`);
    }
    anchor.click();
    return {
      setLibrary: true,
      library: key,
      classid: lib.classid,
      label: lib.label,
      setAt: new Date().toISOString(),
      note: "已点击文献库切换入口，页面会按所选库重新加载结果。"
    };
  }

  function turnPage(options = {}) {
    const pageRaw = options.page;
    const direction = normalizeText(options.direction || "").toLowerCase();
    const mark = document.querySelector(".countPageMark[data-pagenum]");
    const totalPages = mark ? (Number.parseInt(mark.getAttribute("data-pagenum"), 10) || null) : null;
    const curLink = document.querySelector("div.pagesnums a.cur");
    const currentPage = curLink ? (Number.parseInt(curLink.getAttribute("data-curpage"), 10) || null) : null;

    if (pageRaw !== undefined && pageRaw !== null && pageRaw !== "") {
      const page = Number(pageRaw);
      if (!Number.isInteger(page) || page < 1) {
        throw new Error("page 必须是 >= 1 的整数。");
      }
      const link = document.querySelector(`div.pagesnums a[data-curpage="${page}"]`);
      if (link instanceof HTMLAnchorElement) {
        link.click();
        return { turned: true, via: "page", targetPage: page, currentPage, totalPages, turnedAt: new Date().toISOString() };
      }
      throw new Error(`页码 ${page} 不在当前可见页码范围，请用 direction=next/prev 逐步翻页。`);
    }

    if (direction === "next") {
      const nextBtn = document.querySelector("#Page_next_top");
      if (nextBtn instanceof HTMLElement) {
        nextBtn.click();
        return { turned: true, via: "next", targetPage: currentPage ? currentPage + 1 : null, currentPage, totalPages, turnedAt: new Date().toISOString() };
      }
      throw new Error("当前页面未识别到下一页按钮。");
    }
    if (direction === "prev") {
      if (!currentPage || currentPage <= 1) {
        throw new Error("当前已是第 1 页，无法上一页。");
      }
      const prevLink = document.querySelector(`div.pagesnums a[data-curpage="${currentPage - 1}"]`);
      if (prevLink instanceof HTMLAnchorElement) {
        prevLink.click();
        return { turned: true, via: "prev", targetPage: currentPage - 1, currentPage, totalPages, turnedAt: new Date().toISOString() };
      }
      throw new Error("当前页面未识别到上一页按钮。");
    }

    throw new Error("翻页需要 page（页码）或 direction（next/prev）。");
  }

  function expandFacetGroup(groupid) {
    const dl = document.querySelector(`dl[groupid="${groupid}"]`);
    if (!dl) {
      return null;
    }
    if ((dl.className || "").includes("is-up-fold")) {
      const dt = dl.querySelector("dt.tit");
      if (dt instanceof HTMLElement) {
        dt.click();
      }
    }
    return dl;
  }

  function waitForFacetItems(groupid, timeoutMs = 8000) {
    return new Promise((resolve) => {
      const start = Date.now();
      const poll = () => {
        const dd = document.querySelector(`dl[groupid="${groupid}"] > dd`);
        const boxes = dd ? [...dd.querySelectorAll("ul li input[type=checkbox]")] : [];
        if (boxes.length > 0) {
          resolve(boxes);
          return;
        }
        if (Date.now() - start >= timeoutMs) {
          resolve([]);
          return;
        }
        setTimeout(poll, 200);
      };
      poll();
    });
  }

  async function getFilters(options = {}) {
    const rawGroups = options.groups;
    const expandGroups = Array.isArray(rawGroups) ? rawGroups : (rawGroups ? [rawGroups] : []);
    for (const group of expandGroups) {
      expandFacetGroup(String(group));
    }
    if (expandGroups.length > 0) {
      await Promise.all(expandGroups.map((group) => waitForFacetItems(String(group))));
    }

    const filters = [...document.querySelectorAll("dl[groupid]")].map((dl) => {
      const groupid = dl.getAttribute("groupid");
      const dt = dl.querySelector("dt.tit");
      const title = dt?.getAttribute("groupitem") || normalizeText(dt?.querySelector("b")?.textContent || "");
      const folded = (dl.className || "").includes("is-up-fold");
      const items = [...(dl.querySelectorAll("ul li input[type=checkbox]") || [])]
        .map((cb) => ({
          value: cb.getAttribute("value") || null,
          text: cb.getAttribute("text") || cb.getAttribute("title") || null,
          count: normalizeText(cb.parentElement?.querySelector("span")?.textContent || "")
        }))
        .filter((item) => item.value);
      return { groupid, title, folded, itemCount: items.length, items };
    });

    return { url: window.location.href, filters, capturedAt: new Date().toISOString() };
  }

  async function applyFilter(options = {}) {
    const groupid = String(options.group || options.groupid || "").trim();
    if (!groupid) {
      throw new Error("缺少筛选维度 group。");
    }
    const rawValues = options.values;
    const values = Array.isArray(rawValues) ? rawValues.map(String) : (rawValues ? [String(rawValues)] : []);
    if (values.length < 1) {
      throw new Error("缺少筛选值 values。");
    }

    expandFacetGroup(groupid);
    await waitForFacetItems(groupid);

    const dl = document.querySelector(`dl[groupid="${groupid}"]`);
    const checked = [];
    for (const value of values) {
      const cb = dl?.querySelector(`input[type=checkbox][value="${value}"]`);
      if (!cb) {
        continue;
      }
      if (!cb.checked) {
        cb.click();
      }
      checked.push({ value, text: cb.getAttribute("text") || cb.getAttribute("title") || value });
    }
    if (checked.length < 1) {
      throw new Error(`未在 ${groupid} 下找到可选值 ${values.join("/")}。`);
    }

    const submitBtn = document.querySelector("a.btn-submit");
    if (submitBtn instanceof HTMLElement) {
      submitBtn.click();
    } else if (typeof window.mutiSelectedGroup === "function") {
      window.mutiSelectedGroup();
    }

    return {
      applied: true,
      group: groupid,
      checked,
      submittedAt: new Date().toISOString(),
      note: "已勾选筛选值并提交，结果表刷新后读取。"
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

  const KNOWN_MESSAGE_TYPES = [
    "CNKI_GET_PAGE_SNAPSHOT",
    "CNKI_GET_PAGE_DOM",
    "CNKI_GET_SEARCH_RESULTS",
    "CNKI_SUBMIT_SEARCH",
    "CNKI_SORT_SEARCH_RESULTS",
    "CNKI_SET_SEARCH_FIELD",
    "CNKI_SET_LIBRARY",
    "CNKI_TURN_PAGE",
    "CNKI_GET_FILTERS",
    "CNKI_APPLY_FILTER",
    "CNKI_SUBMIT_ADVANCED_SEARCH",
    "CNKI_GET_DOWNLOAD_OPTIONS",
    "CNKI_CLICK_PDF_DOWNLOAD"
  ];

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!KNOWN_MESSAGE_TYPES.includes(message?.type)) {
      return;
    }

    const handle = async () => {
      switch (message.type) {
        case "CNKI_GET_PAGE_SNAPSHOT":
          return getPageSnapshot();
        case "CNKI_GET_PAGE_DOM":
          return getPageDom(message.options);
        case "CNKI_GET_SEARCH_RESULTS":
          return extractSearchResults(message.options);
        case "CNKI_SUBMIT_SEARCH":
          return submitSearch(message.options);
        case "CNKI_SORT_SEARCH_RESULTS":
          return sortSearchResults(message.options);
        case "CNKI_SET_SEARCH_FIELD":
          return setSearchField(message.options);
        case "CNKI_SET_LIBRARY":
          return setLibrary(message.options);
        case "CNKI_TURN_PAGE":
          return turnPage(message.options);
        case "CNKI_GET_FILTERS":
          return await getFilters(message.options);
        case "CNKI_APPLY_FILTER":
          return await applyFilter(message.options);
        case "CNKI_SUBMIT_ADVANCED_SEARCH":
          return submitAdvancedSearch(message.options);
        case "CNKI_GET_DOWNLOAD_OPTIONS":
          return getDownloadOptions();
        case "CNKI_CLICK_PDF_DOWNLOAD":
          return clickPdfDownload();
        default:
          return null;
      }
    };

    handle()
      .then((result) => sendResponse(result))
      .catch((error) => {
        sendResponse({
          ok: false,
          error: error instanceof Error ? error.message : "CNKI 页面动作失败。"
        });
      });
    return true;
  });
})();
