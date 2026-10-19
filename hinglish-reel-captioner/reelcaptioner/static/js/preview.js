// Live caption overlay. Uses the line breaks, scale and display text computed by the server,
// so what you see here is what FFmpeg burns in.

/**
 * Outline as rings of hard text-shadows. That gives round joins, like the libass render, where
 * -webkit-text-stroke draws miter spikes on A, M, N and friends. The inner ring closes pin-holes
 * on thin strokes. The sample count grows with the radius so the edge stays smooth at any size.
 */
export function outlineShadow(radius, color) {
  if (radius < 0.3) return "none";
  const parts = [];
  const ring = (r, n) => {
    for (let i = 0; i < n; i += 1) {
      const a = (i / n) * Math.PI * 2;
      parts.push(`${(Math.cos(a) * r).toFixed(2)}px ${(Math.sin(a) * r).toFixed(2)}px 0 ${color}`);
    }
  };
  const n = Math.min(40, Math.max(12, Math.ceil((Math.PI * 2 * radius) / 1.1)));
  ring(radius, n);
  ring(radius * 0.5, Math.max(8, Math.round(n / 2)));
  return parts.join(",");
}

export class CaptionOverlay {
  constructor(frame, video, layer) {
    this.frame = frame;
    this.video = video;
    this.layer = layer;
    this.cards = [];
    this.style = null;
    this.position = { an: 2, margin: 0.24 };
    this.key = "";
    this.lastCardId = null;
    this.raf = 0;
    const tick = () => this.tick();
    video.addEventListener("timeupdate", tick);
    video.addEventListener("seeked", tick);
    video.addEventListener("loadeddata", tick);
    video.addEventListener("play", () => this.loop());
    video.addEventListener("pause", () => cancelAnimationFrame(this.raf));
    video.addEventListener("ended", () => cancelAnimationFrame(this.raf));
  }

  setData({ cards, style, position }) {
    this.cards = cards || [];
    this.style = style;
    this.position = position || this.position;
    this.key = "";
    this.lastCardId = null;
    this.applyStyle();
    this.tick();
  }

  resize() {
    this.key = "";
    this.tick();
  }

  applyStyle() {
    const s = this.style;
    if (!s) return;
    const layer = this.layer;
    layer.className = `cap an${this.position.an}${s.box ? " box" : ""}`;
    layer.style.setProperty("--margin", String(this.position.margin));
    layer.style.setProperty("--weight", s.weight === "medium" ? "500" : "700");
    layer.style.setProperty("--text", s.text_color);
    layer.style.setProperty("--hl", s.highlight ? s.highlight_color : s.text_color);
    layer.style.setProperty("--outline-color", s.outline_color);
    layer.style.setProperty("--box", s.outline_color);
  }

  loop() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      this.tick();
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  findCard(t) {
    const cards = this.cards;
    let lo = 0;
    let hi = cards.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cards[mid].start <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (found >= 0 && t < cards[found].end) return cards[found];
    return null;
  }

  tick() {
    if (!this.style) return;
    const t = this.video.currentTime || 0;
    const card = this.findCard(t);
    if (!card) {
      if (this.key !== "none") { this.layer.replaceChildren(); this.key = "none"; this.lastCardId = null; }
      return;
    }
    const order = card.lines.flat();
    let active = -1;
    if (this.style.highlight && order.length) {
      active = order[0];
      for (const idx of order) if (card.words[idx].s <= t + 0.001) active = idx;
    }
    const key = `${card.id}|${active}`;
    if (key === this.key) return;
    this.key = key;
    this.render(card, active);
  }

  render(card, active) {
    const s = this.style;
    const height = this.frame.clientHeight || 1;
    const em = (s.font_size / 100) * height * card.scale;
    const inner = document.createElement("div");
    inner.className = "inner";
    if (s.pop && this.lastCardId !== card.id) inner.classList.add("pop-in");
    this.lastCardId = card.id;
    for (const line of card.lines) {
      const row = document.createElement("div");
      row.className = "cap-line";
      const outline = s.outline * em;
      const drop = s.shadow * em;
      row.style.setProperty("--fs", `${em.toFixed(2)}px`);
      row.style.setProperty("--o", `${outline.toFixed(2)}px`);
      // The solid-box look has neither outline nor shadow; the box itself carries the colour.
      row.style.textShadow = s.box ? "none" : outlineShadow(outline, s.outline_color);
      row.style.filter = !s.box && drop >= 0.3
        ? `drop-shadow(${drop.toFixed(2)}px ${drop.toFixed(2)}px 0 rgba(0,0,0,.5))` : "none";
      const holder = s.box ? Object.assign(document.createElement("span"), { className: "bg" }) : row;
      let first = true;
      for (const idx of line) {
        const text = card.d[idx];
        if (!text) continue;
        if (!first) holder.append(" ");
        first = false;
        const span = document.createElement("span");
        span.textContent = text;
        if (idx === active) span.className = "on";
        holder.append(span);
      }
      if (holder !== row) row.append(holder);
      if (!first) inner.append(row);
    }
    this.layer.replaceChildren(inner);
  }
}
