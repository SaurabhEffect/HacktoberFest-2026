/* The Won't-Do List - front end.
   Plain JavaScript, no build step, no network calls except to this app's own server.
   Everything that comes from the model or from the person is put on the page with
   textContent (never innerHTML), so a task like "<img onerror=...>" is just text. */
"use strict";

(() => {
  const BUCKETS = ["now", "later", "expire", "notmine"];
  const KEEP = ["now", "later"];
  const SKIP = ["expire", "notmine"];
  const LABELS = { now: "Do now", later: "Do later", expire: "Let it expire", notmine: "Not my problem" };
  const BLURBS = {
    now: "Ignoring these costs you soon, or they take under ten minutes.",
    later: "They matter, but nothing breaks this week if they wait.",
    expire: "Nothing bad happens if these never get done.",
    notmine: "Someone else owns these, or you are waiting on them.",
  };
  const EMPTY = {
    now: "Nothing urgent. Enjoy the quiet.",
    later: "Nothing waiting in the wings.",
    expire: "Nothing to let go of. Impressive, or suspicious.",
    notmine: "Nothing to hand back.",
  };
  const RISK_LABEL = { low: "Low risk", medium: "Medium risk", high: "High risk" };
  const DRAFT_KEY = "wontdo.draft";
  const SAMPLE = [
    "Pay electricity bill by Friday",
    "Renew passport (expires in 5 months)",
    "Reply to the webinar invite",
    "Learn Rust someday",
    "Reorganize the photo folders",
    "Waiting for the landlord to fix the tap",
    "Book a dentist check-up",
    "Read that 40-page onboarding report",
    "Unsubscribe from 14 newsletters",
    "Call mum about Sunday",
    "Submit the expense report by tomorrow",
    "Buy a standing desk",
    "Try that sourdough recipe",
  ].join("\n");

  const $ = (selector, root = document) => root.querySelector(selector);
  const PROPS = new Set(["value", "checked", "disabled", "hidden", "selected", "readOnly"]);

  function append(node, kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false) continue;
      if (Array.isArray(kid)) append(node, kid);
      else node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
  }

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (PROPS.has(key)) node[key] = value;
        else node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    append(node, kids);
    return node;
  }

  // Like replaceChildren, but understands arrays, null and strings the way h() does.
  function fill(node, ...kids) {
    node.replaceChildren();
    append(node, kids);
    return node;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICONS = {
    check: "M20 6L9 17l-5-5",
    x: "M18 6L6 18M6 6l12 12",
    arrow: "M5 12h14M12 5l7 7-7 7",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4",
    clock: "M12 2a10 10 0 100 20 10 10 0 000-20z M12 6v6l4 2",
    calendar: "M5 4h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V6a2 2 0 012-2z M16 2v4M8 2v4M3 10h18",
    bell: "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.7 21a2 2 0 01-3.4 0",
    hand: "M7 17L17 7M7 7h10v10",
    download: "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3",
    copy: "M9 9h11v11H9z M5 15H4a1 1 0 01-1-1V4a1 1 0 011-1h10a1 1 0 011 1v1",
    trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6",
    alert: "M12 9v4M12 17h.01 M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
    info: "M12 2a10 10 0 100 20 10 10 0 000-20z M12 16v-4M12 8h.01",
    undo: "M1 4v6h6 M3.5 15a9 9 0 102.1-9.4L1 10",
    print: "M6 9V2h12v7 M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2 M6 14h12v8H6z",
    zap: "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", ICONS[name]);
    svg.append(path);
    return svg;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function parseDay(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
  }
  function fmtDay(iso) {
    const day = parseDay(iso);
    return day ? day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) : "";
  }
  function fmtStamp(iso) {
    const when = new Date(iso);
    return Number.isNaN(when.getTime())
      ? ""
      : when.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function fmtMinutes(value) {
    const total = Math.max(0, Math.round(Number(value) || 0));
    if (total < 60) return `${total} min`;
    const hours = Math.floor(total / 60);
    const rest = total % 60;
    return rest ? `${hours} h ${rest} min` : `${hours} h`;
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const shown = (item) => item.user_bucket || item.bucket;
  const clip = (text, limit) => (text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text);

  class ApiFailure extends Error {
    constructor(status, error) {
      super((error && error.message) || "Something went wrong.");
      this.status = status;
      this.code = (error && error.code) || "error";
      this.hint = (error && error.hint) || "";
    }
  }

  async function api(method, path, body) {
    let response;
    try {
      response = await fetch(path, {
        method,
        cache: "no-store",
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      throw new ApiFailure(0, {
        code: "offline",
        message: "The app's server is not answering.",
        hint: "Is the terminal window that started it still open?",
      });
    }
    let data = null;
    if ((response.headers.get("content-type") || "").includes("json")) {
      try {
        data = await response.json();
      } catch (err) {
        data = null;
      }
    }
    if (!response.ok) {
      throw new ApiFailure(response.status, (data && data.error) || { code: "http", message: `The server answered ${response.status}.` });
    }
    return data;
  }

  const state = { status: null, settings: null, current: null, ephemeral: null, busy: false, job: null, routeToken: 0, parseTick: 0, statusAt: 0 };
  const els = {};

  function cacheElements() {
    for (const [key, id] of Object.entries({
      form: "composer", tasks: "tasks", budget: "budget", budgetOut: "budget-out", sortBtn: "sort-btn", sampleBtn: "sample-btn",
      parseNote: "parse-note", progress: "progress", progressText: "progress-text", progressBar: "progress-bar", cancelBtn: "cancel-btn",
      sortError: "sort-error", banner: "model-banner", chip: "model-chip", chipText: "model-chip-text", badge: "checkin-badge", toast: "toast",
      listRoot: "list-root", checkinsRoot: "checkins-root", historyRoot: "history-root", settingsRoot: "settings-root", main: "main",
    })) {
      els[key] = document.getElementById(id);
    }
  }

  let toastTimer = null;
  function hideToast() {
    clearTimeout(toastTimer);
    els.toast.hidden = true;
    els.toast.replaceChildren();
  }
  function toast(message, { action, onAction, ms = 6500 } = {}) {
    clearTimeout(toastTimer);
    fill(els.toast, 
      h("span", { text: message }),
      action
        ? h("button", { type: "button", onclick: () => { hideToast(); if (onAction) onAction(); }, text: action })
        : null
    );
    els.toast.hidden = false;
    toastTimer = setTimeout(hideToast, ms);
  }

  function setChip(kind, text) {
    els.chip.dataset.state = kind;
    els.chip.title = text;
    els.chipText.textContent = text;
  }

  async function refreshStatus(force = false) {
    try {
      state.status = await api("GET", "/api/status" + (force ? "?refresh=1" : ""));
      state.statusAt = Date.now();
    } catch (err) {
      state.status = null;
    }
    renderStatus();
  }

  function renderStatus() {
    const status = state.status;
    if (!status) {
      setChip("down", "App server not answering");
    } else if (status.mode === "model") {
      const backend = status.backend;
      setChip("model", `${backend.model} · ${backend.provider === "ollama" ? "Ollama" : "local server"}`);
    } else if (status.backend.reachable) {
      setChip("demo", status.backend.model_missing ? "Model missing · demo mode" : "Demo mode");
    } else {
      setChip("demo", "No model found · demo mode");
    }
    renderBanner();
  }

  function renderBanner() {
    const status = state.status;
    const banner = els.banner;
    if (status && status.mode === "model") {
      banner.hidden = true;
      banner.replaceChildren();
      return;
    }
    banner.hidden = false;
    if (!status) {
      fill(banner, h("p", null, h("strong", null, "The app's server is not answering. "), "Is the terminal window that started it still open?"));
      return;
    }
    const backend = status.backend;
    if (backend.reachable) {
      fill(banner, 
        h("p", null, h("strong", null, "A model server is running, but there is no model to use. "), backend.error || ""),
        backend.hint ? h("p", null, backend.hint) : null,
        h("a", { class: "btn btn-small", href: "#/settings" }, "Open settings")
      );
      return;
    }
    fill(banner, 
      h("p", null, h("strong", null, "No local model found. "), "You can still try the app: it will sort with simple keyword rules, which are not AI and are labelled as such."),
      h("p", null, "For the real thing, install ", h("a", { href: "https://ollama.com", target: "_blank", rel: "noopener noreferrer" }, "Ollama"), " and run ", h("code", null, "ollama pull gemma3:4b"), ". This page notices on its own."),
      h("a", { class: "btn btn-small", href: "#/settings" }, "Test connection")
    );
  }

  async function refreshCheckinBadge() {
    try {
      const data = await api("GET", "/api/checkins");
      const due = data.stats.due;
      els.badge.hidden = due === 0;
      els.badge.textContent = String(due);
      els.badge.setAttribute("aria-label", `${plural(due, "check-in")} due`);
    } catch (err) {
      els.badge.hidden = true;
    }
  }

  const VIEW_TITLES = {
    sort: "The Won’t-Do List",
    list: "Your sorted list · The Won’t-Do List",
    checkins: "Check-ins · The Won’t-Do List",
    history: "History · The Won’t-Do List",
    settings: "Settings · The Won’t-Do List",
  };

  function routeInfo() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    return { name: parts[0] || "sort", arg: parts[1] ? decodeURIComponent(parts[1]) : "" };
  }

  function showView(view) {
    for (const section of document.querySelectorAll("[data-view]")) section.hidden = section.dataset.view !== view;
    const navKey = view === "list" ? "sort" : view;
    for (const link of document.querySelectorAll("[data-nav]")) {
      if (link.dataset.nav === navKey) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    document.title = VIEW_TITLES[view] || VIEW_TITLES.sort;
  }

  function focusHeading(view) {
    const heading = document.querySelector(`[data-view="${view}"] h1`);
    window.scrollTo(0, 0);
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  }

  function loading(label) {
    return h("p", { class: "lede", role: "status" }, label);
  }

  async function route() {
    const token = ++state.routeToken;
    const { name, arg } = routeInfo();
    const fresh = () => token === state.routeToken;
    let view = "sort";
    if (name === "list" || name === "result") view = "list";
    else if (["checkins", "history", "settings"].includes(name)) view = name;
    showView(view);

    try {
      if (view === "list") {
        if (name === "result") {
          if (state.ephemeral) renderList(state.ephemeral, { readOnly: true });
          else location.replace("#/");
        } else {
          fill(els.listRoot, loading("Opening your list…"));
          const result = await api("GET", `/api/history/${encodeURIComponent(arg)}`);
          if (!fresh()) return;
          renderList(result);
        }
      } else if (view === "checkins") {
        fill(els.checkinsRoot, loading("Looking for check-ins…"));
        const data = await api("GET", "/api/checkins");
        if (!fresh()) return;
        renderCheckins(data);
      } else if (view === "history") {
        fill(els.historyRoot, loading("Opening your history…"));
        const data = await api("GET", "/api/history");
        if (!fresh()) return;
        renderHistory(data.items);
      } else if (view === "settings") {
        fill(els.settingsRoot, loading("Loading settings…"));
        const [settings] = await Promise.all([api("GET", "/api/settings"), refreshStatus(true)]);
        if (!fresh()) return;
        state.settings = settings;
        renderSettings(settings);
      } else {
        refreshStatus();
      }
    } catch (err) {
      if (!fresh()) return;
      const root = { list: els.listRoot, checkins: els.checkinsRoot, history: els.historyRoot, settings: els.settingsRoot }[view];
      if (root) fill(root, errorCard(err));
    }
    if (fresh()) focusHeading(view);
  }

  function errorCard(err) {
    return h("div", { class: "empty-card" },
      h("img", { src: "img/sloth.svg", alt: "", width: "220", height: "210" }),
      h("h1", { class: "page-title-small" }, err.status === 404 ? "That one is gone." : "Something went wrong."),
      h("p", null, err.message || "Please try again."),
      err.hint ? h("p", { class: "hint" }, err.hint) : null,
      h("a", { class: "btn", href: "#/" }, "Back to the start")
    );
  }

  function readDraft() {
    try { return sessionStorage.getItem(DRAFT_KEY) || ""; } catch (err) { return ""; }
  }
  function saveDraft(text) {
    try { if (text) sessionStorage.setItem(DRAFT_KEY, text); else sessionStorage.removeItem(DRAFT_KEY); } catch (err) { /* private mode: fine */ }
  }

  function budgetLabel(value) {
    return Number(value) === 0 ? "No limit" : fmtMinutes(value);
  }

  function applySettingsToComposer() {
    const settings = state.settings;
    if (!settings) return;
    els.budget.max = String(Math.max(480, settings.budget_minutes));
    els.budget.value = String(settings.budget_minutes);
    els.budgetOut.textContent = budgetLabel(els.budget.value);
  }

  let parseTimer = null;
  function scheduleParse() {
    clearTimeout(parseTimer);
    parseTimer = setTimeout(updateParseNote, 250);
  }

  async function updateParseNote() {
    const text = els.tasks.value;
    saveDraft(text);
    const tick = ++state.parseTick;
    if (!text.trim()) {
      els.parseNote.textContent = "Anything you have already ticked off is skipped.";
      return;
    }
    try {
      const info = await api("POST", "/api/parse", { text });
      if (tick !== state.parseTick) return;
      if (info.error) {
        els.parseNote.textContent = info.error.message;
        return;
      }
      const bits = [plural(info.count, "task") + " found"];
      if (info.done_skipped) bits.push(`${plural(info.done_skipped, "ticked-off item")} skipped`);
      if (info.duplicates_merged) bits.push(`${plural(info.duplicates_merged, "duplicate")} merged`);
      if (info.truncated) bits.push(`${plural(info.truncated, "very long task")} shortened`);
      let note = bits.join(" · ");
      if (info.hint === "one_line") note += ". That looks like one long line: put each task on its own line, or separate them with semicolons.";
      els.parseNote.textContent = note;
    } catch (err) {
      if (tick === state.parseTick) els.parseNote.textContent = "";
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    els.sortBtn.disabled = busy;
    els.sampleBtn.disabled = busy;
    els.sortBtn.textContent = busy ? "Sorting…" : "Sort my list";
    els.progress.hidden = !busy;
    if (busy) {
      els.progressText.textContent = "Starting…";
      els.progressBar.removeAttribute("value");
    }
  }

  function renderProgress(snapshot) {
    const progress = snapshot.progress || {};
    els.progressText.textContent = progress.message || "Working…";
    if (progress.total > 0) {
      els.progressBar.max = progress.total;
      els.progressBar.value = progress.done;
    } else {
      els.progressBar.removeAttribute("value");
    }
  }

  async function pollJob(jobId) {
    for (;;) {
      const snapshot = await api("GET", `/api/jobs/${jobId}`);
      renderProgress(snapshot);
      if (snapshot.status !== "running") return snapshot;
      await sleep(300);
    }
  }

  const ERROR_TITLES = {
    no_model: "No model to ask.",
    model_missing: "That model is not installed.",
    unreachable: "The model server went away.",
    timeout: "The model took too long.",
    http: "The model server had a problem.",
    format: "The model's answer did not make sense.",
    remote_blocked: "That server is not on this computer.",
    busy: "Already sorting a list.",
    offline: "The app is not answering.",
    empty: "Nothing to sort yet.",
    too_many: "That is a lot of tasks.",
    too_long: "That is a very long list.",
  };
  const MODEL_PROBLEMS = new Set(["no_model", "model_missing", "unreachable", "timeout", "http", "format", "remote_blocked"]);

  function hideSortError() {
    els.sortError.hidden = true;
    els.sortError.replaceChildren();
  }

  function showSortError(err) {
    const code = err.code || "error";
    const actions = [];
    if (MODEL_PROBLEMS.has(code)) {
      actions.push(h("button", { class: "btn btn-small", type: "button", onclick: () => startSort("demo") }, "Sort with keyword rules instead"));
      actions.push(h("a", { class: "btn btn-small btn-ghost", href: "#/settings" }, "Check settings"));
    }
    fill(els.sortError, 
      h("p", null, h("strong", null, ERROR_TITLES[code] || "Something went wrong."), " ", err.message || ""),
      err.hint ? h("p", null, err.hint) : null,
      actions.length ? h("p", null, actions) : null
    );
    els.sortError.hidden = false;
    els.sortError.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  async function startSort(engine = "auto") {
    if (state.busy) return;
    hideSortError();
    const text = els.tasks.value;
    if (!text.trim()) {
      showSortError({ code: "empty", message: "Paste a few tasks first. One per line is plenty." });
      els.tasks.focus();
      return;
    }
    setBusy(true);
    try {
      const { job } = await api("POST", "/api/sort", { text, budget: Number(els.budget.value), engine });
      state.job = job;
      const snapshot = await pollJob(job);
      if (snapshot.status === "done") {
        finishSort(snapshot.result);
      } else if (snapshot.status === "cancelled") {
        toast("Cancelled. Nothing was saved.");
      } else {
        showSortError(snapshot.error || { message: "Sorting failed." });
      }
    } catch (err) {
      showSortError(err);
    } finally {
      state.job = null;
      setBusy(false);
    }
  }

  function finishSort(result) {
    if (result.sid) {
      location.hash = `#/list/${encodeURIComponent(result.sid)}`;
    } else {
      state.ephemeral = result;
      location.hash = "#/result";
    }
    refreshCheckinBadge();
  }

  async function cancelSort() {
    if (!state.job) return;
    els.cancelBtn.disabled = true;
    try {
      await api("POST", `/api/jobs/${state.job}/cancel`, {});
      els.progressText.textContent = "Cancelling after the current batch…";
    } catch (err) {
      toast(err.message);
    } finally {
      els.cancelBtn.disabled = false;
    }
  }

  function wireComposer() {
    els.tasks.value = readDraft();
    els.form.addEventListener("submit", (event) => { event.preventDefault(); startSort("auto"); });
    els.tasks.addEventListener("input", scheduleParse);
    els.tasks.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); startSort("auto"); }
    });
    els.budget.addEventListener("input", () => { els.budgetOut.textContent = budgetLabel(els.budget.value); });
    els.sampleBtn.addEventListener("click", () => {
      els.tasks.value = SAMPLE;
      updateParseNote();
      els.tasks.focus();
    });
    els.cancelBtn.addEventListener("click", cancelSort);
    if (els.tasks.value) updateParseNote();
  }

  function engineLine(engine) {
    if (!engine || engine.kind === "demo") return "keyword rules (not AI)";
    const name = engine.model || "a local model";
    const where = engine.provider === "ollama" ? " via Ollama" : engine.provider ? " via a local server" : "";
    return engine.kind === "mixed" ? `${name}${where}, with keyword rules for some tasks` : name + where;
  }

  function plainText(result) {
    const byId = new Map(result.tasks.map((task) => [task.id, task]));
    const lines = [`The Won't-Do List · ${result.today}`, ""];
    for (const bucket of BUCKETS) {
      const items = (result.layout[bucket] || []).map((id) => byId.get(id)).filter(Boolean);
      if (!items.length) continue;
      lines.push(`${LABELS[bucket]} (${items.length})`);
      for (const item of items) lines.push(`${KEEP.includes(bucket) ? "[ ]" : "-"} ${item.text} (~${fmtMinutes(item.minutes)})`);
      lines.push("");
    }
    return lines.join("\n").trimEnd() + "\n";
  }

  async function copyText(text, done) {
    try {
      await navigator.clipboard.writeText(text);
      toast(done);
    } catch (err) {
      toast("Could not copy. Use one of the download options instead.");
    }
  }

  function exportMenu(result) {
    const base = `/api/export/${encodeURIComponent(result.sid)}`;
    const link = (label, fmt) => h("a", { href: `${base}?fmt=${fmt}&download=1`, download: "" }, icon("download"), label);
    return h("details", { class: "menu" },
      h("summary", { class: "btn" }, icon("download"), "Export"),
      h("div", { class: "menu-list" },
        h("button", { type: "button", onclick: async () => {
          try {
            const response = await fetch(`${base}?fmt=md`, { cache: "no-store" });
            if (!response.ok) throw new Error("export failed");
            await copyText(await response.text(), "Copied as Markdown.");
          } catch (err) {
            toast("Could not copy the list.");
          }
        } }, icon("copy"), "Copy as Markdown"),
        link("Markdown file", "md"),
        link("Spreadsheet (CSV)", "csv"),
        link("Calendar file (.ics)", "ics"),
        link("Everything (JSON)", "json")
      )
    );
  }

  function flagNote(flag) {
    const kind = { safety: "shield", consistency: "shield", due: "shield", budget: "clock", fallback: "info" }[flag.kind] || "info";
    const css = { budget: "note-budget", fallback: "note-fallback" }[flag.kind] || "";
    const title = /[.!?:]$/.test(flag.title) ? flag.title : `${flag.title}.`;
    return h("p", { class: `note ${css}` }, icon(kind), h("span", null, h("strong", null, title), flag.detail ? ` ${flag.detail}` : ""));
  }

  function renderTask(item, editable) {
    const bucket = shown(item);
    const chips = [h("span", { class: "chip" }, icon("clock"), `~${fmtMinutes(item.minutes)}`)];
    if (item.due) {
      chips.push(h("span", { class: "chip" }, icon("calendar"), `due ${fmtDay(item.due.date)}${item.due.fuzzy ? " (approx.)" : ""}`));
    }
    if (RISK_LABEL[item.risk]) chips.push(h("span", { class: `chip risk-${item.risk}` }, RISK_LABEL[item.risk]));

    const notes = (item.flags || []).map(flagNote);
    if (item.user_bucket) {
      notes.push(
        h("p", { class: "note note-you" }, icon("hand"), h("span", null,
          `You moved this from ${LABELS[item.bucket]}. `,
          editable ? h("button", { class: "link-btn", type: "button", onclick: () => setUserBucket(item.id, null, "Put back where it was.") }, "Put it back") : null))
      );
    }
    if (item.outcome === "fine") notes.push(h("p", { class: "note note-outcome-fine" }, icon("check"), h("span", null, "Checked in: nothing happened. Good call.")));
    if (item.outcome === "regret") notes.push(h("p", { class: "note note-outcome-regret" }, icon("alert"), h("span", null, "Checked in: this one bit you. It will count as a lesson next time.")));

    const actions = [];
    if (editable) {
      actions.push(
        h("select", {
          class: "move-select",
          "aria-label": `Move “${clip(item.text, 40)}” to another bucket`,
          onchange: (event) => {
            const target = event.target.value;
            event.target.value = "";
            if (target) moveTask(item, target);
          },
        },
          h("option", { value: "", selected: true, disabled: true }, "Move to…"),
          BUCKETS.filter((name) => name !== bucket).map((name) => h("option", { value: name }, LABELS[name])))
      );
    }

    return h("li", { class: "task", dataset: { id: item.id }, tabindex: "-1" },
      h("p", { class: "task-text" }, item.text),
      h("p", { class: "chips" }, chips),
      item.why ? h("p", { class: "task-why" }, h("strong", null, "Why: "), item.why) : null,
      item.if_ignored ? h("p", { class: "task-ignored" }, h("strong", null, "If ignored: "), item.if_ignored) : null,
      notes,
      SKIP.includes(bucket) && item.check_on && !item.outcome
        ? h("p", { class: "task-check" }, icon("bell"), `Check back ${fmtDay(item.check_on)}`)
        : null,
      actions.length ? h("div", { class: "task-actions" }, actions) : null
    );
  }

  function renderBucket(result, bucket, editable) {
    const byId = new Map(result.tasks.map((task) => [task.id, task]));
    const items = (result.layout[bucket] || []).map((id) => byId.get(id)).filter(Boolean);
    const minutes = items.reduce((sum, item) => sum + (item.minutes || 0), 0);
    // Tasks that cannot wait stay in Do now even when they are more than the energy set for today; say so plainly.
    let total = fmtMinutes(minutes);
    if (bucket === "now" && result.budget > 0) {
      total = minutes > result.budget
        ? `${fmtMinutes(minutes)}, over your ${fmtMinutes(result.budget)}`
        : `${fmtMinutes(minutes)} of ${fmtMinutes(result.budget)}`;
    }
    return h("section", { class: `bucket bucket-${bucket}`, "aria-labelledby": `bucket-${bucket}` },
      h("header", { class: "bucket-head" },
        h("img", { class: "bucket-badge", src: `img/${bucket}.svg`, alt: "", width: "56", height: "56" }),
        h("div", null,
          h("h3", { id: `bucket-${bucket}` }, LABELS[bucket], h("span", { class: "count" }, String(items.length))),
          h("p", { class: "bucket-desc" }, BLURBS[bucket])),
        items.length ? h("p", { class: "bucket-total" }, total) : null),
      items.length ? h("ul", { class: "tasks" }, items.map((item) => renderTask(item, editable))) : h("p", { class: "empty" }, EMPTY[bucket])
    );
  }

  function resultNotices(result) {
    const stats = result.stats;
    const notices = [];
    if (result.engine && result.engine.kind === "demo") {
      notices.push(h("div", { class: "notice notice-info" },
        h("p", null, h("strong", null, "Sorted with keyword rules, not AI. "), "No model was used for this list, so a handful of simple rules decided. Start a local model for real judgement (", h("code", null, "ollama pull gemma3:4b"), ").")));
    } else if (stats.fallback > 0) {
      notices.push(h("div", { class: "notice" },
        h("p", null, h("strong", null, `${plural(stats.fallback, "task")} sorted by keyword rules. `), "The model could not answer for them; they are marked below.")));
    }
    if (stats.rescued > 0) {
      notices.push(h("div", { class: "notice notice-ok" },
        h("p", null, h("strong", null, `The safety net kept ${plural(stats.rescued, "task")} the sort wanted to drop. `), "Money, paperwork, health, safety and promises never expire silently. Look for the shield.")));
    }
    if (result.warnings && result.warnings.length) {
      notices.push(h("details", { class: "notice" },
        h("summary", null, `Notes from the sort (${result.warnings.length})`),
        h("ul", null, result.warnings.map((warning) => h("li", null, warning)))));
    }
    return notices;
  }

  let rebudgetTimer = null;
  function budgetCard(result) {
    const output = h("output", { for: "list-budget" }, budgetLabel(result.budget));
    const slider = h("input", {
      id: "list-budget", type: "range", min: "0", max: String(Math.max(480, result.budget)), step: "15", value: String(result.budget),
      oninput: () => { output.textContent = budgetLabel(slider.value); },
      onchange: () => {
        clearTimeout(rebudgetTimer);
        rebudgetTimer = setTimeout(() => rebudget(Number(slider.value)), 150);
      },
    });
    slider.value = String(result.budget);
    return h("div", { class: "card budget-card" },
      h("label", { class: "energy-label", for: "list-budget" }, "Energy for today ", output),
      slider,
      h("p", { class: "hint" }, "Slide it to re-flow Do now and Do later instantly. The model is not asked again.")
    );
  }

  async function rebudget(budget) {
    const sid = state.current && state.current.sid;
    if (!sid) return;
    try {
      renderList(await api("POST", `/api/history/${encodeURIComponent(sid)}/budget`, { budget }), { keepFocus: "list-budget" });
    } catch (err) {
      toast(err.message);
    }
  }

  function renderList(result, { readOnly = false, focusTask = "", keepFocus = "" } = {}) {
    state.current = result;
    const editable = !readOnly && Boolean(result.sid);
    const stats = result.stats;
    const skipped = stats.skipped;
    const when = fmtStamp(result.created);

    const headline = [`${plural(stats.total, "task")} in. `, h("em", null, skipped ? `${skipped} you can put down.` : "Every one earns its place.")];
    const nowCount = stats.counts.now;
    const today = nowCount
      ? `${plural(nowCount, "task")} for today, about ${fmtMinutes(stats.minutes.now)}`
      : "nothing that has to happen today";
    const lede = skipped
      ? `Putting those down hands you back about ${fmtMinutes(stats.minutes_skipped)}. That leaves ${today}.`
      : `${today.charAt(0).toUpperCase()}${today.slice(1)}.`;

    const actions = [
      h("button", { class: "btn btn-ghost", type: "button", onclick: () => { saveDraft(""); els.tasks.value = ""; location.hash = "#/"; setTimeout(() => els.tasks.focus(), 50); } }, "Sort another list"),
      editable ? exportMenu(result) : h("button", { class: "btn", type: "button", onclick: () => copyText(plainText(result), "Copied the list as text.") }, icon("copy"), "Copy as text"),
      h("button", { class: "btn btn-ghost", type: "button", onclick: () => window.print() }, icon("print"), "Print"),
    ];

    fill(els.listRoot, 
      h("div", { class: "result-head" },
        h("div", null,
          h("p", { class: "eyebrow" }, when ? `Sorted ${when}` : "Sorted", h("span", { class: "no-caps" }, ` · ${engineLine(result.engine)}`)),
          h("h1", { tabindex: "-1" }, headline),
          h("p", { class: "lede" }, lede)),
        h("div", { class: "result-actions" }, actions)),
      readOnly || !result.sid
        ? h("div", { class: "notice notice-info readonly-note" }, h("p", null, h("strong", null, "This list was not saved. "), "Keeping lists on this computer is switched off in Settings, so moving tasks, exports and check-ins are unavailable for it."))
        : null,
      resultNotices(result),
      editable ? budgetCard(result) : null,
      h("div", { class: "columns" },
        h("div", { class: "col" }, h("h2", { class: "col-title" }, "On your plate"), KEEP.map((bucket) => renderBucket(result, bucket, editable))),
        h("div", { class: "col" }, h("h2", { class: "col-title" }, "Put down"), SKIP.map((bucket) => renderBucket(result, bucket, editable))))
    );

    if (focusTask) {
      const row = els.listRoot.querySelector(`.task[data-id="${focusTask}"]`);
      if (row) {
        row.classList.add("just-moved");
        row.focus({ preventScroll: true });
        row.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    } else if (keepFocus) {
      const target = document.getElementById(keepFocus);
      if (target) target.focus({ preventScroll: true });
    }
  }

  async function patchTask(tid, body) {
    const sid = state.current && state.current.sid;
    return api("PATCH", `/api/history/${encodeURIComponent(sid)}/tasks/${encodeURIComponent(tid)}`, body);
  }

  async function moveTask(item, bucket) {
    const before = item.user_bucket || null;
    try {
      const updated = await patchTask(item.id, { bucket });
      renderList(updated, { focusTask: item.id });
      toast(`Moved to ${LABELS[bucket]}.`, { action: "Undo", onAction: () => setUserBucket(item.id, before, "Move undone.") });
      refreshCheckinBadge();
    } catch (err) {
      toast(err.message);
    }
  }

  async function setUserBucket(tid, bucket, message) {
    try {
      renderList(await patchTask(tid, { bucket }), { focusTask: tid });
      toast(message);
      refreshCheckinBadge();
    } catch (err) {
      toast(err.message);
    }
  }

  function statCard(value, label) {
    return h("div", { class: "stat" }, h("span", { class: "stat-num" }, value), h("span", { class: "stat-label" }, label));
  }

  function checkinRow(row, mode) {
    const meta = [`Put down ${fmtDay(row.sorted_on)}`];
    if (mode === "due") meta.push(row.days_over > 0 ? `check-in was due ${fmtDay(row.check_on)}, ${plural(row.days_over, "day")} ago` : "check-in is due today");
    if (mode === "waiting") meta.push(`check back ${fmtDay(row.check_on)}`);
    const buttons = [];
    if (mode === "due") {
      buttons.push(h("button", { class: "btn btn-small", type: "button", onclick: () => answerCheckin(row, "fine") }, icon("check"), "Nothing happened"));
      buttons.push(h("button", { class: "btn btn-small btn-danger", type: "button", onclick: () => answerCheckin(row, "regret") }, icon("alert"), "It bit me"));
    } else if (mode === "resolved") {
      buttons.push(h("button", { class: "btn btn-small btn-ghost", type: "button", onclick: () => answerCheckin(row, null) }, icon("undo"), "Undo"));
    }
    buttons.push(h("a", { class: "btn btn-small btn-ghost", href: `#/list/${encodeURIComponent(row.sid)}` }, "Open list"));
    return h("li", { class: "row" },
      h("div", { class: "row-main" },
        h("p", { class: "row-title" },
          h("span", { class: `pill pill-${mode === "resolved" ? row.outcome : row.bucket}` }, mode === "resolved" ? (row.outcome === "fine" ? "Fine" : "It bit me") : LABELS[row.bucket]),
          row.text),
        h("p", { class: "row-meta" }, meta.join(" · ")),
        row.if_ignored ? h("p", { class: "row-body" }, h("strong", null, "If ignored: "), row.if_ignored) : null),
      h("div", { class: "row-actions" }, buttons)
    );
  }

  async function answerCheckin(row, outcome) {
    try {
      await api("PATCH", `/api/history/${encodeURIComponent(row.sid)}/tasks/${encodeURIComponent(row.tid)}`, { outcome });
      toast(outcome === "regret" ? "Noted. The model will see this as a lesson next time." : outcome === "fine" ? "Noted. Nothing happened. Lovely." : "Answer removed.");
      renderCheckins(await api("GET", "/api/checkins"));
      refreshCheckinBadge();
    } catch (err) {
      toast(err.message);
    }
  }

  function renderCheckins(data) {
    const stats = data.stats;
    const rate = stats.regret_rate === null ? "none yet" : `${Math.round(stats.regret_rate * 100)}%`;
    const nothing = !data.due.length && !data.waiting.length && !data.resolved.length;

    fill(els.checkinsRoot, 
      h("div", { class: "page-head" },
        h("p", { class: "eyebrow" }, "Check-ins"),
        h("h1", { tabindex: "-1" }, "Did letting go ", h("em", null, "cost you anything?")),
        h("p", { class: "lede" }, "Once the date has passed, each task you put down comes back here for one honest question. Your answers become examples the model sees next time.")),
      nothing
        ? h("div", { class: "empty-card" },
            h("img", { src: "img/sloth.svg", alt: "", width: "220", height: "210" }),
            h("h2", null, "Nothing to check yet."),
            h("p", null, "Sort a list and put a few things down. They will show up here when it is time to find out whether the world ended."),
            h("a", { class: "btn", href: "#/" }, "Sort a list"))
        : [
            h("div", { class: "stats" },
              statCard(String(stats.skipped), "tasks put down"),
              statCard(fmtMinutes(stats.minutes_skipped), "effort let go"),
              statCard(String(stats.resolved), "checked in so far"),
              statCard(String(stats.regrets), `bit you (${rate})`)),
            h("section", { class: "panel", "aria-labelledby": "due-title" },
              h("h2", { id: "due-title" }, `Due now (${data.due.length})`),
              data.due.length
                ? h("ul", { class: "rows" }, data.due.map((row) => checkinRow(row, "due")))
                : h("div", { class: "empty-card" }, h("p", null, "All caught up. Nothing is waiting for an answer."))),
            data.waiting.length
              ? h("section", { class: "panel", "aria-labelledby": "waiting-title" },
                  h("h2", { id: "waiting-title" }, `Coming up (${data.waiting.length})`),
                  h("ul", { class: "rows" }, data.waiting.map((row) => checkinRow(row, "waiting"))))
              : null,
            data.resolved.length
              ? h("section", { class: "panel", "aria-labelledby": "done-title" },
                  h("h2", { id: "done-title" }, `Checked (${stats.resolved})`),
                  h("ul", { class: "rows" }, data.resolved.map((row) => checkinRow(row, "resolved"))))
              : null,
          ]
    );
  }

  function renderHistory(items) {
    fill(els.historyRoot, 
      h("div", { class: "page-head" },
        h("p", { class: "eyebrow" }, "History"),
        h("h1", { tabindex: "-1" }, "Every list you ", h("em", null, "put down.")),
        h("p", { class: "lede" }, "Kept as plain files on this computer. Open one to move things around, export it, or answer its check-ins.")),
      items.length
        ? h("ul", { class: "rows" }, items.map((entry) => {
            const counts = entry.counts || {};
            const engine = entry.engine === "demo" ? "keyword rules" : entry.model || "local model";
            return h("li", { class: "row" },
              h("div", { class: "row-main" },
                h("p", { class: "row-title" }, fmtStamp(entry.created) || entry.sid),
                h("p", { class: "row-meta" },
                  `${plural(entry.total, "task")} · ${counts.now || 0} now · ${counts.later || 0} later · ${entry.skipped} put down (${fmtMinutes(entry.minutes_skipped)}) · ${engine}`),
                entry.preview && entry.preview.length ? h("p", { class: "row-body" }, entry.preview.join(" · ") + (entry.total > entry.preview.length ? " …" : "")) : null),
              h("div", { class: "row-actions" },
                h("a", { class: "btn btn-small", href: `#/list/${encodeURIComponent(entry.sid)}` }, "Open"),
                h("button", { class: "btn btn-small btn-ghost", type: "button", onclick: () => deleteList(entry) }, icon("trash"), "Delete")));
          }))
        : h("div", { class: "empty-card" },
            h("img", { src: "img/sloth.svg", alt: "", width: "220", height: "210" }),
            h("h2", null, "No lists yet."),
            h("p", null, "The first one you sort will be kept here, on this computer only."),
            h("a", { class: "btn", href: "#/" }, "Sort a list"))
    );
  }

  async function deleteList(entry) {
    if (!window.confirm(`Delete the list from ${fmtStamp(entry.created) || entry.sid}? This cannot be undone.`)) return;
    try {
      await api("DELETE", `/api/history/${encodeURIComponent(entry.sid)}`);
      toast("List deleted.");
      renderHistory((await api("GET", "/api/history")).items);
      refreshCheckinBadge();
    } catch (err) {
      toast(err.message);
    }
  }

  function field(label, control, hint, { wide = false, id = "" } = {}) {
    return h("div", { class: `field${wide ? " wide" : ""}` }, h("label", { for: id }, label), control, hint ? h("p", { class: "field-hint" }, hint) : null);
  }

  function checkField(id, label, hint, checked) {
    return h("div", { class: "check wide field" },
      h("input", { id, type: "checkbox", checked: Boolean(checked) }),
      h("div", null, h("label", { for: id }, label), h("span", { class: "field-hint" }, hint)));
  }

  function levelIcon(level) {
    return level === "ok" ? icon("check") : level === "warn" ? icon("alert") : icon("x");
  }

  function renderDoctor(outcome) {
    return h("div", { class: "doctor" },
      h("p", { class: `notice ${outcome.ok ? "notice-ok" : "notice-error"}` },
        h("strong", null, outcome.ok ? "Your model works. " : "Something needs fixing. "),
        outcome.ok ? "It sorted the sample list in the right shape." : "The steps below show where it stopped."),
      h("ul", { class: "doctor-steps" }, outcome.steps.map((step) =>
        h("li", { class: `level-${step.level}` }, levelIcon(step.level),
          h("div", null, h("strong", null, step.name), step.detail ? h("span", null, `: ${step.detail}`) : null, step.hint ? h("p", { class: "hint" }, step.hint) : null)))),
      outcome.raw ? h("div", null, h("p", { class: "hint" }, "What the model actually said:"), h("pre", null, outcome.raw)) : null
    );
  }

  function renderSettings(settings) {
    const status = state.status;
    const models = (status && status.backend && status.backend.models) || [];
    const dataList = h("datalist", { id: "model-list" }, models.map((model) => h("option", { value: model.name })));
    const input = (id, type, value, extra) => h("input", Object.assign({ id, type, value: String(value) }, extra || {}));
    const select = (id, value, options) => h("select", { id }, options.map(([val, label]) => h("option", { value: val, selected: val === value }, label)));

    const doctorSlot = h("div", { id: "doctor-slot", "aria-live": "polite" });
    const formError = h("div", { class: "notice notice-error", role: "alert", hidden: true });

    const form = h("form", { class: "settings-form", novalidate: true, onsubmit: async (event) => {
      event.preventDefault();
      formError.hidden = true;
      const val = (id) => document.getElementById(id);
      const patch = {
        provider: val("s-provider").value,
        base_url: val("s-base").value,
        model: val("s-model").value,
        allow_remote: val("s-remote").checked,
        demo_fallback: val("s-fallback").checked,
        budget_minutes: Number(val("s-budget").value),
        chunk_size: Number(val("s-chunk").value),
        timeout_s: Number(val("s-timeout").value),
        temperature: Number(val("s-temp").value),
        date_order: val("s-date").value,
        checkin_days: Number(val("s-checkin").value),
        never_skip: val("s-never").value,
        lessons: val("s-lessons").checked,
        autosave: val("s-autosave").checked,
      };
      if (val("s-key-clear").checked) patch.api_key = "";
      else if (val("s-key").value) patch.api_key = val("s-key").value;
      try {
        state.settings = await api("PUT", "/api/settings", patch);
        await refreshStatus(true);
        applySettingsToComposer();
        renderSettings(state.settings);
        toast("Settings saved.");
      } catch (err) {
        fill(formError, h("strong", null, "Not saved. "), err.message);
        formError.hidden = false;
        formError.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    } },
      formError,
      h("section", { class: "card", "aria-labelledby": "set-model-title" },
        h("h2", { class: "card-title", id: "set-model-title" }, "Model"),
        h("div", { class: "fields" },
          field("Server type", select("s-provider", settings.provider, [["auto", "Auto-detect"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible (llama.cpp, LM Studio, vLLM)"]]), "Auto-detect looks for Ollama, llama.cpp and LM Studio on this computer.", { id: "s-provider" }),
          field("Server address", input("s-base", "url", settings.base_url, { placeholder: "Leave empty to auto-detect", autocomplete: "off" }), "For example http://localhost:11434", { id: "s-base" }),
          field("Model", input("s-model", "text", settings.model, { placeholder: "Choose automatically", list: "model-list", autocomplete: "off" }),
            models.length ? `Installed: ${models.slice(0, 5).map((m) => m.name).join(", ")}${models.length > 5 ? " …" : ""}` : "Nothing detected yet. Start your server, then press Test connection.", { id: "s-model" }),
          field("API key", input("s-key", "password", "", { placeholder: settings.api_key_set ? "A key is saved. Leave blank to keep it." : "Only if your server asks for one", autocomplete: "off" }),
            null, { id: "s-key" }),
          h("div", { class: "check wide field" }, h("input", { id: "s-key-clear", type: "checkbox" }), h("div", null, h("label", { for: "s-key-clear" }, "Remove the saved key"), h("span", { class: "field-hint" }, "Tick this and save to forget the key."))),
          checkField("s-remote", "Allow a model server on another machine", "Off by default. When on, your task list is sent over your network to that server.", settings.allow_remote),
          checkField("s-fallback", "Finish with keyword rules if the model fails", "If the model stops answering halfway, the rest of the list is sorted by simple rules and labelled. Off means an error instead.", settings.demo_fallback)),
        dataList,
        h("div", { class: "form-actions" },
          h("button", { class: "btn", type: "button", id: "test-btn", onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "Testing…";
            fill(doctorSlot, h("p", { class: "hint" }, "Asking the model to sort three sample tasks. This can take a few seconds."));
            try {
              const outcome = await api("POST", "/api/test-model", {});
              fill(doctorSlot, renderDoctor(outcome));
              refreshStatus(true);
            } catch (err) {
              fill(doctorSlot, h("div", { class: "notice notice-error" }, err.message));
            } finally {
              button.disabled = false;
              button.textContent = "Test connection";
            }
          } }, "Test connection"),
          h("span", { class: "hint" }, "Uses the saved settings: save first if you changed anything.")),
        doctorSlot),

      h("section", { class: "card", "aria-labelledby": "set-sorting-title" },
        h("h2", { class: "card-title", id: "set-sorting-title" }, "Sorting"),
        h("div", { class: "fields" },
          field("Energy for today (minutes)", input("s-budget", "number", settings.budget_minutes, { min: "0", max: "960", step: "5" }), "Do now fills up to this. 0 means no limit.", { id: "s-budget" }),
          field("Check back after (days)", input("s-checkin", "number", settings.checkin_days, { min: "1", max: "60" }), "For tasks without a date of their own.", { id: "s-checkin" }),
          field("Tasks per batch", input("s-chunk", "number", settings.chunk_size, { min: "1", max: "20" }), "Smaller batches suit smaller models.", { id: "s-chunk" }),
          field("Model timeout (seconds)", input("s-timeout", "number", settings.timeout_s, { min: "10", max: "1800" }), "Slow laptops may need more.", { id: "s-timeout" }),
          field("Temperature", input("s-temp", "number", settings.temperature, { min: "0", max: "1", step: "0.05" }), "Low is steadier. 0.1 is a good default.", { id: "s-temp" }),
          field("Dates like 3/4 mean", select("s-date", settings.date_order, [["dmy", "3 April (day first)"], ["mdy", "March 4 (month first)"]]), null, { id: "s-date" }),
          field("Never let these words expire", h("textarea", { id: "s-never", rows: "3", placeholder: "mum\nlandlord\nvisa", spellcheck: "false" }, settings.never_skip.join("\n")),
            "One per line. Any task containing one is kept, whatever the model says.", { wide: true, id: "s-never" }),
          checkField("s-lessons", "Learn from my corrections", "Tasks you pulled back, and skipped ones that bit you, are shown to the model as examples.", settings.lessons),
          checkField("s-autosave", "Keep my lists on this computer", "Needed for history, check-ins, moving tasks and exports. Turn off to keep nothing.", settings.autosave))),

      h("div", { class: "form-actions" }, h("button", { class: "btn btn-large", type: "submit" }, "Save settings")),

      h("div", { class: "danger-zone" },
        h("h2", null, "Your data"),
        h("p", null, "Everything is stored in plain files in this folder:"),
        h("p", { class: "data-note" }, (status && status.data_dir) || "the data folder"),
        h("p", { class: "hint" }, "Nothing is sent anywhere except to your model server."),
        h("p", null, h("button", { class: "btn btn-small btn-danger", type: "button", onclick: forgetEverything }, icon("trash"), "Forget every saved list")))
    );

    fill(els.settingsRoot, 
      h("div", { class: "page-head" },
        h("p", { class: "eyebrow" }, "Settings"),
        h("h1", { tabindex: "-1" }, "Make it ", h("em", null, "yours.")),
        h("p", { class: "lede" }, "Most people never need to change anything here. If a model is running, the app finds it on its own.")),
      form
    );
  }

  async function forgetEverything() {
    if (!window.confirm("Delete every saved list and check-in? Your settings stay. This cannot be undone.")) return;
    try {
      const result = await api("POST", "/api/forget", { confirm: "forget" });
      toast(`Forgot ${plural(result.removed, "list")}.`);
      refreshCheckinBadge();
    } catch (err) {
      toast(err.message);
    }
  }

  function wireGlobal() {
    document.addEventListener("click", (event) => {
      for (const menu of document.querySelectorAll("details.menu[open]")) {
        if (!menu.contains(event.target)) menu.removeAttribute("open");
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      for (const menu of document.querySelectorAll("details.menu[open]")) menu.removeAttribute("open");
      hideToast();
    });
    window.addEventListener("focus", () => {
      if (Date.now() - state.statusAt > 10000) refreshStatus();
    });
    window.addEventListener("hashchange", route);
  }

  async function init() {
    cacheElements();
    wireComposer();
    wireGlobal();
    route();
    try {
      state.settings = await api("GET", "/api/settings");
      applySettingsToComposer();
    } catch (err) {
      /* the status banner explains it */
    }
    refreshStatus();
    refreshCheckinBadge();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
