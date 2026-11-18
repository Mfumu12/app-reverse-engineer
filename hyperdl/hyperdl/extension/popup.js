const serverInput = document.getElementById("serverUrl");
const keyInput = document.getElementById("apiKey");
const capInput = document.getElementById("autoCapture");
const minSizeInput = document.getElementById("minSizeKb");
const ignoredInput = document.getElementById("ignoredHosts");
const statusEl = document.getElementById("statusMsg");
const connPill = document.getElementById("connPill");
const quickUrl = document.getElementById("quickUrl");
const mediaWrap = document.getElementById("mediaWrap");
const mediaList = document.getElementById("mediaList");
const ytWrap = document.getElementById("ytWrap");
const ytPageTitle = document.getElementById("ytPageTitle");
const ytMode = document.getElementById("ytMode");
const ytFormat = document.getElementById("ytFormat");
const ytQuality = document.getElementById("ytQuality");

let activeTabUrl = "";

function isYoutubeUrl(u) {
  try {
    const h = new URL(u).hostname.toLowerCase();
    return /(^|\.)(youtube\.com|youtu\.be|music\.youtube\.com)$/.test(h);
  } catch {
    return false;
  }
}

function updateYtOptions() {
  if (ytMode.value === "audio") {
    ytFormat.innerHTML = `<option value="mp3">MP3</option><option value="m4a">M4A</option><option value="opus">Opus</option>`;
    ytQuality.innerHTML = `<option value="320">320 kbps</option><option value="192">192 kbps</option><option value="128">128 kbps</option>`;
  } else {
    ytFormat.innerHTML = `<option value="mp4">MP4</option><option value="mkv">MKV</option><option value="webm">WebM</option>`;
    ytQuality.innerHTML = `<option value="best">Melhor disponível</option><option value="2160">2160p (4K)</option><option value="1080">1080p (Full HD)</option><option value="720">720p (HD)</option><option value="480">480p</option><option value="360">360p</option>`;
  }
}
ytMode.onchange = updateYtOptions;

async function checkServer(baseUrl) {
  const base = (baseUrl || "http://127.0.0.1:8787").trim().replace(/\/$/, "");
  try {
    const r = await fetch(`${base}/api/ping`);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    connPill.className = "pill ok";
    connPill.textContent = `${j.name || "Gennius DM"} v${j.version}`;
    return true;
  } catch (e) {
    connPill.className = "pill err";
    connPill.textContent = "Offline";
    return false;
  }
}

chrome.storage.local.get(
  { serverUrl: "http://127.0.0.1:8787", apiKey: "", autoCapture: true, minSizeKb: 256, ignoredHosts: "" },
  (res) => {
    serverInput.value = res.serverUrl;
    keyInput.value = res.apiKey;
    capInput.checked = res.autoCapture;
    minSizeInput.value = String(res.minSizeKb ?? 256);
    ignoredInput.value = res.ignoredHosts || "";
    checkServer(res.serverUrl);
  }
);

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs && tabs[0];
  if (!tab) return;
  activeTabUrl = tab.url || "";

  if (isYoutubeUrl(activeTabUrl)) {
    ytWrap.style.display = "block";
    ytPageTitle.textContent = tab.title || activeTabUrl;
  }

  chrome.runtime.sendMessage({ type: "getTabMedia", tabId: tab.id }, (res) => {
    const items = (res && res.items) || [];
    if (!items.length) return;
    mediaWrap.style.display = "block";
    mediaList.innerHTML = "";
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "media-item";

      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.kind;

      const name = document.createElement("span");
      name.className = "media-name";
      name.textContent = item.filename || item.url;
      name.title = item.url;

      const btn = document.createElement("button");
      btn.textContent = "Baixar";
      btn.onclick = () => {
        chrome.runtime.sendMessage(
          { type: "sendUrl", url: item.url, filename: item.filename, referer: activeTabUrl },
          (resp) => {
            statusEl.style.color = resp && resp.ok ? "#34d399" : "#f87171";
            statusEl.textContent = resp && resp.ok ? "Enviado para o Gennius DM." : "Erro ao enviar stream.";
          }
        );
      };

      row.appendChild(tag);
      row.appendChild(name);
      row.appendChild(btn);
      mediaList.appendChild(row);
    }
  });
});

document.getElementById("ytDownload").onclick = () => {
  const mode = ytMode.value;
  let isPl = false;
  try {
    isPl = new URL(activeTabUrl).searchParams.has("list") && /\/playlist/i.test(activeTabUrl);
  } catch {}
  chrome.runtime.sendMessage(
    {
      type: "sendUrl",
      url: activeTabUrl,
      media_mode: mode,
      container: ytFormat.value,
      quality: mode === "audio" ? "best" : ytQuality.value,
      audio_bitrate: mode === "audio" ? ytQuality.value : "320",
      download_playlist: isPl,
      queue_id: mode === "audio" ? "main" : "video",
      referer: activeTabUrl
    },
    (resp) => {
      statusEl.style.color = resp && resp.ok ? "#34d399" : "#f87171";
      statusEl.textContent = resp && resp.ok
        ? "Download do YouTube iniciado no Gennius DM."
        : (resp && resp.error) || "Erro ao iniciar download.";
    }
  );
};

document.getElementById("sendQuick").onclick = () => {
  const url = quickUrl.value.trim();
  if (!url) return;
  chrome.runtime.sendMessage({ type: "sendUrl", url, referer: activeTabUrl }, (resp) => {
    if (resp && resp.ok) {
      quickUrl.value = "";
      statusEl.style.color = "#34d399";
      statusEl.textContent = "Download adicionado ao Gennius DM.";
    } else {
      statusEl.style.color = "#f87171";
      statusEl.textContent = "Falha: verifica se o GenniusDM.exe está aberto.";
    }
  });
};

document.getElementById("save").onclick = async () => {
  const serverUrl = serverInput.value.trim() || "http://127.0.0.1:8787";
  const apiKey = keyInput.value.trim();
  const autoCapture = capInput.checked;
  const minSizeKb = Number(minSizeInput.value || 256);
  const ignoredHosts = ignoredInput.value.trim();
  await chrome.storage.local.set({ serverUrl, apiKey, autoCapture, minSizeKb, ignoredHosts });
  const ok = await checkServer(serverUrl);
  statusEl.style.color = ok ? "#34d399" : "#f87171";
  statusEl.textContent = ok ? "Configuração guardada e ligada." : "Guardado, mas servidor offline.";
};

document.getElementById("openDash").onclick = () => {
  const base = (serverInput.value.trim() || "http://127.0.0.1:8787").replace(/\/$/, "");
  chrome.tabs.create({ url: base });
};
