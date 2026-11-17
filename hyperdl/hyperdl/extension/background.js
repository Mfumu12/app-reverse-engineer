const DEFAULT_SERVER = "http://127.0.0.1:8787";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "hyperdl-download-link",
    title: "Download with HyperDL",
    contexts: ["link", "video", "audio"]
  });
});

async function getConfig() {
  const res = await chrome.storage.local.get({ serverUrl: DEFAULT_SERVER, autoCapture: false, apiKey: "" });
  return res;
}

async function sendToHyperDL(url, filename) {
  const cfg = await getConfig();
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;
  const resp = await fetch(`${cfg.serverUrl.replace(/\/$/, "")}/api/downloads/add`, {
    method: "POST",
    headers,
    body: JSON.stringify({ url, filename: filename || undefined })
  });
  return resp.ok;
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  const targetUrl = info.linkUrl || info.srcUrl;
  if (targetUrl) {
    await sendToHyperDL(targetUrl);
  }
});

chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getConfig();
  if (!cfg.autoCapture) return;
  if (!item.url || !item.url.startsWith("http")) return;
  try {
    const ok = await sendToHyperDL(item.url, item.filename ? item.filename.split(/[\\/]/).pop() : undefined);
    if (ok) {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    }
  } catch (e) {
    console.warn("HyperDL capture failed:", e);
  }
});
