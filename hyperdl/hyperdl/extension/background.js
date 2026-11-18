const DEFAULT_SERVER = "http://127.0.0.1:8787";
const NMH_HOST = "com.genniusdm.host";
const tabMedia = new Map(); // tabId -> Array<{ url, kind, size, filename, referer }>
const bypassUrls = new Set();

let registeredId = false;

function registerExtensionIdOnce(baseUrl, apiKey) {
  if (registeredId || !chrome.runtime || !chrome.runtime.id) return;
  registeredId = true;
  const base = (baseUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;
  fetch(`${base}/api/extension/register-id`, {
    method: "POST",
    headers,
    body: JSON.stringify({ extension_id: chrome.runtime.id })
  }).catch(() => {
    registeredId = false;
  });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["autoCapture", "minSizeKb", "ignoredHosts"], (res) => {
    const updates = {};
    if (typeof res.autoCapture !== "boolean") updates.autoCapture = true;
    if (typeof res.minSizeKb !== "number") updates.minSizeKb = 256;
    if (typeof res.ignoredHosts !== "string") updates.ignoredHosts = "";
    if (Object.keys(updates).length) chrome.storage.local.set(updates);
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
    apiKey: "",
    minSizeKb: 256,
    ignoredHosts: ""
  });
}

function isIgnoredUrl(targetUrl, ignoredHostsRaw) {
  if (!ignoredHostsRaw) return false;
  try {
    const host = new URL(targetUrl).hostname.toLowerCase();
    const parts = ignoredHostsRaw
      .split(/[\s,;]+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    return parts.some((p) => host === p || host.endsWith("." + p));
  } catch {
    return false;
  }
}

async function getCookieHeader(targetUrl) {
  if (!chrome.cookies || typeof chrome.cookies.getAll !== "function") return "";
  try {
    const list = await chrome.cookies.getAll({ url: targetUrl });
    if (!list || !list.length) return "";
    return list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    return "";
  }
}

async function isServerReachable(baseUrl) {
  const base = (baseUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 650);
  try {
    const r = await fetch(`${base}/api/ping`, { signal: ctrl.signal });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function sendNativeWake() {
  return new Promise((resolve) => {
    if (!chrome.runtime || typeof chrome.runtime.sendNativeMessage !== "function") {
      resolve(false);
      return;
    }
    try {
      chrome.runtime.sendNativeMessage(NMH_HOST, { action: "ensure_running" }, (resp) => {
        if (chrome.runtime.lastError) {
          resolve(false);
          return;
        }
        resolve(Boolean(resp && resp.ok));
      });
    } catch {
      resolve(false);
    }
  });
}

async function ensureServerRunning(baseUrl, apiKey) {
  if (await isServerReachable(baseUrl)) {
    registerExtensionIdOnce(baseUrl, apiKey);
    return true;
  }
  const woke = await sendNativeWake();
  if (woke) {
    registerExtensionIdOnce(baseUrl, apiKey);
    return true;
  }
  return false;
}

async function sendToGenniusDM(payload) {
  const cfg = await getConfig();
  const base = (cfg.serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
  const headers = { "Content-Type": "application/json" };
  if (cfg.apiKey) headers["X-API-Key"] = cfg.apiKey;

  const ready = await ensureServerRunning(base, cfg.apiKey);
  if (!ready) {
    return {
      ok: false,
      status: 0,
      error: "Não foi possível iniciar o GenniusDM.exe automaticamente. Abre o GenniusDM.exe uma vez."
    };
  }

  // Enrich HTTP/HLS download with browser Cookie & User-Agent so authenticated links work seamlessly
  const enriched = { ...payload };
  const dlHeaders = { ...(enriched.headers || {}) };
  if (!dlHeaders["Cookie"] && enriched.url) {
    const cookieStr = await getCookieHeader(enriched.url);
    if (cookieStr) dlHeaders["Cookie"] = cookieStr;
  }
  if (Object.keys(dlHeaders).length) enriched.headers = dlHeaders;
  if (!enriched.user_agent && typeof navigator !== "undefined" && navigator.userAgent) {
    enriched.user_agent = navigator.userAgent;
  }

  const resp = await fetch(`${base}/api/downloads/add`, {
    method: "POST",
    headers,
    body: JSON.stringify(enriched)
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

  const ready = await ensureServerRunning(base, cfg.apiKey);
  if (!ready) {
    return { ok: false, data: { error: "Gennius DM offline" } };
  }

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
    if (kind !== "HLS" && size > 0 && size < 512 * 1024) return;

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
      chrome.tabs.sendMessage(details.tabId, { type: "tabMediaUpdated", items: list }).catch(() => {});
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
    width: 490,
    height: 445,
    focused: true
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.linkUrl || info.srcUrl;
  if (!targetUrl) return;
  const cfg = await getConfig();
  ensureServerRunning(cfg.serverUrl, cfg.apiKey).catch(() => {});
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
  if (bypassUrls.has(targetUrl) || bypassUrls.has(item.url)) {
    bypassUrls.delete(targetUrl);
    bypassUrls.delete(item.url);
    return;
  }
  if (targetUrl.startsWith("http://127.0.0.1:8787") || targetUrl.startsWith("http://localhost:8787")) return;
  if (isIgnoredUrl(targetUrl, cfg.ignoredHosts)) return;

  const totalBytes = item.totalBytes || item.fileSize || 0;
  const minBytes = Math.max(0, Number(cfg.minSizeKb || 0)) * 1024;
  if (totalBytes > 0 && minBytes > 0 && totalBytes < minBytes) {
    return;
  }

  let alive = await isServerReachable(cfg.serverUrl);
  if (!alive) {
    try {
      await chrome.downloads.pause(item.id);
    } catch {}
    alive = await ensureServerRunning(cfg.serverUrl, cfg.apiKey);
    if (!alive) {
      try {
        await chrome.downloads.resume(item.id);
      } catch {}
      return;
    }
  } else {
    registerExtensionIdOnce(cfg.serverUrl, cfg.apiKey);
  }

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
    totalBytes,
    referer: item.referrer || ""
  });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;
  if (msg.type === "getTabMedia") {
    const tid = msg.tabId || (sender.tab && sender.tab.id);
    sendResponse({ items: tid ? tabMedia.get(tid) || [] : [] });
    return true;
  }
  if (msg.type === "sendUrl") {
    const reqHeaders = {};
    const ref = msg.referer || (sender.tab && sender.tab.url) || "";
    if (ref && /^https?:\/\//i.test(ref)) reqHeaders["Referer"] = ref;
    const payload = {
      url: msg.url,
      filename: msg.filename || undefined,
      save_subdir: msg.save_subdir || undefined,
      max_connections: msg.max_connections || undefined,
      queue_id: msg.queue_id || undefined,
      media_mode: msg.media_mode || undefined,
      container: msg.container || undefined,
      quality: msg.quality || undefined,
      audio_bitrate: msg.audio_bitrate || undefined,
      subtitles: Boolean(msg.subtitles),
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
  if (msg.type === "browserFallback" && msg.url) {
    bypassUrls.add(msg.url);
    setTimeout(() => bypassUrls.delete(msg.url), 15000);
    chrome.downloads.download({ url: msg.url, filename: msg.filename || undefined }, () => {
      sendResponse({ ok: true });
    });
    return true;
  }
  return false;
});
