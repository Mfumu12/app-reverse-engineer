const params = new URLSearchParams(location.search);
const urlInput = document.getElementById("dlUrl");
const nameInput = document.getElementById("dlName");
const connsSel = document.getElementById("dlConns");
const queueSel = document.getElementById("dlQueue");
const subdirSel = document.getElementById("dlSubdir");
const customDirRow = document.getElementById("customDirRow");
const customDirInput = document.getElementById("dlCustomDir");
const browseDirBtn = document.getElementById("browseDirBtn");
const sizeBadge = document.getElementById("sizeBadge");
const errBox = document.getElementById("errBox");
const startBtn = document.getElementById("startBtn");

const referer = params.get("referer") || "";
urlInput.value = params.get("url") || "";
nameInput.value = params.get("filename") || "";

const bytes = parseInt(params.get("size") || "0", 10);
if (bytes > 0) {
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  sizeBadge.textContent = `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

subdirSel.onchange = () => {
  customDirRow.style.display = subdirSel.value === "__custom__" ? "flex" : "none";
};

browseDirBtn.onclick = () => {
  browseDirBtn.disabled = true;
  chrome.runtime.sendMessage({ type: "pickFolder" }, (resp) => {
    browseDirBtn.disabled = false;
    if (resp && resp.path) {
      customDirInput.value = resp.path;
    }
  });
};

document.getElementById("cancelBtn").onclick = () => window.close();

document.getElementById("browserBtn").onclick = () => {
  const url = urlInput.value.trim();
  if (!url) {
    window.close();
    return;
  }
  chrome.runtime.sendMessage(
    { type: "browserFallback", url, filename: nameInput.value.trim() || undefined },
    () => window.close()
  );
};

startBtn.onclick = () => {
  const url = urlInput.value.trim();
  if (!url) return;
  errBox.style.display = "none";
  startBtn.disabled = true;
  startBtn.textContent = "A iniciar...";

  let save_dir = undefined;
  let save_subdir = subdirSel.value;
  if (save_subdir === "__custom__") {
    const custom = customDirInput.value.trim();
    save_subdir = undefined;
    if (custom) {
      if (/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(custom)) save_dir = custom;
      else save_subdir = custom;
    }
  }

  chrome.runtime.sendMessage(
    {
      type: "sendUrl",
      url,
      filename: nameInput.value.trim() || undefined,
      save_dir,
      save_subdir,
      max_connections: Number(connsSel.value || 8),
      queue_id: queueSel.value || "main",
      referer
    },
    (resp) => {
      if (resp && resp.ok) {
        window.close();
      } else {
        startBtn.disabled = false;
        startBtn.textContent = "Descarregar";
        errBox.style.display = "block";
        errBox.textContent =
          (resp && resp.error) ||
          "Não foi possível ligar ao Gennius DM. Verifica se o GenniusDM.exe está aberto.";
      }
    }
  );
};
