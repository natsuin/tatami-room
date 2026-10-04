// The terminal panel: one xterm per session, kept while the app runs so each keeps its
// scrollback. The app says which one to show, and everything typed goes to that session's
// terminal inside WSL (tatami term).
"use strict";
const T = window.term;
const main = document.getElementById("main");
const terms = new Map();  // session key -> { term, fit, el, pending }
let current = null;

function make(key) {
  const el = document.createElement("div");
  el.className = "term";
  main.append(el);
  const term = new Terminal({
    allowTransparency: true, cursorBlink: true, scrollback: 5000,
    fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, monospace', fontSize: 14,
    theme: { background: "rgba(0, 0, 0, 0)", foreground: "#f5e6f0", cursor: "#ff85c0",
             selectionBackground: "rgba(195, 166, 255, .35)" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  const t = { term, fit, el, pending: [] };
  terms.set(key, t);
  term.onData((d) => T.input(key, d));
  term.onBinary((d) => T.binary(key, d));
  term.onResize(({ cols, rows }) => T.resize(key, cols, rows));
  // Ctrl+V pastes; Ctrl+C copies when there's a selection (Ctrl+Shift+C always), else it's Ctrl+C.
  // Returning false only keeps xterm from typing the key: the key is cancelled too, or Chromium
  // pastes (or copies) on its own as well, and xterm sends that paste again.
  // With no text on the clipboard (a screenshot), the agent gets the Ctrl+V key itself and reads
  // the image from the clipboard on its own.
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !e.ctrlKey) return true;
    const k = e.key.toLowerCase();
    if (k === "v") {
      e.preventDefault();
      T.paste().then((text) => { if (text) term.paste(text); else T.input(key, "\x16"); });
      return false;
    }
    if (k === "c" && (e.shiftKey || term.hasSelection())) {
      e.preventDefault();
      T.copy(term.getSelection());
      term.clearSelection();
      return false;
    }
    return true;
  });
  // What it printed before the panel drew it, then whatever came in meanwhile.
  T.backlog(key).then((data) => {
    if (data) term.write(data);
    for (const chunk of t.pending) term.write(chunk);
    t.pending = null;
  });
  return t;
}

// The window tints are very dark; the same hue, light, marks the session in the header.
function lightHue(hex) {
  const n = parseInt(String(hex).slice(1), 16);
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return "#a898b4";
  const r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
  if (!d) return "#a898b4";
  const h = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return `hsl(${Math.round(h * 60)} 75% 78%)`;
}

function apply(look) {
  document.getElementById("name").textContent = look.name;
  document.title = look.name;
  const tint = /^#[0-9a-f]{6}$/i.test(look.tint) ? look.tint : "#2a2233";
  document.documentElement.style.setProperty("--tint", tint);
  document.documentElement.style.setProperty("--hue", lightHue(tint));
  const girl = document.getElementById("girl");
  if (look.girl && girl.dataset.src !== look.girl) {
    girl.dataset.src = look.girl;
    girl.onload = () => { girl.hidden = false; fade(); };
    girl.onerror = () => { girl.hidden = true; };
    girl.src = look.girl;
  } else if (!look.girl) {
    girl.hidden = true;
  }
}

function fitCurrent() {
  const t = terms.get(current);
  if (!t) return;
  t.fit.fit();
  T.resize(current, t.term.cols, t.term.rows);
  fade();
}

// The dot picture is stretched to cover the panel, so in a big panel every dot is drawn bigger
// beside the same-size letters; they fade in step with their size, like in Claude Waifu windows
// (waifu's FADE_FROM and FADE_FLOOR; it draws the dots in 12 x 24 px cells).
function fade() {
  const t = terms.get(current), girl = document.getElementById("girl");
  if (!t || !girl.naturalWidth) return;
  const scale = Math.max(t.term.cols * 12 / girl.naturalWidth, t.term.rows * 24 / girl.naturalHeight);
  girl.style.setProperty("--fade", Math.max(.4, Math.min(1, .8 / scale)));
}

T.onShow((key, look) => {
  for (const k of [...ended]) if (k !== key) forget(k);
  const t = terms.get(key) || make(key);
  for (const [k, o] of terms) o.el.hidden = k !== key;
  current = key;
  apply(look);
  requestAnimationFrame(() => { fitCurrent(); t.term.focus(); });
});
T.onLook((key, look) => { if (key === current) apply(look); });
T.onData((key, chunk) => {
  const t = terms.get(key);
  if (!t) return;  // not drawn yet: its backlog has this
  if (t.pending) t.pending.push(chunk); else t.term.write(chunk);
});
// An ended terminal says so while it's on show, and is let go once it isn't.
const ended = new Set();
function forget(key) {
  const t = terms.get(key);
  if (!t) return;
  t.term.dispose();
  t.el.remove();
  terms.delete(key);
  ended.delete(key);
}
T.onEnd((key) => {
  const t = terms.get(key);
  if (!t) return;
  if (key !== current) return forget(key);
  ended.add(key);
  t.term.write("\r\n\x1b[2m[This terminal has ended.]\x1b[0m\r\n");
});
document.getElementById("hide").addEventListener("click", () => T.hide());
document.getElementById("end").addEventListener("click", () => { if (current) T.end(current); });

// The grip on the left edge: the app moves the panel's edge as it's dragged.
const grip = document.getElementById("grip");
let drag = null;
grip.addEventListener("pointerdown", (e) => {
  drag = { x: e.screenX, width: window.innerWidth, next: null };
  grip.setPointerCapture(e.pointerId);
  grip.classList.add("held");
});
grip.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const first = drag.next === null;
  drag.next = drag.width + (drag.x - e.screenX);
  if (first) requestAnimationFrame(() => { if (drag) { T.width(drag.next); drag.next = null; } });
});
const letGo = () => { drag = null; grip.classList.remove("held"); };
grip.addEventListener("pointerup", letGo);
grip.addEventListener("pointercancel", letGo);
new ResizeObserver(fitCurrent).observe(main);
