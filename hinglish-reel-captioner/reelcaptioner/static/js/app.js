import { api, uploadJob, debounce, fmtBytes, fmtDuration, toast } from "./api.js";
import { CaptionOverlay } from "./preview.js";
import { CardEditor } from "./editor.js";
import { StylePanel } from "./stylepanel.js";

const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback = "") { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode: fine */ } },
};
const ASPECTS = { vertical: 9 / 16, square: 1, landscape: 16 / 9 };
const TAGS = { queued: "In line", working: "Working", ready: "Ready", error: "Stopped", cancelled: "Cancelled" };

const state = {
  status: null, file: null, uploading: false,
  jobId: null, job: null, ready: false, previewSet: false, pollTimer: 0,
};
let overlay;
let editor;
let stylePanel;
let syncChain = Promise.resolve();
let syncSeq = 0;

async function init() {
  overlay = new CaptionOverlay($("frame"), $("player"), $("cap"));
  wireHome();
  try {
    state.status = await api("/api/status");
  } catch (err) {
    setPill("Cannot reach the app");
    showUploadError(err.message);
  }
  if (state.status) {
    stylePanel = new StylePanel({ presets: state.status.presets, onChange: (style, opts) => queueSync({ regroup: opts.regroup }) });
    editor = new CardEditor($("rows"), {
      onTextEdit: () => textSync(),
      onStructure: () => { textSync.cancel(); queueSync({ structure: true }); },
      onSeek: (t) => { $("player").currentTime = t + 0.01; },
      onMessage: (m) => toast(m),
    });
    populateOptions();
    updatePill();
    wireStudio();
  }
  window.addEventListener("hashchange", route);
  route();
}

/** The short part always shows; the tail ("no model needed") is dropped on narrow screens. */
function setPill(main, extra = "") {
  $("statusText").textContent = main;
  $("statusExtra").textContent = extra ? ` \u00b7 ${extra}` : "";
}

function updatePill() {
  const st = state.status;
  const pill = $("statusPill");
  if (!st) return;
  if (!st.tools.ffmpeg) {
    pill.className = "pill warn";
    setPill("FFmpeg not found");
    showUploadError("FFmpeg was not found on this computer. Install it from ffmpeg.org, then restart the app.");
    return;
  }
  if (st.demo) {
    pill.className = "pill demo";
    setPill("Demo mode", "no model needed");
    return;
  }
  const model = st.models.find((m) => m.id === st.default_model);
  const cached = !model || model.cached;
  pill.className = cached ? "pill" : "pill warn";
  setPill(`${st.device.toUpperCase()} \u00b7 ${st.default_model}`, cached ? "" : "downloads on first run");
}

// The note under the model list says what the chosen model is good for and whether it is already downloaded.
function describeModel() {
  const m = state.status.models.find((x) => x.id === $("optModel").value);
  if (!m) return;
  $("modelNote").textContent = `${m.note}. ${m.cached ? "Already on this computer." : "Downloads once, then works offline."}`;
}

function populateOptions() {
  const st = state.status;
  const lang = $("optLanguage");
  for (const l of st.languages) lang.append(new Option(l.label, l.id));
  lang.value = store.get("rc.language", "hinglish");
  const model = $("optModel");
  for (const m of st.models) model.append(new Option(`${m.label}  ${m.cached ? "✓" : "↓"}`, m.id));
  model.value = store.get("rc.model", st.default_model);
  if (!model.value) model.value = st.default_model;
  describeModel();
  model.addEventListener("change", describeModel);
  $("optWords").value = store.get("rc.words", "");
  $("optCanvas").value = store.get("rc.canvas", "vertical");
  const ollama = st.ollama || {};
  $("optPolish").disabled = !ollama.available;
  $("polishNote").textContent = ollama.available
    ? `Cleans up spelling card by card with ${ollama.picked}. Never changes timing or word count.`
    : "Ollama was not detected on this computer. Start it with a model like gemma3 to enable this.";
  for (const [id, key] of [["optLanguage", "rc.language"], ["optModel", "rc.model"], ["optCanvas", "rc.canvas"]]) {
    $(id).addEventListener("change", () => store.set(key, $(id).value));
  }
  $("optWords").addEventListener("input", () => store.set("rc.words", $("optWords").value));
}

