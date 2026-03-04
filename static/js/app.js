/* ===========================================
   LINEスタンプ自動でつくるくん - Client JS
   =========================================== */

const state = {
  currentStep: 1,
  files: [],
  sessionId: null,
  mode: "A",
  templates: [],
  selectedTemplates: [],
};

// --- DOM ---
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// --- Step Navigation ---
function goToStep(n) {
  state.currentStep = n;
  $$(".panel").forEach((p) => p.classList.remove("active"));
  $(`#panel-${n}`).classList.add("active");
  $$(".step").forEach((s) => {
    const sn = +s.dataset.step;
    s.classList.toggle("active", sn === n);
    s.classList.toggle("done", sn < n);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// --- Step 1: Upload ---
function initUpload() {
  const area = $("#upload-area");
  const input = $("#file-input");

  area.addEventListener("click", (e) => {
    if (e.target.closest("label")) return;
    input.click();
  });

  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = "";
  });

  area.addEventListener("dragover", (e) => {
    e.preventDefault();
    area.classList.add("dragover");
  });

  area.addEventListener("dragleave", () => {
    area.classList.remove("dragover");
  });

  area.addEventListener("drop", (e) => {
    e.preventDefault();
    area.classList.remove("dragover");
    addFiles(e.dataTransfer.files);
  });

  $("#btn-next-1").addEventListener("click", () => {
    if (state.files.length === 8) {
      uploadFiles();
    }
  });
}

function isHeic(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".heic") || name.endsWith(".heif") || file.type === "image/heic" || file.type === "image/heif";
}

async function addFiles(fileList) {
  for (const file of fileList) {
    if (state.files.length >= 8) break;
    // HEIC/HEIF はtypeが空のことがあるので拡張子でもチェック
    if (!file.type.startsWith("image/") && !isHeic(file)) continue;
    state.files.push(file);
  }
  renderPreviewGrid();
  updateCounter();
}

function removeFile(index) {
  state.files.splice(index, 1);
  renderPreviewGrid();
  updateCounter();
}

async function createThumbnailUrl(file) {
  // サーバー側でJPEGサムネイルに変換（HEIC/HEIF含む全形式対応）
  try {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch("/api/preview-image", { method: "POST", body: formData });
    if (res.ok) {
      const blob = await res.blob();
      return URL.createObjectURL(blob);
    }
  } catch (e) {
    console.warn("サーバー変換失敗:", e);
  }
  // フォールバック: ブラウザで直接表示を試みる
  return URL.createObjectURL(file);
}

function renderPreviewGrid() {
  const grid = $("#preview-grid");
  grid.innerHTML = "";

  state.files.forEach((file, i) => {
    const div = document.createElement("div");
    div.className = "preview-item";

    const img = document.createElement("img");
    // まずプレースホルダーを表示
    img.alt = file.name;

    // 非同期でサムネイルを生成
    createThumbnailUrl(file).then((url) => {
      img.src = url;
    });

    // 読み込み失敗時はファイル名を表示
    img.onerror = () => {
      div.classList.add("preview-fallback");
      img.style.display = "none";
      const label = document.createElement("span");
      label.className = "fallback-label";
      label.textContent = file.name.slice(0, 12);
      div.appendChild(label);
    };

    const num = document.createElement("span");
    num.className = "preview-num";
    num.textContent = i + 1;

    const btn = document.createElement("button");
    btn.className = "remove-btn";
    btn.textContent = "✕";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeFile(i);
    });

    div.append(img, num, btn);
    grid.appendChild(div);
  });
}

function updateCounter() {
  const counter = $("#counter");
  const count = $("#count");
  const btn = $("#btn-next-1");

  counter.hidden = state.files.length === 0;
  count.textContent = state.files.length;
  count.className = state.files.length === 8 ? "count-ok" : "";
  btn.disabled = state.files.length !== 8;
}

