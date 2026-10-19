// Caption list: one editable row per card. Split, join and delete rebuild the list;
// plain text edits only mark the row so the cursor never jumps.
import { fmtTime } from "./api.js";

function makeButton(className, label, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `mini ${className}`.trim();
  b.textContent = label;
  b.title = title;
  b.setAttribute("aria-label", title);
  return b;
}

export class CardEditor {
  constructor(container, { onTextEdit, onStructure, onSeek, onMessage }) {
    this.container = container;
    this.onTextEdit = onTextEdit;
    this.onStructure = onStructure;
    this.onSeek = onSeek;
    this.onMessage = onMessage;
    this.cards = [];
    this.rows = [];
    this.activeId = null;
    this.rev = 0;
  }

  setCards(cards) {
    this.cards = cards.map((c) => ({
      id: c.id, start: c.start, end: c.end, words: c.words,
      text: c.words.map((w) => w.w).join(" "),
    }));
    this.render();
  }

  /** Take layout/timing from the server without touching what the user is typing. */
  mergeServer(cards) {
    if (cards.length !== this.cards.length) return this.setCards(cards);
    cards.forEach((c, i) => {
      const mine = this.cards[i];
      mine.id = c.id; mine.start = c.start; mine.end = c.end; mine.words = c.words;
      const label = this.rows[i] && this.rows[i].querySelector(".time");
      if (label) label.textContent = fmtTime(c.start);
    });
  }

  payload() {
    return this.cards.map((c) => ({ start: c.start, end: c.end, text: c.text, words: c.words }));
  }

  render() {
    this.container.replaceChildren();
    this.rows = this.cards.map((card, i) => this.buildRow(card, i));
    this.container.append(...this.rows);
    this.applyActive();
  }

  buildRow(card, index) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.index = String(index);

    const time = document.createElement("button");
    time.type = "button";
    time.className = "time";
    time.textContent = fmtTime(card.start);
    time.title = "Jump to this caption";
    time.addEventListener("click", () => this.onSeek(this.cards[index].start));

    const input = document.createElement("input");
    input.type = "text";
    input.className = "txt";
    input.value = card.text;
    input.spellcheck = false;
    input.setAttribute("aria-label", `Caption ${index + 1}`);
    input.addEventListener("input", () => {
      this.cards[index].text = input.value;
      this.rev += 1;
      this.onTextEdit();
    });

    const actions = document.createElement("div");
    actions.className = "row-actions";
    const split = makeButton("", "Split", "Split here: click between two words in the text first");
    split.addEventListener("click", () => this.split(index, input));
    const join = makeButton("", "Join", "Join with the next caption");
    join.addEventListener("click", () => this.join(index));
    const del = makeButton("danger", "\u00d7", "Delete this caption");
    del.addEventListener("click", () => this.remove(index));
    actions.append(split, join, del);

    row.append(time, input, actions);
    return row;
  }

  split(index, input) {
    const card = this.cards[index];
    const tokens = card.text.split(/\s+/).filter(Boolean);
    const caret = input.selectionStart ?? input.value.length;
    const k = input.value.slice(0, caret).split(/\s+/).filter(Boolean).length;
    if (tokens.length < 2 || k < 1 || k >= tokens.length) {
      this.onMessage("Put the cursor between two words, then press split.");
      return;
    }
    const aligned = tokens.length === card.words.length;
    let a;
    let b;
    if (aligned) {
      const cut = card.words[k].s;
      a = { start: card.start, end: Math.max(card.start + 0.05, cut), words: card.words.slice(0, k) };
      b = { start: Math.max(card.start + 0.05, cut), end: card.end, words: card.words.slice(k) };
    } else {
      const lenA = tokens.slice(0, k).join(" ").length;
      const share = lenA / tokens.join(" ").length;
      const mid = card.start + (card.end - card.start) * share;
      a = { start: card.start, end: mid, words: [] };
      b = { start: mid, end: card.end, words: [] };
    }
    a.text = tokens.slice(0, k).join(" ");
    b.text = tokens.slice(k).join(" ");
    this.cards.splice(index, 1, a, b);
    this.rev += 1;
    this.onStructure();
  }

  join(index) {
    if (index >= this.cards.length - 1) {
      this.onMessage("This is the last caption, so there is nothing to join it with.");
      return;
    }
    const a = this.cards[index];
    const b = this.cards[index + 1];
    const merged = {
      start: a.start, end: b.end, words: [...a.words, ...b.words],
      text: `${a.text.trim()} ${b.text.trim()}`.trim(),
    };
    this.cards.splice(index, 2, merged);
    this.rev += 1;
    this.onStructure();
  }

  remove(index) {
    this.cards.splice(index, 1);
    this.rev += 1;
    this.onStructure();
  }

  setActive(id) {
    if (id === this.activeId) return;
    this.activeId = id;
    this.applyActive();
  }

  applyActive() {
    this.rows.forEach((row, i) => {
      const on = this.cards[i] && this.cards[i].id === this.activeId;
      row.classList.toggle("active", on);
      if (on && !this.container.contains(document.activeElement)) {
        const box = this.container.closest(".tabpanel");
        if (!box || box.hidden) return;
        const r = row.getBoundingClientRect();
        const b = box.getBoundingClientRect();
        if (r.top < b.top + 8 || r.bottom > b.bottom - 8) {
          box.scrollBy({ top: r.top - b.top - b.height / 3, behavior: "smooth" });
        }
      }
    });
  }
}
