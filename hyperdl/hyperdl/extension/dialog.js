const params = new URLSearchParams(location.search);
const urlInput = document.getElementById("dlUrl");
const nameInput = document.getElementById("dlName");
const connsSel = document.getElementById("dlConns");
const queueSel = document.getElementById("dlQueue");
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

  chrome.runtime.sendMessage(
    {
      type: "sendUrl",
      url,
      filename: nameInput.value.trim() || undefined,
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
