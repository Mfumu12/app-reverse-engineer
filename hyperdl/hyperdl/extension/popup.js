const serverInput = document.getElementById("serverUrl");
const keyInput = document.getElementById("apiKey");
const capInput = document.getElementById("autoCapture");
const statusEl = document.getElementById("statusMsg");
const connPill = document.getElementById("connPill");
const quickUrl = document.getElementById("quickUrl");
const mediaWrap = document.getElementById("mediaWrap");
const mediaList = document.getElementById("mediaList");

let activeTabUrl = "";

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
  { serverUrl: "http://127.0.0.1:8787", apiKey: "", autoCapture: false },
  (res) => {
    serverInput.value = res.serverUrl;
    keyInput.value = res.apiKey;
    capInput.checked = res.autoCapture;
    checkServer(res.serverUrl);
  }
);

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs && tabs[0];
  if (!tab) return;
  activeTabUrl = tab.url || "";
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
  await chrome.storage.local.set({ serverUrl, apiKey, autoCapture });
  const ok = await checkServer(serverUrl);
  statusEl.style.color = ok ? "#34d399" : "#f87171";
  statusEl.textContent = ok ? "Configuração guardada e ligada." : "Guardado, mas servidor offline.";
};

document.getElementById("openDash").onclick = () => {
  const base = (serverInput.value.trim() || "http://127.0.0.1:8787").replace(/\/$/, "");
  chrome.tabs.create({ url: base });
};