async function uploadFiles() {
  const formData = new FormData();
  state.files.forEach((f) => formData.append("files", f));

  try {
    $("#btn-next-1").disabled = true;
    $("#btn-next-1").textContent = "アップロード中...";

    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || "アップロードに失敗しました");

    state.sessionId = data.session_id;
    goToStep(2);
  } catch (e) {
    alert(e.message);
  } finally {
    $("#btn-next-1").disabled = state.files.length !== 8;
    $("#btn-next-1").textContent = "次へ →";
  }
}

// --- Step 2: Message Mode ---
function initModeSelection() {
  $$(".mode-card").forEach((card) => {
    card.addEventListener("click", () => {
      $$(".mode-card").forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      state.mode = card.dataset.mode;

      // Show/hide detail panels
      $$("#detail-B, #detail-C").forEach((d) => (d.hidden = true));
      if (state.mode === "B") {
        $("#detail-B").hidden = false;
        loadTemplates();
      } else if (state.mode === "C") {
        $("#detail-C").hidden = false;
      }
    });
  });

  $("#btn-back-2").addEventListener("click", () => goToStep(1));
  $("#btn-next-2").addEventListener("click", () => generateStamps());
}

async function loadTemplates() {
  if (state.templates.length > 0) {
    renderTemplateGrid();
    return;
  }

  const res = await fetch("/api/templates");
  const data = await res.json();
  state.templates = data.templates;
  renderTemplateGrid();
}

function renderTemplateGrid() {
  const grid = $("#template-grid");
  grid.innerHTML = "";

  state.templates.forEach((t) => {
    const chip = document.createElement("span");
    chip.className = "template-chip";
    chip.textContent = t;

    if (state.selectedTemplates.includes(t)) {
      chip.classList.add("selected");
    }

    chip.addEventListener("click", () => {
      const idx = state.selectedTemplates.indexOf(t);
      if (idx >= 0) {
        state.selectedTemplates.splice(idx, 1);
        chip.classList.remove("selected");
      } else if (state.selectedTemplates.length < 8) {
        state.selectedTemplates.push(t);
        chip.classList.add("selected");
      }
      $("#template-selected").textContent = state.selectedTemplates.length;
    });

    grid.appendChild(chip);
  });
}

function getMessages() {
  if (state.mode === "A" || state.mode === "D") return "";
  if (state.mode === "B") return JSON.stringify(state.selectedTemplates);
  if (state.mode === "C") {
    const msgs = $$(".msg-input").map((input) => input.value.trim() || input.placeholder);
    return JSON.stringify(msgs);
  }
  return "";
}

function validateStep2() {
  if (state.mode === "B" && state.selectedTemplates.length !== 8) {
    alert("メッセージを8個選んでください");
    return false;
  }
  return true;
}

// --- Step 3: Generate ---
async function generateStamps() {
  if (!validateStep2()) return;

  goToStep(3);
  $("#processing").hidden = false;
  $("#result").hidden = true;
  $("#error-panel").hidden = true;

  // Fake progress animation
  let progress = 0;
  const progressInterval = setInterval(() => {
    progress = Math.min(progress + Math.random() * 8, 90);
    $("#progress-fill").style.width = progress + "%";
  }, 500);

  try {
    const formData = new FormData();
    formData.append("session_id", state.sessionId);
    formData.append("mode", state.mode);
    formData.append("messages", getMessages());

    const res = await fetch("/api/generate", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || "生成に失敗しました");

    clearInterval(progressInterval);
    $("#progress-fill").style.width = "100%";

    await new Promise((r) => setTimeout(r, 500));

    showResult(data);
  } catch (e) {
    clearInterval(progressInterval);
    showError(e.message);
  }
}

function showResult(data) {
  $("#processing").hidden = true;
  $("#result").hidden = false;

  const grid = $("#result-grid");
  grid.innerHTML = "";

  data.stamps.forEach((filename) => {
    const div = document.createElement("div");
    div.className = "result-item";

    const img = document.createElement("img");
    img.src = `/api/stamp/${state.sessionId}/${filename}`;
    img.alt = filename;
    img.loading = "lazy";

    div.appendChild(img);
    grid.appendChild(div);
  });

  $("#btn-download").href = `/api/download/${state.sessionId}`;
}

