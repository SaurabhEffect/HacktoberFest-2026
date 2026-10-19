// Style tab: presets, sliders, segmented controls, colours and a saved "brand look".
import { debounce } from "./api.js";

// Fields a preset changes. Rhythm (words per card, lines), punctuation and position stay yours.
const LOOK = ["weight", "font_size", "case", "highlight", "pop", "text_color", "highlight_color",
  "outline_color", "outline", "shadow", "box"];
const BRAND_KEY = "rc.brand";

function readBrand() {
  try { return JSON.parse(localStorage.getItem(BRAND_KEY) || "null"); } catch { return null; }
}

export class StylePanel {
  constructor({ presets, onChange }) {
    this.presets = presets;
    this.onChange = onChange;
    this.style = null;
    this.rev = 0;
    this.el = (id) => document.getElementById(id);
    this.emit = debounce((regroup) => this.onChange({ ...this.style }, { regroup }), 160);
    this.buildPresets();
    this.bind();
  }

  static brand() { return readBrand(); }

  buildPresets() {
    const host = this.el("presets");
    host.replaceChildren();
    const items = [...this.presets];
    const brand = readBrand();
    if (brand) items.push({ ...brand, preset: "brand", label: "My brand" });
    for (const p of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "preset";
      b.dataset.preset = p.preset;
      b.style.setProperty("--oc", p.outline_color);
      b.style.setProperty("--hc", p.highlight ? p.highlight_color : p.text_color);
      b.style.background = p.box ? "#0e1b18" : "#1c2f2b";
      b.innerHTML = `<b><span>Aa</span></b>${p.label}`;
      b.firstChild.style.color = p.text_color;
      b.addEventListener("click", () => this.applyPreset(p));
      host.append(b);
    }
    this.markPreset();
  }

  applyPreset(p) {
    const next = { ...this.style, preset: p.preset };
    for (const key of LOOK) next[key] = p[key];
    this.rev += 1;
    this.set(next);
    this.emit(false);
  }

  markPreset() {
    this.el("presets").querySelectorAll(".preset").forEach((b) =>
      b.setAttribute("aria-pressed", String(this.style && b.dataset.preset === this.style.preset)));
  }

  /** Reflect a style object coming from the server in all controls. */
  set(style) {
    this.style = { ...style };
    const s = this.style;
    this.range("fontSize", s.font_size, (v) => `${v.toFixed(1)}%`);
    this.range("maxWords", s.max_words, (v) => String(v));
    this.range("outline", s.outline, (v) => v.toFixed(2));
    for (const [id, key] of [["segPosition", "position"], ["segCase", "case"], ["segPunct", "punct"], ["segLines", "lines"]]) {
      this.el(id).querySelectorAll("button").forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.val === String(s[key]))));
    }
    this.el("tgHighlight").checked = !!s.highlight;
    this.el("tgPop").checked = !!s.pop;
    this.el("tgBox").checked = !!s.box;
    this.el("cText").value = s.text_color.toLowerCase();
    this.el("cHighlight").value = s.highlight_color.toLowerCase();
    this.el("cOutline").value = s.outline_color.toLowerCase();
    this.markPreset();
  }

  range(id, value, fmt) {
    const input = this.el(id);
    input.value = String(value);
    this.el(`${id}Out`).textContent = fmt(Number(value));
  }

  touch(patch, regroup = false) {
    this.rev += 1;
    this.style = { ...this.style, ...patch, preset: "custom" };
    this.set(this.style);
    this.emit(regroup);
  }

  bind() {
    const rangeBind = (id, key, fmt, { regroup = false, integer = false } = {}) => {
      const input = this.el(id);
      input.addEventListener("input", () => {
        const v = integer ? parseInt(input.value, 10) : parseFloat(input.value);
        this.el(`${id}Out`).textContent = fmt(v);
        this.rev += 1;
        this.style = { ...this.style, [key]: v, preset: "custom" };
        this.markPreset();
        if (!regroup) this.emit(false);
      });
      if (regroup) input.addEventListener("change", () => this.emit(true));
    };
    rangeBind("fontSize", "font_size", (v) => `${v.toFixed(1)}%`);
    rangeBind("outline", "outline", (v) => v.toFixed(2));
    rangeBind("maxWords", "max_words", (v) => String(v), { regroup: true, integer: true });

    const segBind = (id, key, { regroup = false, number = false } = {}) => {
      this.el(id).addEventListener("click", (event) => {
        const btn = event.target.closest("button");
        if (!btn) return;
        const raw = btn.dataset.val;
        this.rev += 1;
        // Position and punctuation are layout choices, they do not turn a preset into "custom".
        const keepPreset = key === "position" || key === "punct" || key === "lines";
        this.style = { ...this.style, [key]: number ? Number(raw) : raw, ...(keepPreset ? {} : { preset: "custom" }) };
        this.set(this.style);
        this.emit(regroup);
      });
    };
    segBind("segPosition", "position");
    segBind("segCase", "case");
    segBind("segPunct", "punct");
    segBind("segLines", "lines", { regroup: true, number: true });

    this.el("tgHighlight").addEventListener("change", (e) => this.touch({ highlight: e.target.checked }));
    this.el("tgPop").addEventListener("change", (e) => this.touch({ pop: e.target.checked }));
    this.el("tgBox").addEventListener("change", (e) => this.touch({ box: e.target.checked }));
    this.el("cText").addEventListener("input", (e) => this.touch({ text_color: e.target.value.toUpperCase() }));
    this.el("cHighlight").addEventListener("input", (e) => this.touch({ highlight_color: e.target.value.toUpperCase() }));
    this.el("cOutline").addEventListener("input", (e) => this.touch({ outline_color: e.target.value.toUpperCase() }));

    this.el("saveBrand").addEventListener("click", () => {
      const look = {};
      for (const key of LOOK) look[key] = this.style[key];
      try {
        localStorage.setItem(BRAND_KEY, JSON.stringify(look));
        this.el("brandNote").textContent = "Saved. It now sits next to the presets and is used for new videos.";
      } catch {
        this.el("brandNote").textContent = "This browser would not let me save it.";
      }
      this.style = { ...this.style, preset: "brand" };
      this.buildPresets();
    });
  }
}
