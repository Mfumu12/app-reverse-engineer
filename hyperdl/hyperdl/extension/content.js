/* Gennius DM — In-page YouTube floating download button & format selector */
(() => {
  if (window.__genniusDmInjected) return;
  window.__genniusDmInjected = true;

  const HOST_ID = "gennius-dm-yt-host";
  let lastUrl = location.href;
  let currentMode = "video";
  let probeCache = null;

  function isWatchOrShorts(u) {
    return /youtube\.com\/(watch|shorts)|music\.youtube\.com\/watch/i.test(u);
  }

  function hasListParam(u) {
    try {
      return new URL(u).searchParams.has("list");
    } catch {
      return false;
    }
  }

  function createWidget() {
    let host = document.getElementById(HOST_ID);
    if (host) return host;

    host = document.createElement("div");
    host.id = HOST_ID;
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
          display: none; width: 310px; padding: 12px; border-radius: 10px;
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
        .pl-row {
          display: none; align-items: center; gap: 7px;
          padding: 6px 8px; margin-bottom: 8px; border-radius: 6px;
          background: rgba(37, 99, 235, 0.12); border: 1px solid rgba(37, 99, 235, 0.3);
          font-size: 11.5px; color: #e4e4e7; cursor: pointer;
        }
        .pl-row.show { display: flex; }
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
            <button id="gdmTabVideo" class="tab active">Video</button>
            <button id="gdmTabAudio" class="tab">Audio</button>
          </div>
          <div class="row">
            <div>
              <label>Formato</label>
              <select id="gdmFmt"></select>
            </div>
            <div>
              <label id="gdmQualLbl">Qualidade</label>
              <select id="gdmQual"></select>
            </div>
          </div>
          <label id="gdmPlRow" class="pl-row">
            <input id="gdmPlCheck" type="checkbox" />
            <span>Baixar playlist completa</span>
          </label>
          <button id="gdmDownload" class="dl-btn">Descarregar</button>
          <div id="gdmMsg" class="msg"></div>
        </div>
      </div>
    `;

    const panel = shadow.getElementById("gdmPanel");
    const tabVideo = shadow.getElementById("gdmTabVideo");
    const tabAudio = shadow.getElementById("gdmTabAudio");
    const fmtSel = shadow.getElementById("gdmFmt");
    const qualSel = shadow.getElementById("gdmQual");
    const qualLbl = shadow.getElementById("gdmQualLbl");
    const plRow = shadow.getElementById("gdmPlRow");
    const plCheck = shadow.getElementById("gdmPlCheck");
    const dlBtn = shadow.getElementById("gdmDownload");
    const msgEl = shadow.getElementById("gdmMsg");

    function renderSelects() {
      if (currentMode === "audio") {
        tabAudio.classList.add("active");
        tabVideo.classList.remove("active");
        qualLbl.textContent = "Bitrate";
        fmtSel.innerHTML = `
          <option value="mp3">MP3</option>
          <option value="m4a">M4A</option>
          <option value="opus">Opus</option>
        `;
        qualSel.innerHTML = `
          <option value="320">320 kbps</option>
          <option value="192">192 kbps</option>
          <option value="128">128 kbps</option>
        `;
      } else {
        tabVideo.classList.add("active");
        tabAudio.classList.remove("active");
        qualLbl.textContent = "Qualidade";
        fmtSel.innerHTML = `
          <option value="mp4">MP4</option>
          <option value="mkv">MKV</option>
          <option value="webm">WebM</option>
        `;
        const quals = (probeCache && probeCache.video_qualities) || [
          { value: "best", label: "Melhor disponível" },
          { value: "2160", label: "2160p (4K)" },
          { value: "1080", label: "1080p (Full HD)" },
          { value: "720", label: "720p (HD)" },
          { value: "480", label: "480p" },
          { value: "360", label: "360p" }
        ];
        qualSel.innerHTML = quals
          .map((q) => `<option value="${q.value}">${q.label}</option>`)
          .join("");
      }
      if (hasListParam(location.href)) {
        plRow.classList.add("show");
      } else {
        plRow.classList.remove("show");
        plCheck.checked = false;
      }
    }

    shadow.getElementById("gdmTrigger").onclick = () => {
      const opening = !panel.classList.contains("open");
      panel.classList.toggle("open");
      msgEl.textContent = "";
      renderSelects();
      if (opening) {
        chrome.runtime.sendMessage({ type: "probeYoutube", url: location.href }, (res) => {
          if (res && res.ok && res.data) {
            probeCache = res.data;
            renderSelects();
          }
        });
      }
    };

    shadow.getElementById("gdmClose").onclick = () => panel.classList.remove("open");
    tabVideo.onclick = () => { currentMode = "video"; renderSelects(); };
    tabAudio.onclick = () => { currentMode = "audio"; renderSelects(); };

    dlBtn.onclick = () => {
      dlBtn.disabled = true;
      dlBtn.textContent = "A enviar...";
      msgEl.textContent = "";
      const qual = qualSel.value;
      chrome.runtime.sendMessage(
        {
          type: "sendUrl",
          url: location.href,
          queue_id: currentMode === "video" ? "video" : "main",
          media_mode: currentMode,
          container: fmtSel.value,
          quality: currentMode === "audio" ? "best" : qual,
          audio_bitrate: currentMode === "audio" ? qual : "320",
          download_playlist: plCheck.checked,
          referer: location.href
        },
        (resp) => {
          dlBtn.disabled = false;
          dlBtn.textContent = "Descarregar";
          if (resp && resp.ok) {
            msgEl.style.color = "#34d399";
            msgEl.textContent = plCheck.checked
              ? "Playlist adicionada ao Gennius DM!"
              : "Download iniciado no Gennius DM!";
            plCheck.checked = false;
          } else {
            msgEl.style.color = "#f87171";
            msgEl.textContent = "Abre o GenniusDM.exe para transferir.";
          }
        }
      );
    };

    renderSelects();
    document.documentElement.appendChild(host);
    return host;
  }

  function syncVisibility() {
    const existing = document.getElementById(HOST_ID);
    if (isWatchOrShorts(location.href) || /youtube\.com\/playlist/i.test(location.href)) {
      const host = createWidget();
      host.style.display = "block";
    } else if (existing) {
      existing.style.display = "none";
    }
  }

  syncVisibility();
  window.addEventListener("yt-navigate-finish", () => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      probeCache = null;
    }
    syncVisibility();
  });
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      probeCache = null;
      syncVisibility();
    }
  }, 1000);
})();
