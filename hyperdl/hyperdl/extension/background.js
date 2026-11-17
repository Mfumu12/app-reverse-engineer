const DEFAULT_SERVER = "http://127.0.0.1:8787";
const tabMedia = new Map(); // tabId -> Array<{ url, type, size, filename, referer }>

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "genniusdm-download-link",
      title: "Transferir com Gennius DM",
      contexts: ["link", "video", "audio"]
    });
  });
});

async function getConfig() {
  return chrome.storage.local.get({
    serverUrl: DEFAULT_SERVER,
    autoCapture: false,
    apiKey: ""
  });
}

async function sendToGenniusDM(url, filename, referer) {
  const cfg = await getConfig();
  const base = (cfg.serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;
  const reqHeaders = {};
  if (referer && /^https?:\/\//i.test(referer)) reqHeaders["Referer"] = referer;

  const resp = await fetch(`${base}/api/downloads/add`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      url,
      filename: filename || undefined,
      headers: Object.keys(reqHeaders).length ? reqHeaders : undefined
    })
  });
  return resp.ok;
}

function guessFilename(url) {
  try {
    const u = new URL(url);
    const seg = u.pathname.split("/").filter(Boolean).pop();
    return seg ? decodeURIComponent(seg).slice(0, 80) : u.hostname;
  } catch {
    return url.slice(0, 60);
  }
}

function detectMediaKind(url, contentType) {
  const lower = (url || "").toLowerCase();
  const ct = (contentType || "").toLowerCase();
  if (lower.includes(".m3u8") || ct.includes("mpegurl")) return "HLS";
  if (ct.startsWith("video/") || /\.(mp4|mkv|webm|mov)(\?|$)/i.test(lower)) return "VIDEO";
  if (ct.startsWith("audio/") || /\.(mp3|flac|aac|m4a|ogg)(\?|$)/i.test(lower)) return "AUDIO";
  return null;
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0 || !details.url || !details.url.startsWith("http")) return;
    if (details.url.startsWith("http://127.0.0.1:8787") || details.url.startsWith("http://localhost:8787")) return;

    let ct = "";
    let size = 0;
    for (const h of details.responseHeaders || []) {
      const name = (h.name || "").toLowerCase();
      if (name === "content-type") ct = h.value || "";
      if (name === "content-length") size = parseInt(h.value || "0", 10) || 0;
    }

    const kind = detectMediaKind(details.url, ct);
    if (!kind) return;
    // Ignore tiny video/audio tracker fragments unless HLS playlist
    if (kind !== "HLS" && size > 0 && size < 256 * 1024) return;

    const list = tabMedia.get(details.tabId) || [];
    if (!list.some((m) => m.url === details.url)) {
      list.unshift({
        url: details.url,
        kind,
        size,
        filename: guessFilename(details.url),
        referer: details.initiator || ""
      });
      if (list.length > 12) list.pop();
      tabMedia.set(details.tabId, list);
      chrome.action.setBadgeText({ tabId: details.tabId, text: String(list.length) });
      chrome.action.setBadgeBackgroundColor({ tabId: details.tabId, color: "#2563eb" });
    }
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

chrome.tabs.onRemoved.addListener((tabId) => {
  tabMedia.delete(tabId);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading") {
    tabMedia.delete(tabId);
    chrome.action.setBadgeText({ tabId, text: "" });
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.linkUrl || info.srcUrl;
  if (targetUrl) {
    try {
      await sendToGenniusDM(targetUrl, undefined, tab && tab.url);
    } catch (e) {
      console.warn("Gennius DM menu send failed:", e);
    }
  }
});

chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getConfig();
  if (!cfg.autoCapture) return;
  if (!item.url || !item.url.startsWith("http")) return;
  if (item.url.startsWith("http://127.0.0.1:8787") || item.url.startsWith("http://localhost:8787")) return;

  try {
    const fname = item.filename ? item.filename.split(/[\\/]/).pop() : undefined;
    const ok = await sendToGenniusDM(item.url, fname, item.referrer);
    if (ok) {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    }
  } catch (e) {
    console.warn("Gennius DM auto-capture failed:", e);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "getTabMedia") {
    sendResponse({ items: tabMedia.get(msg.tabId) || [] });
    return true;
  }
  if (msg && msg.type === "sendUrl") {
    sendToGenniusDM(msg.url, msg.filename, msg.referer)
      .then((ok) => sendResponse({ ok }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
