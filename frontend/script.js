/*
 * Transmint demo client — vanilla JavaScript, no build step.
 *
 * CONFIGURATION: set API_BASE_URL to wherever the FastAPI backend runs.
 * You can also override it without editing this file by opening the page with
 * ?api=http://host:port (handy when the default port is busy).
 */
const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";   // for local
// const DEFAULT_API_BASE_URL = "https://transmint.onrender.com";   // for production

const API_BASE_URL = (new URLSearchParams(location.search).get("api") || DEFAULT_API_BASE_URL).replace(/\/+$/, "");

const ALLOWED_EXTENSIONS = [".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"];
const POLL_INTERVAL_MS = 2000;
const MAX_POLL_MINUTES = 60;

const state = {
  mode: "file", // "file" | "youtube"
  file: null,
  transcriptionId: null,
  summaryId: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);
const els = {
  status: $("backend-status"),
  error: $("error"),
  dropZone: $("drop-zone"),
  fileInput: $("file-input"),
  filePanel: $("file-panel"),
  youtubePanel: $("youtube-panel"),
  youtubeUrl: $("youtube-url"),
  preferCaptions: $("prefer-captions"),
  sourceRadios: document.querySelectorAll('input[name="source"]'),
  fileName: $("file-name"),
  transcribeBtn: $("transcribe-btn"),
  transcribeProgress: $("transcribe-progress"),
  transcribeProgressText: $("transcribe-progress-text"),
  transcriptMeta: $("transcript-meta"),
  transcriptText: $("transcript-text"),
  segmentsDetails: $("segments-details"),
  segmentsList: $("segments-list"),
  summaryType: $("summary-type"),
  outputLanguage: $("output-language"),
  summarizeBtn: $("summarize-btn"),
  summarizeProgress: $("summarize-progress"),
  summaryOutput: $("summary-output"),
};

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** Turn any backend error body into a readable message. */
async function errorMessage(response) {
  let detail = null;
  try {
    const body = await response.json();
    detail = body.detail;
  } catch {
    /* non-JSON body */
  }
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg}, ...]
    detail = detail.map((d) => (d.loc ? `${d.loc.slice(1).join(".") || "request"}: ${d.msg}` : d.msg)).join("\n");
  }
  const retry = response.headers.get("Retry-After");
  const suffix = retry ? ` (retry after ${retry}s)` : "";
  return `${detail || response.statusText || "Request failed"}${suffix} [HTTP ${response.status}]`;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, options);
  } catch {
    throw new ApiError(`Cannot reach the backend at ${API_BASE_URL}. Is it running, and is this origin allowed by CORS?`, 0);
  }
  if (!response.ok) throw new ApiError(await errorMessage(response), response.status);
  return response;
}

const apiJson = async (path, options) => (await api(path, options)).json();

// ---------------------------------------------------------------------------
// UI helpers (all untrusted text goes through textContent)
// ---------------------------------------------------------------------------

function showError(message) {
  els.error.textContent = message;
  els.error.hidden = false;
}

