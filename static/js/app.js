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

  // Editor
  editorOpen: false,
  editingIndex: -1,
  editingFilename: "",
  activeTool: "eraser",
  brushSize: 20,
  undoStack: [],
  subjectOffsetX: 0,
  subjectOffsetY: 0,
  subjectScale: 1.0,
  isDragging: false,
  dragStartX: 0,
  dragStartY: 0,
  stampMessages: [],
  moveBaseImageData: null,
  // テキスト拡張
  selectedFont: "zen-maru",
  textColor: "white",
  vertical: false,
  stampFonts: [],
  stampTextColors: [],
  stampVerticals: [],
  // テキスト座標
  textX: null,
  textY: null,
  isDraggingText: false,
  stampTextX: [],
  stampTextY: [],
};

const FONT_FAMILY_MAP = {
  "zen-maru": "Zen Maru Gothic",
  "noto-sans": "Noto Sans JP",
  "zen-kaku": "Zen Kaku Gothic",
  "kosugi-maru": "Kosugi Maru",
  "hachi-maru": "Hachi Maru Pop",
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
  // HEIC/HEIF以外はブラウザ側Canvasで高速サムネイル生成
  if (!isHeic(file)) {
    try {
      return await createBrowserThumbnail(file);
    } catch (e) {
      console.warn("ブラウザサムネイル失敗、サーバーにフォールバック:", e);
    }
  }
  // HEIC/HEIFまたはフォールバック: サーバー側でJPEG変換
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
  return URL.createObjectURL(file);
}