function showError(message) {
  $("#processing").hidden = true;
  $("#error-panel").hidden = false;
  $("#error-message").textContent = message;
}

function resetApp() {
  state.files = [];
  state.sessionId = null;
  state.mode = "A";
  state.selectedTemplates = [];

  renderPreviewGrid();
  updateCounter();

  $$(".mode-card").forEach((c) => c.classList.remove("selected"));
  $$('.mode-card[data-mode="A"]').forEach((c) => c.classList.add("selected"));
  $$("#detail-B, #detail-C").forEach((d) => (d.hidden = true));
  $$(".msg-input").forEach((input) => (input.value = ""));

  goToStep(1);
}

// --- LINE Creators Market Upload ---
function initLineUpload() {
  $("#btn-upload-line").addEventListener("click", () => {
    $("#line-upload-form").hidden = false;
    $("#btn-upload-line").hidden = true;
  });

  $("#btn-cancel-upload").addEventListener("click", () => {
    $("#line-upload-form").hidden = true;
    $("#btn-upload-line").hidden = false;
  });

  $("#btn-start-upload").addEventListener("click", async () => {
    const title = $("#stamp-title").value.trim() || "ペットスタンプ";
    const desc = $("#stamp-desc").value.trim() || "かわいいペットのスタンプです";

    $("#line-upload-form").hidden = true;
    $("#line-uploading").hidden = false;

    const formData = new FormData();
    formData.append("title", title);
    formData.append("description", desc);

    try {
      const res = await fetch(`/api/upload-to-line/${state.sessionId}`, {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      // ステータスポーリング開始
      startStatusPolling();
    } catch (e) {
      alert("エラー: " + e.message);
      $("#line-uploading").hidden = true;
      $("#btn-upload-line").hidden = false;
    }
  });
}

// --- LINE Upload Status Polling ---
let statusPollTimer = null;

function startStatusPolling() {
  if (statusPollTimer) clearInterval(statusPollTimer);

  statusPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/upload-status/${state.sessionId}`);
      const data = await res.json();

      const statusEl = $("#line-upload-status-text");
      if (statusEl) {
        statusEl.textContent = `[${data.step}] ${data.message}`;
      }

      const progressEl = $("#line-upload-progress");
      if (progressEl) {
        progressEl.style.width = (data.progress || 0) + "%";
      }

      // ログ全文を表示
      const logsEl = $("#line-upload-logs");
      if (logsEl && data.logs && data.logs.length > 0) {
        logsEl.textContent = data.logs.join("\n");
        logsEl.scrollTop = logsEl.scrollHeight;
      }

      // 完了またはエラーでポーリング停止 & デバッグスクリーンショット取得
      if (data.step === "完了" || data.step === "エラー" || data.step === "中断") {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        loadDebugScreenshots();
      }
    } catch (e) {
      // ネットワークエラーは無視して次のポーリングを待つ
    }
  }, 2000);
}

async function loadDebugScreenshots() {
  try {
    const res = await fetch(`/api/debug-screenshots/${state.sessionId}`);
    const data = await res.json();
    const container = $("#debug-screenshots");
    if (!container || !data.screenshots || data.screenshots.length === 0) return;
    container.hidden = false;
    const grid = container.querySelector(".debug-grid") || container;
    grid.innerHTML = "";
    for (const name of data.screenshots) {
      const img = document.createElement("img");
      img.src = `/api/debug-screenshots/${state.sessionId}/${name}`;
      img.alt = name;
      img.title = name;
      img.style.maxWidth = "300px";
      img.style.border = "1px solid #ccc";
      img.style.borderRadius = "8px";
      img.style.margin = "4px";
      grid.appendChild(img);
    }
  } catch (e) {
    // ignore
  }
}

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
  initUpload();
  initModeSelection();
  initLineUpload();

  $("#btn-restart").addEventListener("click", resetApp);
  $("#btn-retry").addEventListener("click", resetApp);
});