function clearError() {
  els.error.textContent = "";
  els.error.hidden = true;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function formatTime(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${String(m).padStart(2, "0")}:${sec}`;
}

function setDownloadButtons(kind, enabled) {
  document.querySelectorAll(`[data-download="${kind}"]`).forEach((b) => (b.disabled = !enabled));
}

function updateButtons() {
  const hasInput = state.mode === "file" ? Boolean(state.file) : els.youtubeUrl.value.trim() !== "";
  els.transcribeBtn.disabled = state.busy || !hasInput;
  els.summarizeBtn.disabled = state.busy || !state.transcriptionId;
}

function setBusy(busy) {
  state.busy = busy;
  els.fileInput.disabled = busy;
  els.youtubeUrl.disabled = busy;
  els.preferCaptions.disabled = busy;
  els.sourceRadios.forEach((r) => (r.disabled = busy));
  updateButtons();
}

// ---------------------------------------------------------------------------
// Input source switch (audio file / YouTube link)
// ---------------------------------------------------------------------------

els.sourceRadios.forEach((radio) =>
  radio.addEventListener("change", () => {
    state.mode = radio.value;
    els.filePanel.hidden = state.mode !== "file";
    els.youtubePanel.hidden = state.mode !== "youtube";
    clearError();
    updateButtons();
  }),
);
els.youtubeUrl.addEventListener("input", updateButtons);
els.youtubeUrl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !els.transcribeBtn.disabled) els.transcribeBtn.click();
});

// ---------------------------------------------------------------------------
// File selection
// ---------------------------------------------------------------------------

function selectFile(file) {
  clearError();
  if (!file) return;
  const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
  if (!ALLOWED_EXTENSIONS.includes(ext)) {
    state.file = null;
    els.fileName.textContent = "none";
    showError(`Unsupported file type "${ext || "none"}". Use one of: ${ALLOWED_EXTENSIONS.join(", ")}.`);
  } else if (file.size === 0) {
    state.file = null;
    showError("The selected file is empty.");
  } else {
    state.file = file;
    els.fileName.textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
  }
  updateButtons();
}

els.fileInput.addEventListener("change", () => selectFile(els.fileInput.files[0]));
["dragenter", "dragover"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.add("dragover");
  }),
);
["dragleave", "drop"].forEach((evt) =>
  els.dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropZone.classList.remove("dragover");
  }),
);
els.dropZone.addEventListener("drop", (e) => {
  if (!state.busy) selectFile(e.dataTransfer.files[0]);
});

// ---------------------------------------------------------------------------
// Transcription
// ---------------------------------------------------------------------------

function resetResults() {
  state.transcriptionId = null;
  state.summaryId = null;
  els.transcriptMeta.textContent = "No transcript yet.";
  els.transcriptText.textContent = "";
  els.segmentsList.replaceChildren();
  els.segmentsDetails.hidden = true;
  els.summaryOutput.replaceChildren();
  setDownloadButtons("transcript", false);
  setDownloadButtons("summary", false);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pollUntilDone(id, isYouTube) {
  const started = Date.now();
  while (Date.now() - started < MAX_POLL_MINUTES * 60_000) {
    const status = await apiJson(`/api/transcriptions/${encodeURIComponent(id)}/status`);
    const elapsed = Math.round((Date.now() - started) / 1000);
    if (status.status === "completed") return;
    if (status.status === "failed") throw new ApiError(`Transcription failed: ${status.error || "unknown error"}`, 0);
    const working = isYouTube
      ? `Fetching captions or downloading and transcribing audio… (${elapsed}s)`
      : `Transcribing… (${elapsed}s). Long recordings take a while.`;
    els.transcribeProgressText.textContent = status.status === "queued" ? `Queued… (${elapsed}s)` : working;
    await sleep(POLL_INTERVAL_MS);
  }
  throw new ApiError(
    `Transcription is still running after ${MAX_POLL_MINUTES} minutes. Check later with job ID ${id}.`,
    0,
  );
}

const TRANSCRIPT_SOURCE_LABELS = {
  whisper: "Whisper",
  youtube_captions: "YouTube captions",
  youtube_auto_captions: "YouTube auto-generated captions",
};

function renderTranscript(record) {
  const parts =
    record.source_type === "youtube"
      ? [`Video: ${record.title || record.source_url}`]
      : [`File: ${record.original_filename}`];
  if (record.transcript_source) parts.push(`Source: ${TRANSCRIPT_SOURCE_LABELS[record.transcript_source] || record.transcript_source}`);
  if (record.language) parts.push(`Language: ${record.language}`);
  if (record.duration_seconds) parts.push(`Duration: ${formatTime(record.duration_seconds)}`);
  els.transcriptMeta.textContent = parts.join(" · ");
  els.transcriptText.textContent = record.text || "(No speech was detected in this recording.)";

  const items = (record.segments || []).map((seg) => {
    const li = el("li");
    li.append(el("span", `[${formatTime(seg.start)}–${formatTime(seg.end)}]`, "time"), el("span", seg.text));
    return li;
  });
  els.segmentsList.replaceChildren(...items);
  els.segmentsDetails.hidden = items.length === 0;
}

/** Start a backend job for the current input and return it. */
async function startJob() {
  if (state.mode === "youtube") {
    return apiJson("/api/transcriptions/youtube", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: els.youtubeUrl.value.trim(), prefer_captions: els.preferCaptions.checked }),
    });
  }
  const form = new FormData();
  form.append("file", state.file);
  return apiJson("/api/transcriptions", { method: "POST", body: form });
}

els.transcribeBtn.addEventListener("click", async () => {
  if (els.transcribeBtn.disabled || state.busy) return;
  clearError();
  if (state.mode === "youtube" && !/^(https?:\/\/)?([\w-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)\//i.test(els.youtubeUrl.value.trim())) {
    showError("Please enter a YouTube video link, e.g. https://www.youtube.com/watch?v=...");
    return;
  }
  resetResults();
  setBusy(true);
  els.transcribeProgress.hidden = false;
  els.transcribeProgressText.textContent = state.mode === "youtube" ? "Submitting link…" : "Uploading…";
  try {
    const job = await startJob();
    await pollUntilDone(job.id, state.mode === "youtube");
    const record = await apiJson(`/api/transcriptions/${encodeURIComponent(job.id)}`);
    renderTranscript(record);
    state.transcriptionId = record.id;
    setDownloadButtons("transcript", true);
  } catch (err) {
    showError(err.message);
  } finally {
    els.transcribeProgress.hidden = true;
    setBusy(false);
  }
});

// ---------------------------------------------------------------------------
// Summarization
// ---------------------------------------------------------------------------

function addListSection(container, title, items, format = (x) => x) {
  if (!items || items.length === 0) return;
  container.append(el("h3", title));
  const ul = el("ul");
  items.forEach((item) => ul.append(format(item)));
  container.append(ul);
}

function renderSummary(summary) {
  const c = summary.content;
  const out = els.summaryOutput;
  out.replaceChildren();
  out.append(el("h3", "Overview"), el("p", c.overview));
  addListSection(out, "Key points", c.key_points, (t) => el("li", t));
  addListSection(out, "Important concepts", c.important_concepts, (x) => {
    const li = el("li");
    li.append(el("strong", x.term), document.createTextNode(x.explanation ? `: ${x.explanation}` : ""));
    return li;
  });
  addListSection(out, "Decisions", c.decisions, (t) => el("li", t));
  addListSection(out, "Suggestions (not confirmed)", c.suggestions, (t) => el("li", t));
  addListSection(out, "Action items", c.action_items, (a) => {
    const li = el("li", a.task);
    const tags = [a.owner && `owner: ${a.owner}`, a.deadline && `deadline: ${a.deadline}`].filter(Boolean);
    if (tags.length) li.append(el("span", ` (${tags.join(", ")})`, "tag"));
    return li;
  });
  addListSection(out, "Follow-up questions", c.follow_up_questions, (t) => el("li", t));
  addListSection(out, "Uncertain or unclear parts", c.uncertainties, (t) => el("li", t));
  out.append(el("p", `Model: ${summary.model} · chunks: ${summary.chunk_count}`, "muted"));
}

els.summarizeBtn.addEventListener("click", async () => {
  if (!state.transcriptionId || state.busy) return;
  clearError();
  const language = els.outputLanguage.value.trim();
  if (language && !/^[A-Za-z][A-Za-z \-()]*$/.test(language)) {
    showError("Output language may only contain letters, spaces, hyphens and parentheses.");
    return;
  }
  setBusy(true);
  els.summarizeProgress.hidden = false;
  els.summaryOutput.replaceChildren();
  setDownloadButtons("summary", false);
  try {
    const body = { transcription_id: state.transcriptionId, summary_type: els.summaryType.value };
    if (language) body.output_language = language;
    const summary = await apiJson("/api/summaries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.summaryId = summary.id;
    renderSummary(summary);
    setDownloadButtons("summary", true);
  } catch (err) {
    showError(err.message);
  } finally {
    els.summarizeProgress.hidden = true;
    setBusy(false);
  }
});

// ---------------------------------------------------------------------------
// Downloads (via the backend export endpoints)
// ---------------------------------------------------------------------------

function filenameFrom(response, fallback) {
  const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
  return match ? match[1] : fallback;
}

document.querySelectorAll("[data-download]").forEach((button) => {
  button.addEventListener("click", async () => {
    clearError();
    const { download: kind, format } = button.dataset;
    const id = kind === "transcript" ? state.transcriptionId : state.summaryId;
    if (!id) return;
    const path = kind === "transcript" ? "transcriptions" : "summaries";
    try {
      const response = await api(`/api/${path}/${encodeURIComponent(id)}/export?format=${format}`);
      const url = URL.createObjectURL(await response.blob());
      const link = el("a");
      link.href = url;
      link.download = filenameFrom(response, `transmint-${kind}.${format}`);
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      showError(err.message);
    }
  });
});

// ---------------------------------------------------------------------------
// Startup: check the backend
// ---------------------------------------------------------------------------

(async function checkHealth() {
  try {
    const health = await apiJson("/api/health");
    const notes = [];
    if (!health.transcription_available) notes.push("transcription engine unavailable");
    if (!health.youtube_available) notes.push("YouTube input unavailable");
    if (!health.summarization_configured) notes.push("summarization not configured (set GROQ_API_KEY)");
    els.status.textContent = `Backend ${health.version} connected at ${API_BASE_URL}` + (notes.length ? ` — ${notes.join("; ")}` : "");
    els.status.className = `status-line ${notes.length ? "warn" : "ok"}`;
  } catch (err) {
    els.status.textContent = err.message;
    els.status.className = "status-line warn";
  }
})();