function wireHome() {
  const target = $("dropTarget");
  const input = $("fileInput");
  ["dragenter", "dragover"].forEach((ev) => target.addEventListener(ev, (e) => { e.preventDefault(); target.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => target.addEventListener(ev, (e) => { e.preventDefault(); target.classList.remove("over"); }));
  target.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => { if (input.files[0]) setFile(input.files[0]); });
  $("uploadForm").addEventListener("submit", startUpload);
  $("brandLink").addEventListener("click", () => { if (location.hash === "#/" || location.hash === "") route(); });
}

function showUploadError(message) {
  const el = $("uploadError");
  el.textContent = message;
  el.hidden = !message;
}

function setFile(file) {
  showUploadError("");
  const limit = state.status ? state.status.max_upload_mb * 1024 * 1024 : Infinity;
  if (file.size > limit) {
    showUploadError(`That file is ${fmtBytes(file.size)}. The limit is ${state.status.max_upload_mb} MB.`);
    return;
  }
  state.file = file;
  $("dropTarget").classList.add("has-file");
  $("dropTitle").textContent = file.name;
  $("dropSub").textContent = `${fmtBytes(file.size)} · click to choose a different file`;
  $("goBtn").disabled = !state.status;
  $("goHint").textContent = "Ready when you are. Open Options to pick a language or model.";
}

async function startUpload(event) {
  event.preventDefault();
  if (!state.file || state.uploading || !state.status) return;
  showUploadError("");
  const form = new FormData();
  form.append("file", state.file);
  form.append("language", $("optLanguage").value);
  form.append("model", $("optCustom").value.trim() || $("optModel").value);
  form.append("hints", $("optHints").value.trim());
  form.append("user_words", $("optWords").value);
  form.append("polish", $("optPolish").checked ? "1" : "0");
  form.append("canvas", $("optCanvas").value);
  const brand = StylePanel.brand();
  if (brand) form.append("style", JSON.stringify({ ...brand, preset: "brand" }));

  state.uploading = true;
  $("goBtn").disabled = true;
  const label = $("goBtn").lastChild;
  try {
    const { id } = await uploadJob(form, (p) => {
      $("goHint").textContent = `Uploading to this computer… ${Math.round(p * 100)}%`;
    });
    state.file = null;
    $("dropTarget").classList.remove("has-file");
    $("dropTitle").textContent = "Drop a video or voice note";
    $("dropSub").textContent = "or click to choose · MP4, MOV, WebM, MP3, WAV, M4A…";
    $("fileInput").value = "";
    $("goHint").textContent = "Pick a file to begin.";
    location.hash = `#/job/${id}`;
  } catch (err) {
    showUploadError(err.message);
    $("goHint").textContent = "Try again, or pick another file.";
    $("goBtn").disabled = false;
  } finally {
    state.uploading = false;
    if (label) $("goBtn").disabled = !state.file;
  }
}

async function refreshRecent() {
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch { return; }
  const list = $("recentList");
  list.replaceChildren();
  $("recent").hidden = jobs.length === 0;
  for (const job of jobs.slice(0, 8)) {
    const li = document.createElement("li");
    const left = document.createElement("div");
    const name = document.createElement("div");
    name.className = "nm";
    name.textContent = job.name;
    const meta = document.createElement("div");
    meta.className = "meta";
    const when = new Date(job.created * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    const dur = job.info && job.info.duration ? ` · ${fmtDuration(job.info.duration)}` : "";
    meta.textContent = `${when}${dur}`;
    left.append(name, meta);
    const right = document.createElement("div");
    right.className = "row-inline";
    const tag = document.createElement("span");
    tag.className = `tag ${job.status}`;
    tag.textContent = TAGS[job.status] || job.status;
    const open = document.createElement("a");
    open.className = "btn small";
    open.href = `#/job/${job.id}`;
    open.textContent = "Open";
    right.append(tag, open);
    li.append(left, right);
    list.append(li);
  }
}

function route() {
  const match = location.hash.match(/^#\/job\/([a-f0-9]+)$/);
  if (match && state.status) openJob(match[1]);
  else showHome();
}

function showHome() {
  stopPolling();
  state.jobId = null;
  state.job = null;
  const player = $("player");
  player.pause();
  $("studio").hidden = true;
  $("home").hidden = false;
  refreshRecent();
  window.scrollTo({ top: 0 });
}

async function openJob(id) {
  stopPolling();
  state.jobId = id;
  state.job = null;
  state.ready = false;
  state.previewSet = false;
  $("home").hidden = true;
  $("studio").hidden = false;
  window.scrollTo({ top: 0 });
  resetStudio();
  try {
    const job = await api(`/api/jobs/${id}`);
    if (state.jobId !== id) return;
    await applyJob(job);
    keepPolling(job);
  } catch (err) {
    toast(err.message);
    location.hash = "#/";
  }
}

function resetStudio() {
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  $("panel").hidden = true;
  $("progress").hidden = false;
  $("progError").hidden = true;
  $("cancelBtn").textContent = "Cancel";
  $("jobName").textContent = "";
  $("jobTag").textContent = "";
  $("jobTag").className = "tag";
  $("rows").replaceChildren();
  $("notes").replaceChildren();
  setBar($("progBar"), 0);
  overlay.setData({ cards: [], style: overlay.style || null, position: overlay.position });
  resetRenderUi();
  fit();
}

function stopPolling() { clearTimeout(state.pollTimer); }

function keepPolling(job) {
  stopPolling();
  const busy = ["queued", "working"].includes(job.status) || (job.task && job.task.status === "working");
  if (!busy) return;
  const id = state.jobId;
  state.pollTimer = setTimeout(async () => {
    if (state.jobId !== id) return;
    try {
      const next = await api(`/api/jobs/${id}`);
      if (state.jobId !== id) return;
      await applyJob(next);
      keepPolling(next);
    } catch (err) {
      if (err.status === 404) { toast("That project was deleted."); location.hash = "#/"; return; }
      state.pollTimer = setTimeout(() => keepPolling(job), 1500);
    }
  }, 700);
}

function aspectOf(job) {
  const info = job && job.info;
  if (info && info.has_video && info.width && info.height) return info.width / info.height;
  return ASPECTS[(job && job.options && job.options.canvas) || "vertical"] || 9 / 16;
}

function fit() {
  const full = document.fullscreenElement === $("stage");
  const ar = aspectOf(state.job);
  const col = $("stage").parentElement;
  const availW = full ? window.innerWidth : Math.max(200, col.clientWidth - 14);
  const availH = full ? window.innerHeight : Math.max(320, window.innerHeight * 0.74);
  const w = Math.min(availW, availH * ar);
  const frame = $("frame");
  frame.style.width = `${Math.round(w)}px`;
  frame.style.height = `${Math.round(w / ar)}px`;
  overlay.resize();
}

function setBar(bar, fraction) {
  const pct = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
  bar.firstElementChild.style.width = `${pct}%`;
  bar.setAttribute("aria-valuenow", String(pct));
  bar.classList.toggle("done", pct >= 100);
}

async function applyJob(job) {
  const aspectChanged = aspectOf(state.job) !== aspectOf(job);
  state.job = job;
  $("jobName").textContent = job.name;
  const tag = $("jobTag");
  tag.className = `tag ${job.status}`;
  tag.textContent = TAGS[job.status] || job.status;
  $("stage").classList.toggle("audio", !!(job.info && job.info.duration && !job.info.has_video));
  if (aspectChanged) fit();

  if (job.preview && !state.previewSet) {
    state.previewSet = true;
    $("player").src = job.preview;
  }

  if (job.status === "queued" || job.status === "working") {
    $("progress").hidden = false;
    $("progTitle").textContent = job.stage || "Working on it";
    $("progStage").textContent = job.status === "queued" ? "Waiting in line" : "This stays on your machine";
    $("progPct").textContent = `${Math.round((job.progress || 0) * 100)}%`;
    setBar($("progBar"), job.progress || 0);
    $("cancelBtn").hidden = false;
    $("progError").hidden = true;
  } else if (job.status === "error" || job.status === "cancelled") {
    $("progress").hidden = false;
    $("progTitle").textContent = job.status === "cancelled" ? "Cancelled" : "That did not work";
    $("progStage").textContent = "";
    $("progPct").textContent = "";
    setBar($("progBar"), 0);
    $("progError").textContent = job.error || "The job was cancelled.";
    $("progError").hidden = !job.error && job.status !== "error";
    $("cancelBtn").textContent = "Back to start";
    $("cancelBtn").hidden = false;
  } else if (job.status === "ready") {
    if (!state.ready) await loadCards();
    updateRenderUi(job.task);
  }
}

async function loadCards() {
  const id = state.jobId;
  const data = await api(`/api/jobs/${id}/cards`);
  if (state.jobId !== id) return;
  state.ready = true;
  stylePanel.set(data.style);
  editor.setCards(data.cards);
  overlay.setData(data);
  $("progress").hidden = true;
  $("panel").hidden = false;
  const notes = $("notes");
  notes.replaceChildren();
  for (const text of (state.job && state.job.notes) || []) {
    const p = document.createElement("p");
    p.className = "notice";
    p.textContent = text;
    notes.append(p);
  }
  fit();
}

const textSync = debounce(() => queueSync({}), 600);

function queueSync(options) {
  syncChain = syncChain.then(() => doSync(options)).catch(() => {});
  return syncChain;
}

async function flushPending() {
  textSync.cancel();
  await queueSync({});
}

async function doSync({ regroup = false, structure = false }) {
  const id = state.jobId;
  if (!id || !state.ready) return;
  const seq = ++syncSeq;
  const sentRev = editor.rev;
  const sentStyleRev = stylePanel.rev;
  markRenderStale();
  try {
    const body = regroup ? { style: stylePanel.style } : { style: stylePanel.style, cards: editor.payload() };
    const data = await api(`/api/jobs/${id}/${regroup ? "regroup" : "sync"}`, { method: "POST", json: body });
    if (id !== state.jobId || seq !== syncSeq) return;
    overlay.setData(data);
    if (regroup || structure) editor.setCards(data.cards);
    else if (editor.rev === sentRev) editor.mergeServer(data.cards);
    if (stylePanel.rev === sentStyleRev) stylePanel.set(data.style);
    if (regroup) toast("Captions re-flowed to the new card size");
  } catch (err) {
    toast(err.message);
  }
}

function wireStudio() {
  const player = $("player");
  const syncActive = () => {
    const card = overlay.findCard(player.currentTime || 0);
    editor.setActive(card ? card.id : null);
  };
  player.addEventListener("timeupdate", syncActive);
  player.addEventListener("seeked", syncActive);
  window.addEventListener("resize", debounce(fit, 80));
  document.addEventListener("fullscreenchange", fit);
  $("fsBtn").addEventListener("click", () => {
    const stage = $("stage");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (stage.requestFullscreen) stage.requestFullscreen();
  });
  $("backBtn").addEventListener("click", () => { location.hash = "#/"; });

  $("cancelBtn").addEventListener("click", async () => {
    const job = state.job;
    if (job && (job.status === "queued" || job.status === "working")) {
      try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST", json: {} }); toast("Cancelling…"); } catch (err) { toast(err.message); }
    } else {
      location.hash = "#/";
    }
  });
  $("deleteBtn").addEventListener("click", async () => {
    if (!state.jobId || !confirm("Delete this project and its files from this computer?")) return;
    try {
      await api(`/api/jobs/${state.jobId}`, { method: "DELETE" });
      toast("Project deleted");
      location.hash = "#/";
    } catch (err) { toast(err.message); }
  });

  const tabs = ["captions", "style", "export"];
  const select = (name) => {
    for (const t of tabs) {
      $(`t-${t}`).setAttribute("aria-selected", String(t === name));
      $(`tab-${t}`).hidden = t !== name;
    }
  };
  tabs.forEach((t, i) => {
    const el = $(`t-${t}`);
    el.addEventListener("click", () => select(t));
    el.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      select(next);
      $(`t-${next}`).focus();
    });
  });

  $("renderBtn").addEventListener("click", async () => {
    $("renderBtn").disabled = true;
    try {
      await flushPending();
      const job = await api(`/api/jobs/${state.jobId}/render`, { method: "POST", json: { max_height: Number($("renderSize").value) } });
      state.job = job;
      updateRenderUi(job.task);
      keepPolling(job);
    } catch (err) {
      toast(err.message);
      $("renderBtn").disabled = false;
    }
  });

  for (const [id, kind] of [["dlSrt", "srt"], ["dlVtt", "vtt"], ["dlAss", "ass"], ["dlTxt", "txt"]]) {
    $(id).addEventListener("click", async (event) => {
      event.preventDefault();
      await flushPending();
      window.location.href = `/api/jobs/${state.jobId}/download/${kind}`;
    });
  }
}

function resetRenderUi() {
  $("renderBar").hidden = true;
  $("renderMsg").textContent = "";
  $("renderDl").hidden = true;
  $("renderBtn").disabled = false;
  $("renderBtn").textContent = "Burn captions in";
}

function updateRenderUi(task) {
  if (!task || task.kind !== "render" || task.status === "idle") return;
  const bar = $("renderBar");
  if (task.status === "working") {
    bar.hidden = false;
    setBar(bar, task.progress || 0);
    $("renderMsg").textContent = `${task.message || "Working"} · ${Math.round((task.progress || 0) * 100)}%`;
    $("renderBtn").disabled = true;
    $("renderDl").hidden = true;
  } else if (task.status === "done") {
    bar.hidden = true;
    $("renderMsg").textContent = task.message || "Your video is ready";
    $("renderBtn").disabled = false;
    $("renderBtn").textContent = "Burn again";
    const link = $("renderDl");
    link.href = task.url;
    link.hidden = !task.url;
  } else if (task.status === "error") {
    bar.hidden = true;
    $("renderMsg").textContent = task.message || "The render did not finish.";
    $("renderBtn").disabled = false;
    $("renderDl").hidden = true;
  }
}

function markRenderStale() {
  const link = $("renderDl");
  if (!link.hidden) {
    link.hidden = true;
    $("renderMsg").textContent = "Captions changed since the last render. Burn again to update the video.";
  }
}

init();