function createBrowserThumbnail(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const maxSize = 150;
      let w = img.width, h = img.height;
      if (w > h) { h = Math.round(h * maxSize / w); w = maxSize; }
      else { w = Math.round(w * maxSize / h); h = maxSize; }
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (blob) resolve(URL.createObjectURL(blob));
        else reject(new Error("Canvas toBlob failed"));
      }, "image/jpeg", 0.6);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Image load failed")); };
    img.src = url;
  });
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

  // メッセージとテキスト位置を保存（エディターで使用）
  state.stampMessages = data.messages || [];
  // 名前付き位置を座標に変換
  const posToCoords = { top: [185, 35], bottom: [185, 280], left: [42, 160], right: [327, 160] };
  const positions = data.text_positions || [];
  state.stampTextX = positions.map(p => posToCoords[p] ? posToCoords[p][0] : null);
  state.stampTextY = positions.map(p => posToCoords[p] ? posToCoords[p][1] : null);

  const grid = $("#result-grid");
  grid.innerHTML = "";

  data.stamps.forEach((filename, i) => {
    const div = document.createElement("div");
    div.className = "result-item";
    div.dataset.index = i;
    div.dataset.filename = filename;
    div.style.cursor = "pointer";
    div.addEventListener("click", () => openEditor(i));

    const img = document.createElement("img");
    img.src = `/api/stamp/${state.sessionId}/${filename}`;
    img.alt = filename;
    img.loading = "lazy";

    const badge = document.createElement("span");
    badge.className = "edit-badge";
    badge.textContent = "編集";

    div.append(img, badge);
    grid.appendChild(div);
  });

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

    // セッションIDが無効な場合はエラー
    if (!state.sessionId || state.sessionId === "null" || state.sessionId === "undefined") {
      alert("セッションが無効です。ページをリロードしてやり直してください。");
      return;
    }

    // 通知パーミッションをリクエスト（初回のみ）
    requestNotificationPermission();

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

  // セッションIDが無効な場合はポーリングしない
  if (!state.sessionId || state.sessionId === "null" || state.sessionId === "undefined") {
    return;
  }

  let waitingCount = 0; // 「待機中」が続いた回数
  const MAX_WAITING = 60; // 120秒(2秒×60回)でタイムアウト
  let networkErrorCount = 0;
  const MAX_NETWORK_ERRORS = 10;

  statusPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/upload-status/${state.sessionId}`);
      const data = await res.json();
      networkErrorCount = 0; // ネットワーク成功でリセット

      const statusEl = $("#line-upload-status-text");
      if (statusEl) {
        statusEl.textContent = data.message;
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

      // 「待機中」が長く続いた場合 → 処理が開始されていないためエラー表示
      if (data.step === "待機中") {
        waitingCount++;
        if (waitingCount >= MAX_WAITING) {
          clearInterval(statusPollTimer);
          statusPollTimer = null;
          if (statusEl) {
            statusEl.textContent = "処理がタイムアウトしました。ページをリロードしてやり直してください。";
          }
          const uploading = $("#line-uploading");
          const waiting = $("#line-waiting");
          if (waiting && !waiting.hidden) {
            waiting.hidden = true;
            if (uploading) uploading.hidden = false;
          }
          return;
        }
      } else {
        waitingCount = 0; // 有効なステップが来たらリセット
      }

      // QRコード表示 → ログイン画面にQRコードを表示
      if (data.step === "QRコード") {
        const uploading = $("#line-uploading");
        const waiting = $("#line-waiting");
        if (uploading) uploading.hidden = false;
        if (waiting) waiting.hidden = true;

        // QRコード画像を表示
        const qrDisplay = $("#qr-code-display");
        const qrImg = $("#qr-code-img");
        if (qrDisplay && qrImg) {
          qrDisplay.hidden = false;
          // キャッシュバスターでQR画像を更新（30秒ごとにバックエンドが新画像を保存）
          const newSrc = `/api/qr-code/${state.sessionId}?t=${Date.now()}`;
          if (qrImg.src !== newSrc) {
            qrImg.src = newSrc;
          }
        }
        // タイトルテキストを更新
        const loginTitle = $("#line-login-title");
        const loginDesc = $("#line-login-desc");
        if (loginTitle) loginTitle.textContent = "QRコードでログイン";
        if (loginDesc) loginDesc.textContent = "LINEアプリでQRコードをスキャンしてログインしてください";

        const statusEl = $("#line-upload-status-text");
        if (statusEl) {
          statusEl.textContent = data.message;
          statusEl.style.color = "";
        }
      }

      // ログイン完了後 → 待機画面に切り替え（QRコード非表示にする）
      const waitingSteps = ["自動処理中", "画像アップ", "審査準備中", "開始", "ログイン"];
      if (waitingSteps.includes(data.step)) {
        const uploading = $("#line-uploading");
        const waiting = $("#line-waiting");
        if (uploading) uploading.hidden = true;
        if (waiting) waiting.hidden = false;
        // QRコード表示を非表示にする
        const qrDisplay = $("#qr-code-display");
        if (qrDisplay) qrDisplay.hidden = true;
        // 待機画面のプログレスバーとステータステキスト更新
        const waitProgress = $("#line-waiting-progress");
        if (waitProgress) {
          waitProgress.style.width = (data.progress || 25) + "%";
        }
        const waitStatus = $("#line-waiting-status");
        if (waitStatus) {
          waitStatus.textContent = data.message || "処理中...";
        }
      }

      // 完了 → 完了画面に切り替え + 通知
      if (data.step === "完了") {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        const uploading = $("#line-uploading");
        const waiting = $("#line-waiting");
        const complete = $("#line-complete");
        if (uploading) uploading.hidden = true;
        if (waiting) waiting.hidden = true;
        if (complete) complete.hidden = false;
        loadDebugScreenshots();
        notifyComplete();
      }

      // エラー・中断 → ポーリング停止、エラーメッセージ表示
      if (data.step === "エラー" || data.step === "中断") {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        const waiting = $("#line-waiting");
        const uploading = $("#line-uploading");
        if (waiting && !waiting.hidden) {
          waiting.hidden = true;
          if (uploading) uploading.hidden = false;
        }
        if (statusEl) {
          statusEl.textContent = data.message || "エラーが発生しました。もう一度お試しください。";
          statusEl.style.color = "#e74c3c";
        }
        loadDebugScreenshots();
      }
    } catch (e) {
      // ネットワークエラーが連続した場合はポーリング停止
      networkErrorCount++;
      if (networkErrorCount >= MAX_NETWORK_ERRORS) {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
        const statusEl = $("#line-upload-status-text");
        if (statusEl) {
          statusEl.textContent = "通信エラーが発生しました。ページをリロードしてやり直してください。";
          statusEl.style.color = "#e74c3c";
        }
        const waiting = $("#line-waiting");
        const uploading = $("#line-uploading");
        if (waiting && !waiting.hidden) {
          waiting.hidden = true;
          if (uploading) uploading.hidden = false;
        }
      }
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

// --- 完了通知 ---
function notifyComplete() {
  // ブラウザ通知
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification("🐾 スタンプ登録完了！", {
        body: "LINE Creators Marketへの登録が完了しました。審査をお待ちください。",
        icon: "/static/img/favicon.png",
        tag: "stamp-complete",
      });
    } catch (e) {
      // 通知がサポートされていない環境
    }
  }

  // 音で通知（短いビープ音を生成）
  try {
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const playTone = (freq, start, dur) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.frequency.value = freq;
      osc.type = "sine";
      gain.gain.setValueAtTime(0.3, audioCtx.currentTime + start);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + start + dur);
      osc.start(audioCtx.currentTime + start);
      osc.stop(audioCtx.currentTime + start + dur);
    };
    // 明るい3音チャイム
    playTone(523, 0, 0.15);    // C5
    playTone(659, 0.15, 0.15); // E5
    playTone(784, 0.3, 0.3);   // G5
  } catch (e) {
    // AudioContext がサポートされていない環境
  }

  // ページタイトルを点滅させて注意を引く
  let blinkCount = 0;
  const originalTitle = document.title;
  const blinkInterval = setInterval(() => {
    document.title = blinkCount % 2 === 0 ? "✅ 登録完了！" : originalTitle;
    blinkCount++;
    if (blinkCount >= 10) {
      clearInterval(blinkInterval);
      document.title = originalTitle;
    }
  }, 1000);
}

// 通知パーミッションをリクエスト
function requestNotificationPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
}

// --- Stamp Editor ---
let isErasing = false;

function initEditor() {
  // ツール切り替え
  $$(".editor-tool-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".editor-tool-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.activeTool = btn.dataset.tool;

      $("#eraser-controls").hidden = state.activeTool !== "eraser";
      $("#move-controls").hidden = state.activeTool !== "move";
      $("#text-controls").hidden = state.activeTool !== "text";

      updateCanvasCursor();

      // 移動ツールに切り替えた時、現在のcanvasを基準画像として保存
      if (state.activeTool === "move") {
        const canvas = $("#editor-canvas");
        const ctx = canvas.getContext("2d");
        state.moveBaseImageData = ctx.getImageData(0, 0, 370, 320);
        state.subjectOffsetX = 0;
        state.subjectOffsetY = 0;
        state.subjectScale = 1.0;
        $("#zoom-slider").value = 100;
        $("#zoom-val").textContent = "100";
      }
    });
  });

  // ブラシサイズ
  $("#brush-size").addEventListener("input", (e) => {
    state.brushSize = +e.target.value;
    $("#brush-size-val").textContent = state.brushSize;
    updateCanvasCursor();
  });

  // ズームスライダー
  $("#zoom-slider").addEventListener("input", (e) => {
    state.subjectScale = +e.target.value / 100;
    $("#zoom-val").textContent = e.target.value;
    redrawMoveCanvas();
  });

  // Canvas イベント（PointerEvents で mouse + touch 統合）
  const canvas = $("#editor-canvas");
  canvas.addEventListener("pointerdown", onCanvasPointerDown);
  canvas.addEventListener("pointermove", onCanvasPointerMove);
  canvas.addEventListener("pointerup", onCanvasPointerUp);
  canvas.addEventListener("pointerleave", onCanvasPointerUp);
  canvas.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });

  // ボタン
  $("#editor-back").addEventListener("click", closeEditor);
  $("#editor-cancel").addEventListener("click", closeEditor);
  $("#editor-save").addEventListener("click", saveEditedStamp);
  $("#btn-undo-step").addEventListener("click", undoOneStep);
  $("#btn-reset").addEventListener("click", resetToOriginal);
  $("#btn-reset-pos").addEventListener("click", resetPosition);
  $("#editor-prev").addEventListener("click", () => navigateStamp(-1));
  $("#editor-next").addEventListener("click", () => navigateStamp(1));

  // テキスト配置クリア
  $("#btn-clear-text-pos").addEventListener("click", () => {
    state.textX = null;
    state.textY = null;
    renderTextPreview();
  });

  // テキスト入力でリアルタイムプレビュー
  $("#editor-text-input").addEventListener("input", renderTextPreview);

  // フォント選択
  $("#font-select").addEventListener("change", (e) => {
    state.selectedFont = e.target.value;
    renderTextPreview();
  });

  // 文字色ボタン
  $$(".color-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".color-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.textColor = btn.dataset.color;
      renderTextPreview();
    });
  });

  // 方向ボタン (横書き/縦書き)
  $$(".dir-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".dir-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.vertical = btn.dataset.dir === "vertical";
      renderTextPreview();
    });
  });
}

function updateCanvasCursor() {
  const canvas = $("#editor-canvas");
  if (state.activeTool === "eraser") {
    canvas.style.cursor = "crosshair";
  } else if (state.activeTool === "move") {
    canvas.style.cursor = "grab";
  } else if (state.activeTool === "text") {
    canvas.style.cursor = "crosshair";
  } else {
    canvas.style.cursor = "default";
  }
}

function openEditor(index) {
  state.editingIndex = index;
  state.editingFilename = `${String(index + 1).padStart(2, "0")}.png`;
  state.editorOpen = true;
  state.undoStack = [];
  state.subjectOffsetX = 0;
  state.subjectOffsetY = 0;
  state.subjectScale = 1.0;
  state.moveBaseImageData = null;

  $("#result").hidden = true;
  $("#editor").hidden = false;
  $("#editor-title").textContent = `${state.editingFilename} を編集中`;

  // base画像（テキストなし版）をCanvasにロード。なければ通常画像にフォールバック
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = () => {
    ctx.clearRect(0, 0, 370, 320);
    ctx.drawImage(img, 0, 0, 370, 320);
    pushUndo();
  };
  const baseName = state.editingFilename.replace(".png", "_base.png");
  img.onerror = () => {
    // base画像がない場合（旧セッション）は通常画像を読み込む
    const fallback = new Image();
    fallback.crossOrigin = "anonymous";
    fallback.onload = () => {
      ctx.clearRect(0, 0, 370, 320);
      ctx.drawImage(fallback, 0, 0, 370, 320);
      pushUndo();
    };
    fallback.src = `/api/stamp/${state.sessionId}/${state.editingFilename}?t=${Date.now()}`;
  };
  img.src = `/api/stamp/${state.sessionId}/${baseName}?t=${Date.now()}`;

  // ツールをリセット
  state.activeTool = "eraser";
  $$(".editor-tool-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.tool === "eraser");
  });
  $("#eraser-controls").hidden = false;
  $("#move-controls").hidden = true;
  $("#text-controls").hidden = true;
  updateCanvasCursor();

  // テキスト入力欄に現在のメッセージを設定
  const currentMsg = state.stampMessages[index] || "";
  $("#editor-text-input").value = currentMsg || "";
  // テキスト座標を復元
  state.textX = state.stampTextX[index] != null ? state.stampTextX[index] : null;
  state.textY = state.stampTextY[index] != null ? state.stampTextY[index] : null;

  // フォント・色・方向を復元
  state.selectedFont = state.stampFonts[index] || "zen-maru";
  state.textColor = state.stampTextColors[index] || "white";
  state.vertical = state.stampVerticals[index] || false;
  $("#font-select").value = state.selectedFont;
  $$(".color-btn").forEach((b) => b.classList.toggle("active", b.dataset.color === state.textColor));
  $$(".dir-btn").forEach((b) => {
    const isVert = b.dataset.dir === "vertical";
    b.classList.toggle("active", isVert === state.vertical);
  });

  // テキストプレビューを描画
  renderTextPreview();

  updateNavButtons();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function closeEditor() {
  state.editorOpen = false;
  $("#editor").hidden = true;
  $("#result").hidden = false;
  // テキストプレビューをクリア
  const previewCanvas = $("#text-preview-canvas");
  previewCanvas.getContext("2d").clearRect(0, 0, 370, 320);
}

// --- Eraser ---
function onCanvasPointerDown(e) {
  if (state.activeTool === "eraser") {
    isErasing = true;
    pushUndo();
    eraseAt(e);
  } else if (state.activeTool === "move") {
    state.isDragging = true;
    const rect = e.target.getBoundingClientRect();
    state.dragStartX = e.clientX - rect.left;
    state.dragStartY = e.clientY - rect.top;
    e.target.style.cursor = "grabbing";
  } else if (state.activeTool === "text") {
    state.isDraggingText = true;
    placeTextAt(e);
  }
}

function onCanvasPointerMove(e) {
  if (state.activeTool === "eraser" && isErasing) {
    eraseAt(e);
  } else if (state.activeTool === "move" && state.isDragging) {
    const rect = e.target.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const scaleX = 370 / rect.width;
    const scaleY = 320 / rect.height;
    state.subjectOffsetX += (x - state.dragStartX) * scaleX;
    state.subjectOffsetY += (y - state.dragStartY) * scaleY;
    state.dragStartX = x;
    state.dragStartY = y;
    redrawMoveCanvas();
  } else if (state.activeTool === "text" && state.isDraggingText) {
    placeTextAt(e);
  }
}

function onCanvasPointerUp(e) {
  if (state.activeTool === "move" && state.isDragging) {
    e.target.style.cursor = "grab";
  }
  isErasing = false;
  state.isDragging = false;
  state.isDraggingText = false;
}

function placeTextAt(e) {
  const canvas = $("#editor-canvas");
  const rect = canvas.getBoundingClientRect();
  const scaleX = 370 / rect.width;
  const scaleY = 320 / rect.height;
  state.textX = Math.round((e.clientX - rect.left) * scaleX);
  state.textY = Math.round((e.clientY - rect.top) * scaleY);
  // 範囲内に制限
  state.textX = Math.max(0, Math.min(370, state.textX));
  state.textY = Math.max(0, Math.min(320, state.textY));
  renderTextPreview();
}

function eraseAt(e) {
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const scaleX = 370 / rect.width;
  const scaleY = 320 / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top) * scaleY;
  const radius = state.brushSize / 2;

  ctx.save();
  ctx.globalCompositeOperation = "destination-out";
  // ぼかし丸: blurフィルタでソフトエッジ
  const blurAmount = Math.max(radius * 0.4, 3);
  ctx.filter = `blur(${blurAmount}px)`;
  ctx.fillStyle = "rgba(0,0,0,1)";
  ctx.beginPath();
  ctx.arc(x, y, radius * 0.7, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function pushUndo() {
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");
  state.undoStack.push(ctx.getImageData(0, 0, 370, 320));
  if (state.undoStack.length > 10) state.undoStack.shift();
}

function undoOneStep() {
  if (state.undoStack.length <= 1) return;
  state.undoStack.pop();
  const prev = state.undoStack[state.undoStack.length - 1];
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");
  ctx.putImageData(prev, 0, 0);
}

function resetToOriginal() {
  if (state.undoStack.length === 0) return;
  const original = state.undoStack[0];
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");
  ctx.putImageData(original, 0, 0);
  state.undoStack = [original];
}

// --- Text Preview ---
function renderTextPreview() {
  const canvas = $("#text-preview-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 370, 320);

  const text = $("#editor-text-input").value.trim();
  if (!text) return;

  // 座標未設定 → ヒント表示
  if (state.textX == null || state.textY == null) {
    ctx.font = '14px sans-serif';
    ctx.fillStyle = "rgba(0,0,0,0.5)";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("テキストツールでキャンバスをタップ", 370 / 2, 320 / 2);
    return;
  }

  const fontSize = 32;
  const outlineWidth = 3;
  const fontFamily = FONT_FAMILY_MAP[state.selectedFont] || "Zen Maru Gothic";

  // 色設定
  let fillColor, strokeColor;
  if (state.textColor === "black") {
    fillColor = "rgba(0,0,0,1)";
    strokeColor = "rgba(255,255,255,1)";
  } else {
    fillColor = "rgba(255,255,255,1)";
    strokeColor = "rgba(0,0,0,1)";
  }

  ctx.font = `${fontSize}px "${fontFamily}", sans-serif`;
  ctx.lineJoin = "round";

  if (state.vertical) {
    renderVerticalText(ctx, text, state.textX, state.textY, fontSize, outlineWidth, fillColor, strokeColor);
  } else {
    renderHorizontalText(ctx, text, state.textX, state.textY, fontSize, outlineWidth, fillColor, strokeColor);
  }
}

function renderHorizontalText(ctx, text, tx, ty, fontSize, outlineWidth, fillColor, strokeColor) {
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  // 縁取り
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = outlineWidth * 2;
  ctx.strokeText(text, tx, ty);
  // 本文
  ctx.fillStyle = fillColor;
  ctx.fillText(text, tx, ty);
}

function renderVerticalText(ctx, text, tx, ty, fontSize, outlineWidth, fillColor, strokeColor) {
  ctx.textAlign = "center";
  ctx.textBaseline = "top";

  // 各文字のサイズを計算
  const spacing = 2;
  let totalH = 0;
  const charHeights = [];
  for (const ch of text) {
    charHeights.push(fontSize);
    totalH += fontSize + spacing;
  }
  totalH -= spacing;

  const cx = tx;
  const yStart = ty - totalH / 2;

  let yCursor = yStart;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    // 縁取り
    ctx.strokeStyle = strokeColor;
    ctx.lineWidth = outlineWidth * 2;
    ctx.strokeText(ch, cx, yCursor);
    // 本文
    ctx.fillStyle = fillColor;
    ctx.fillText(ch, cx, yCursor);
    yCursor += charHeights[i] + spacing;
  }
}

// --- Move/Zoom ---
function redrawMoveCanvas() {
  if (!state.moveBaseImageData) return;
  const canvas = $("#editor-canvas");
  const ctx = canvas.getContext("2d");

  const tempCanvas = document.createElement("canvas");
  tempCanvas.width = 370;
  tempCanvas.height = 320;
  const tempCtx = tempCanvas.getContext("2d");
  tempCtx.putImageData(state.moveBaseImageData, 0, 0);

  ctx.clearRect(0, 0, 370, 320);
  ctx.save();
  ctx.translate(370 / 2 + state.subjectOffsetX, 320 / 2 + state.subjectOffsetY);
  ctx.scale(state.subjectScale, state.subjectScale);
  ctx.translate(-370 / 2, -320 / 2);
  ctx.drawImage(tempCanvas, 0, 0);
  ctx.restore();
}

function resetPosition() {
  state.subjectOffsetX = 0;
  state.subjectOffsetY = 0;
  state.subjectScale = 1.0;
  $("#zoom-slider").value = 100;
  $("#zoom-val").textContent = "100";
  redrawMoveCanvas();
}

// --- Save ---
async function saveEditedStamp() {
  const canvas = $("#editor-canvas");
  const saveBtn = $("#editor-save");
  saveBtn.disabled = true;
  saveBtn.textContent = "保存中...";

  try {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));

    // テキスト情報を取得
    const text = $("#editor-text-input").value.trim();

    const formData = new FormData();
    formData.append("image", blob, state.editingFilename);
    formData.append("text", text);
    formData.append("text_x", state.textX != null ? Math.round(state.textX) : -1);
    formData.append("text_y", state.textY != null ? Math.round(state.textY) : -1);
    formData.append("font_id", state.selectedFont);
    formData.append("text_color", state.textColor);
    formData.append("vertical", state.vertical);

    const res = await fetch(
      `/api/stamp/${state.sessionId}/${state.editingFilename}`,
      { method: "PUT", body: formData }
    );

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error || "保存に失敗しました");
    }

    // ステートを更新
    state.stampMessages[state.editingIndex] = text;
    state.stampTextX[state.editingIndex] = state.textX;
    state.stampTextY[state.editingIndex] = state.textY;
    state.stampFonts[state.editingIndex] = state.selectedFont;
    state.stampTextColors[state.editingIndex] = state.textColor;
    state.stampVerticals[state.editingIndex] = state.vertical;

    // サムネイルを更新（キャッシュバスト）
    const thumb = $(`#result-grid .result-item[data-index="${state.editingIndex}"] img`);
    if (thumb) {
      thumb.src = `/api/stamp/${state.sessionId}/${state.editingFilename}?t=${Date.now()}`;
    }

    closeEditor();
  } catch (e) {
    alert(e.message);
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "保存する ✓";
  }
}

// --- Navigation ---
function navigateStamp(delta) {
  const newIndex = state.editingIndex + delta;
  if (newIndex < 0 || newIndex > 7) return;
  openEditor(newIndex);
}

function updateNavButtons() {
  $("#editor-prev").disabled = state.editingIndex <= 0;
  $("#editor-next").disabled = state.editingIndex >= 7;
}

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
  initUpload();
  initModeSelection();
  initLineUpload();
  initEditor();

  $("#btn-restart").addEventListener("click", resetApp);
  $("#btn-retry").addEventListener("click", resetApp);
});
