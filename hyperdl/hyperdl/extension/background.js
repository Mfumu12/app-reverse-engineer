const DEFAULT_SERVER = "http://127.0.0.1:8787";
const tabMedia = new Map(); // tabId -> Array<{ url, kind, size, filename, referer }>

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["autoCapture"], (res) => {
    if (typeof res.autoCapture !== "boolean") {
      chrome.storage.local.set({ autoCapture: true });
    }
  });
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
    autoCapture: true,
    apiKey: ""
  });
}

async function isServerReachable(baseUrl) {
  const base = (baseUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 700);
  try {
    const r = await fetch(`${base}/api/ping`, { signal: ctrl.signal });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function sendToGenniusDM(payload) {
  const cfg = await getConfig();
  const base = (cfg.serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;

  const resp = await fetch(`${base}/api/downloads/add`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload)
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.ok) {
    fetch(`${base}/api/desktop/focus`, { method: "POST", headers }).catch(() => {});
  }
  return { ok: resp.ok, status: resp.status, data };
}

async function probeYoutubeOnServer(url) {
  const cfg = await getConfig();
  const base = (cfg.serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;

  const resp = await fetch(`${base}/api/youtube/probe`, {
    method: "POST",
    headers,
    body: JSON.stringify({ url })
  });
  const data = await resp.json().catch(() => ({}));
  return { ok: resp.ok, data };
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

function openConfirmDialog({ url, filename, totalBytes, referer }) {
  const params = new URLSearchParams({
    url: url || "",
    filename: filename || "",
    size: String(totalBytes || 0),
    referer: referer || ""
  });
  chrome.windows.create({
    url: chrome.runtime.getURL(`dialog.html?${params.toString()}`),
    type: "popup",
    width: 480,
    height: 390,
    focused: true
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.linkUrl || info.srcUrl;
  if (!targetUrl) return;
  openConfirmDialog({
    url: targetUrl,
    filename: guessFilename(targetUrl),
    totalBytes: 0,
    referer: tab && tab.url ? tab.url : ""
  });
});

chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getConfig();
  if (!cfg.autoCapture) return;
  const targetUrl = item.finalUrl || item.url;
  if (!targetUrl || !targetUrl.startsWith("http")) return;
  if (targetUrl.startsWith("http://127.0.0.1:8787") || targetUrl.startsWith("http://localhost:8787")) return;

  // Only intercept if GenniusDM desktop server is actually running
  const alive = await isServerReachable(cfg.serverUrl);
  if (!alive) return;

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {
    console.warn("Cancel browser download warning:", e);
  }

  const fname = item.filename ? item.filename.split(/[\\/]/).pop() : guessFilename(targetUrl);
  openConfirmDialog({
    url: targetUrl,
    filename: fname,
    totalBytes: item.totalBytes || item.fileSize || 0,
    referer: item.referrer || ""
  });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;
  if (msg.type === "getTabMedia") {
    sendResponse({ items: tabMedia.get(msg.tabId) || [] });
    return true;
  }
  if (msg.type === "sendUrl") {
    const reqHeaders = {};
    if (msg.referer && /^https?:\/\//i.test(msg.referer)) reqHeaders["Referer"] = msg.referer;
    const payload = {
      url: msg.url,
      filename: msg.filename || undefined,
      max_connections: msg.max_connections || undefined,
      queue_id: msg.queue_id || undefined,
      media_mode: msg.media_mode || undefined,
      container: msg.container || undefined,
      quality: msg.quality || undefined,
      audio_bitrate: msg.audio_bitrate || undefined,
      download_playlist: Boolean(msg.download_playlist),
      headers: Object.keys(reqHeaders).length ? reqHeaders : undefined
    };
    sendToGenniusDM(payload)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  if (msg.type === "probeYoutube") {
    probeYoutubeOnServer(msg.url)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
