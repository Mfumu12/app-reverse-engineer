/* Gennius DM — In-page YouTube downloader + Universal HLS/Video stream sniffer widget */
(() => {
  if (window.__genniusDmInjected) return;
  window.__genniusDmInjected = true;

  const YT_HOST_ID = "gennius-dm-yt-host";
  const MEDIA_HOST_ID = "gennius-dm-media-host";
  let lastUrl = location.href;
  let currentMode = "video";
  let probeCache = null;
  let mediaDismissed = false;
  let latestStreams = [];

  function isYoutubeDomain() {
    return /(^|\.)(youtube\.com|youtu\.be|music\.youtube\.com)$/i.test(location.hostname);
  }

  function isYoutubeDownloadPage(u) {
    return /youtube\.com\/(watch|shorts|playlist)|music\.youtube\.com\/watch/i.test(u);
  }

  function hasListParam(u) {
    try {
      return new URL(u).searchParams.has("list");
    } catch {
      return false;
    }
  }

  function fmtSize(bytes) {
    const n = Number(bytes || 0);
    if (!n || n <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return ` · ~${i === 0 ? v : v.toFixed(0)} ${units[i]}`;
  }

  /* ==================== 1. YOUTUBE WIDGET ==================== */
  function createYoutubeWidget() {
    let host = document.getElementById(YT_HOST_ID);
    if (host) return host;

    host = document.createElement("div");
    host.id = YT_HOST_ID;
    host.style.cssText = "position:fixed;top:68px;right:20px;z-index:2147483647;font-family:system-ui,-apple-system,sans-serif;";
    const shadow = host.attachShadow({ mode: "open" });

    shadow.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
        .wrap { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
        .trigger {
          display: inline-flex; align-items: center; gap: 7px;
          padding: 7px 12px; border-radius: 8px;
          background: rgba(17, 20, 27, 0.94); color: #f4f4f5;
          border: 1px solid rgba(59, 130, 246, 0.55);
          box-shadow: 0 8px 24px rgba(0,0,0,0.45);
          font-size: 12px; font-weight: 600; cursor: pointer;
          backdrop-filter: blur(8px); transition: background 0.15s, border-color 0.15s;
        }
        .trigger:hover { background: #181d27; border-color: #3b82f6; }
        .dot { width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; }
        .panel {
          display: none; width: 325px; padding: 12px; border-radius: 10px;
          background: #11141b; color: #e4e4e7; border: 1px solid #27272a;
          box-shadow: 0 14px 36px rgba(0,0,0,0.6); font-size: 12px;
        }
        .panel.open { display: block; }
        .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .title { font-weight: 600; font-size: 12.5px; color: #f4f4f5; }
        .close { background: none; border: 0; color: #a1a1aa; cursor: pointer; font-size: 13px; padding: 2px 5px; }
        .tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }
        .tab {
          padding: 6px; border-radius: 6px; border: 1px solid #27272a;
          background: #0b0d11; color: #a1a1aa; font-size: 12px; font-weight: 500; cursor: pointer;
        }
        .tab.active {
          background: rgba(37, 99, 235, 0.2); border-color: rgba(59, 130, 246, 0.5); color: #93c5fd;
        }
        .row { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }
        label { display: block; font-size: 10.5px; color: #a1a1aa; margin-bottom: 3px; }
        select {
          width: 100%; padding: 6px 7px; border-radius: 6px;
          border: 1px solid #27272a; background: #0b0d11; color: #f4f4f5; font-size: 11.5px;
        }
        .chk-row {
          display: flex; align-items: center; gap: 7px;
          padding: 6px 8px; margin-bottom: 7px; border-radius: 6px;
          background: #0b0d11; border: 1px solid #27272a;
          font-size: 11.5px; color: #d4d4d8; cursor: pointer;
        }
        .pl-row {
          display: none; align-items: center; gap: 7px;
          padding: 6px 8px; margin-bottom: 8px; border-radius: 6px;
          background: rgba(37, 99, 235, 0.12); border: 1px solid rgba(37, 99, 235, 0.3);
          font-size: 11.5px; color: #e4e4e7; cursor: pointer;
        }
        .pl-row.show { display: flex; }
        .hint { font-size: 10.5px; color: #93c5fd; margin-bottom: 8px; display: none; }
        .dl-btn {
          width: 100%; padding: 8px; border-radius: 6px; border: 0;
          background: #2563eb; color: #fff; font-size: 12px; font-weight: 600; cursor: pointer;
        }
        .dl-btn:hover { background: #3b82f6; }
        .dl-btn:disabled { opacity: 0.6; cursor: wait; }
        .msg { margin-top: 7px; font-size: 11px; min-height: 14px; color: #34d399; }
      </style>
      <div class="wrap">
        <button id="gdmTrigger" class="trigger" title="Descarregar com Gennius DM">
          <span class="dot"></span>
          <span>Descarregar (Gennius DM)</span>
        </button>
        <div id="gdmPanel" class="panel">
          <div class="top">
            <span class="title">Gennius DM — YouTube</span>
            <button id="gdmClose" class="close">✕</button>
          </div>
          <div class="tabs">
            <button id="tabVideo" class="tab active">Video</button>
            <button id="tabAudio" class="tab">Audio</button>
          </div>
          <div class="row">
            <div>
              <label>Formato</label>
              <select id="gdmFmt"></select>
            </div>
            <div>
              <label id="gdmQualLbl">Resolução</label>
              <select id="gdmQual"></select>
            </div>
          </div>
          <label id="gdmSubRow" class="chk-row">
            <input id="gdmSubChk" type="checkbox" />
            <span>Embutir legendas (PT / EN)</span>
          </label>
          <div id="gdmAudioHint" class="hint">Capa do vídeo + metadados ID3 embutidos automaticamente.</div>
          <label id="gdmPlRow" class="pl-row">
            <input id="gdmPlChk" type="checkbox" />
            <span>Descarregar playlist completa</span>
          </label>
          <button id="gdmStart" class="dl-btn">Descarregar</button>
          <div id="gdmMsg" class="msg"></div>
        </div>
      </div>
    `;

    const trigger = shadow.getElementById("gdmTrigger");
    const panel = shadow.getElementById("gdmPanel");
    const closeBtn = shadow.getElementById("gdmClose");
    const tabVideo = shadow.getElementById("tabVideo");
    const tabAudio = shadow.getElementById("tabAudio");
    const fmtSel = shadow.getElementById("gdmFmt");
    const qualSel = shadow.getElementById("gdmQual");
    const qualLbl = shadow.getElementById("gdmQualLbl");
    const subRow = shadow.getElementById("gdmSubRow");
    const subChk = shadow.getElementById("gdmSubChk");
    const audioHint = shadow.getElementById("gdmAudioHint");
    const plRow = shadow.getElementById("gdmPlRow");
    const plChk = shadow.getElementById("gdmPlChk");
    const startBtn = shadow.getElementById("gdmStart");
    const msgEl = shadow.getElementById("gdmMsg");

    function renderOptions() {
      if (currentMode === "audio") {
        tabVideo.classList.remove("active");
        tabAudio.classList.add("active");
        qualLbl.textContent = "Bitrate";
        subRow.style.display = "none";
        audioHint.style.display = "block";
        fmtSel.innerHTML = `<option value="mp3">MP3</option><option value="m4a">M4A</option><option value="opus">Opus</option>`;
        qualSel.innerHTML = `<option value="320">320 kbps</option><option value="192">192 kbps</option><option value="128">128 kbps</option>`;
      } else {
        tabAudio.classList.remove("active");
        tabVideo.classList.add("active");
        qualLbl.textContent = "Resolução";
        subRow.style.display = "flex";
        audioHint.style.display = "none";
        fmtSel.innerHTML = `<option value="mp4">MP4</option><option value="mkv">MKV</option><option value="webm">WebM</option>`;
        const qs = (probeCache && probeCache.video_qualities) || [
          { value: "best", label: "Melhor disponível", estimated_size: 0 },
          { value: "2160", label: "2160p (4K)", estimated_size: 0 },
          { value: "1080", label: "1080p (Full HD)", estimated_size: 0 },
          { value: "720", label: "720p (HD)", estimated_size: 0 },
          { value: "480", label: "480p", estimated_size: 0 },
          { value: "360", label: "360p", estimated_size: 0 }
        ];
        qualSel.innerHTML = qs
          .map((q) => `<option value="${q.value}">${q.label}${fmtSize(q.estimated_size)}</option>`)
          .join("");
      }
      if (hasListParam(location.href)) {
        plRow.classList.add("show");
        plChk.checked = /youtube\.com\/playlist/i.test(location.href);
      } else {
        plRow.classList.remove("show");
        plChk.checked = false;
      }
    }

    trigger.onclick = () => {
      panel.classList.toggle("open");
      if (panel.classList.contains("open")) {
        renderOptions();
        msgEl.textContent = "";
        if (!probeCache || probeCache._url !== location.href) {
          chrome.runtime.sendMessage({ type: "probeYoutube", url: location.href }, (res) => {
            if (res && res.ok && res.data) {
              probeCache = { ...res.data, _url: location.href };
              renderOptions();
            }
          });
        }
      }
    };
    closeBtn.onclick = () => panel.classList.remove("open");
    tabVideo.onclick = () => { currentMode = "video"; renderOptions(); };
    tabAudio.onclick = () => { currentMode = "audio"; renderOptions(); };

    startBtn.onclick = () => {
      startBtn.disabled = true;
      startBtn.textContent = "A enviar...";
      msgEl.style.color = "#a1a1aa";
      msgEl.textContent = "A ligar ao Gennius DM...";
      const isPlaylist = plChk.checked;

      chrome.runtime.sendMessage(
        {
          type: "sendUrl",
          url: location.href,
          media_mode: currentMode,
          container: fmtSel.value,
          quality: currentMode === "audio" ? "best" : qualSel.value,
          audio_bitrate: currentMode === "audio" ? qualSel.value : "320",
          subtitles: currentMode === "video" && subChk.checked,
          download_playlist: isPlaylist,
          queue_id: currentMode === "audio" ? "main" : "video",
          referer: location.href
        },
        (resp) => {
          startBtn.disabled = false;
          startBtn.textContent = "Descarregar";
          if (resp && resp.ok) {
            msgEl.style.color = "#34d399";
            msgEl.textContent = isPlaylist ? "Playlist adicionada ao Gennius DM!" : "Transferência iniciada no Gennius DM!";
            setTimeout(() => panel.classList.remove("open"), 1400);
          } else {
            msgEl.style.color = "#f87171";
            msgEl.textContent = (resp && resp.error) || "Erro ao ligar ao Gennius DM.";
          }
        }
      );
    };

    document.documentElement.appendChild(host);
    return host;
  }

  /* ==================== 2. UNIVERSAL STREAM SNIFFER BAR ==================== */
  function renderMediaPill(items) {
    latestStreams = items || [];
    if (isYoutubeDomain() || mediaDismissed || !latestStreams.length) {
      const existing = document.getElementById(MEDIA_HOST_ID);
      if (existing) existing.remove();
      return;
    }

    let host = document.getElementById(MEDIA_HOST_ID);
    if (!host) {
      host = document.createElement("div");
      host.id = MEDIA_HOST_ID;
      host.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:2147483647;font-family:system-ui,-apple-system,sans-serif;";
      const shadow = host.attachShadow({ mode: "open" });
      shadow.innerHTML = `
        <style>
          * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
          .wrap { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
          .bar {
            display: flex; align-items: center; gap: 6px;
            padding: 6px 10px; border-radius: 8px;
            background: rgba(17, 20, 27, 0.94); color: #f4f4f5;
            border: 1px solid rgba(59, 130, 246, 0.5);
            box-shadow: 0 8px 24px rgba(0,0,0,0.45);
            font-size: 12px; backdrop-filter: blur(8px);
          }
          .btn {
            background: #2563eb; color: #fff; border: 0; border-radius: 5px;
            padding: 4px 9px; font-size: 11.5px; font-weight: 600; cursor: pointer;
          }
          .btn:hover { background: #3b82f6; }
          .x {
            background: none; border: 0; color: #a1a1aa; cursor: pointer; font-size: 12px; padding: 2px 4px;
          }
          .menu {
            display: none; width: 300px; max-height: 220px; overflow-y: auto;
            padding: 8px; border-radius: 8px; background: #11141b;
            border: 1px solid #27272a; box-shadow: 0 12px 32px rgba(0,0,0,0.6);
          }
          .menu.open { display: flex; flex-direction: column; gap: 5px; }
          .item {
            display: flex; align-items: center; justify-content: space-between; gap: 6px;
            padding: 5px 7px; border-radius: 5px; background: #0b0d11; border: 1px solid #27272a;
          }
          .name {
            flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            font-size: 11px; color: #e4e4e7;
          }
          .tag {
            font-size: 9px; font-family: monospace; color: #93c5fd;
            background: rgba(37, 99, 235, 0.2); padding: 1px 4px; border-radius: 3px;
          }
        </style>
        <div class="wrap">
          <div id="gdmMediaMenu" class="menu"></div>
          <div class="bar">
            <span id="gdmMediaCount" style="font-weight:600;">Vídeo detetado</span>
            <button id="gdmMediaBtn" class="btn">Transferir</button>
            <button id="gdmMediaClose" class="x" title="Fechar">✕</button>
          </div>
        </div>
      `;
      shadow.getElementById("gdmMediaClose").onclick = () => {
        mediaDismissed = true;
        host.remove();
      };
      shadow.getElementById("gdmMediaBtn").onclick = () => {
        if (latestStreams.length === 1) {
          const item = latestStreams[0];
          chrome.runtime.sendMessage({
            type: "sendUrl",
            url: item.url,
            filename: item.filename,
            queue_id: "video",
            referer: location.href
          });
        } else {
          shadow.getElementById("gdmMediaMenu").classList.toggle("open");
        }
      };
      document.documentElement.appendChild(host);
    }

    const shadow = host.shadowRoot;
    const countEl = shadow.getElementById("gdmMediaCount");
    const menuEl = shadow.getElementById("gdmMediaMenu");
    countEl.textContent = latestStreams.length === 1 ? "Vídeo detetado" : `${latestStreams.length} streams detetados`;
    menuEl.innerHTML = "";
    for (const item of latestStreams) {
      const row = document.createElement("div");
      row.className = "item";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.kind;
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = (item.filename || item.url) + fmtSize(item.size);
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = "Baixar";
      b.onclick = () => {
        chrome.runtime.sendMessage({
          type: "sendUrl",
          url: item.url,
          filename: item.filename,
          queue_id: "video",
          referer: location.href
        });
        menuEl.classList.remove("open");
      };
      row.appendChild(tag);
      row.appendChild(name);
      row.appendChild(b);
      menuEl.appendChild(row);
    }
  }

  function syncVisibility() {
    if (isYoutubeDomain()) {
      const existing = document.getElementById(YT_HOST_ID);
      if (isYoutubeDownloadPage(location.href)) {
        const host = createYoutubeWidget();
        host.style.display = "block";
      } else if (existing) {
        existing.style.display = "none";
      }
    } else {
      chrome.runtime.sendMessage({ type: "getTabMedia" }, (res) => {
        if (res && Array.isArray(res.items) && res.items.length) {
          renderMediaPill(res.items);
        }
      });
    }
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "tabMediaUpdated" && Array.isArray(msg.items)) {
      renderMediaPill(msg.items);
    }
  });

  syncVisibility();
  window.addEventListener("yt-navigate-finish", () => {
    probeCache = null;
    syncVisibility();
  });
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      probeCache = null;
      mediaDismissed = false;
      syncVisibility();
    }
  }, 1000);
})();
