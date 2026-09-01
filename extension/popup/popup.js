const readButton = document.querySelector("#read-page");
const statusNode = document.querySelector("#status");
const resultNode = document.querySelector("#result");

function setStatus(message, isError = false) {
  statusNode.textContent = message;
  statusNode.classList.toggle("error", isError);
}

function showSnapshot(snapshot) {
  document.querySelector("#page-title").textContent = snapshot.title || "（无标题）";
  document.querySelector("#page-url").textContent = snapshot.url;
  document.querySelector("#page-ready-state").textContent = snapshot.readyState;
  document.querySelector("#page-text-length").textContent = String(snapshot.textLength);
  document.querySelector("#page-preview").textContent = snapshot.textPreview || "（页面没有可读正文）";
  resultNode.hidden = false;
}

readButton.addEventListener("click", async () => {
  readButton.disabled = true;
  resultNode.hidden = true;
  setStatus("正在读取当前标签页…");

  try {
    const response = await chrome.runtime.sendMessage({ type: "CNKI_GET_ACTIVE_PAGE" });
    if (!response?.ok) {
      setStatus(response?.message || "页面操作失败。", true);
      return;
    }

    showSnapshot(response.data);
    setStatus("页面信息已读取。", false);
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "页面操作失败。", true);
  } finally {
    readButton.disabled = false;
  }
});
