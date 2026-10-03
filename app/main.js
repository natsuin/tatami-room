// Tatami Room as a Windows app. It starts the board inside WSL (the Python server that knows
// your agents), shows the desk in a window of its own, and does what a web page can't: open a
// new Claude window, bring an agent's window up, keep WSL running while it's open, sit in the
// tray, and tell you when an agent needs your OK.
"use strict";
const { app, BrowserWindow, Menu, Notification, Tray, WebContentsView, clipboard, dialog, ipcMain, nativeTheme, screen,
  shell } = require("electron");
const { execFile, spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

// Written by the installer (waifu desk): {distro, tatami: the tatami command's Linux path,
// launcher: the Windows folder with the Claude Waifu launcher and the board's address}.
const CONFIG = JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8"));
const APP_ID = "TatamiRoom.Desk";  // Windows groups the app's windows, shortcuts and notifications by this
const ICON = path.join(__dirname, "tatami.ico");
const AGENT_ID = /^[A-Za-z0-9._-]{1,60}$/;
const BOUNDS = path.join(app.getPath("userData"), "window.json");

let win = null;
let tray = null;
let hold = null;
let board = null;  // the board's address, with its token
let quitting = false;
let restarting = false;  // a restart leaves the app's terminals running, to attach to again

// ---- WSL ----

// Run the tatami command inside WSL, hidden. Resolves with its exit code.
function tatami(args, timeout = 60000) {
  return new Promise((resolve) => {
    execFile("wsl.exe", ["-d", CONFIG.distro, "--", CONFIG.tatami, ...args], { windowsHide: true, timeout },
      (err) => resolve(!err ? 0 : typeof err.code === "number" ? err.code : -1));
  });
}

// The board leaves its address next to the launchers when it starts.
function readBoard() {
  try {
    const url = fs.readFileSync(path.join(CONFIG.launcher, "board.url"), "utf8").trim();
    return /^http:\/\/127\.0\.0\.1:\d+\/\?token=[0-9a-f]+$/.test(url) ? new URL(url) : null;
  } catch {
    return null;
  }
}

// WSL stops a distro soon after its last terminal closes, and the board with it. `tatami hold`
// holds it up while the desk keeps checking in, which this app does for as long as it runs.
let heldAt = 0;
function holdWsl() {
  // Only one hold runs at a time (a second one ends at once), so check back now and then.
  if ((hold && hold.exitCode === null) || Date.now() - heldAt < 60000) return;
  heldAt = Date.now();
  hold = spawn("wsl.exe", ["-d", CONFIG.distro, "--", CONFIG.tatami, "hold"], { windowsHide: true, stdio: "ignore" });
  hold.on("error", () => {});
}

// ---- what the desk page may ask for ----

// + Claude: Claude in a terminal of the app's own, in the panel beside the desk (see below). A
// Claude Waifu window in Windows Terminal is still in the tray menu.
function newClaude() {
  startSession();
}

function newWindow() {
  execFile("wscript.exe", [path.join(CONFIG.launcher, "launch.vbs")], { windowsHide: true }, () => {});
}

// Bringing windows up: helper.ps1, started once and kept running, does it by the window's handle,
// which each agent finds for itself when it starts. Asking takes milliseconds, not a trip to WSL.
let helper = null;
let asked = 0;
const answers = new Map();
function startHelper() {
  helper = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-NoLogo", "-ExecutionPolicy", "Bypass",
    "-File", path.join(__dirname, "helper.ps1")], { windowsHide: true, stdio: ["pipe", "pipe", "ignore"] });
  let buf = "";
  helper.stdout.setEncoding("utf8");
  helper.stdout.on("data", (chunk) => {
    buf += chunk;
    for (let i; (i = buf.indexOf("\n")) >= 0; buf = buf.slice(i + 1)) {
      const [id, rc] = buf.slice(0, i).trim().split(" ");
      answers.get(id)?.(Number(rc));
      answers.delete(id);
    }
  });
  helper.on("error", () => {});
  helper.on("exit", () => {
    helper = null;
    for (const done of answers.values()) done(2);
    answers.clear();
  });
}

function bringUp(hwnd) {
  return new Promise((resolve) => {
    if (!helper) startHelper();
    const id = String(++asked);
    const late = setTimeout(() => { answers.delete(id); resolve(2); }, 3000);
    answers.set(id, (rc) => { clearTimeout(late); resolve(rc); });
    helper.stdin.write(`${id} ${hwnd}\n`);
  });
}

// 0: its window is in front. 1: shown, but Windows kept the focus elsewhere. 2: no window.
const windows = new Map();  // agent id -> its window's handle, from the board
async function focusAgent(id) {
  if (!AGENT_ID.test(id)) return 2;
  for (const [key, a] of bySession) {  // one of the app's own terminals: show it in the panel
    if (a.id === id && sessions.has(key)) {
      showSession(key);
      return 0;
    }
  }
  const hwnd = windows.get(id);
  if (hwnd) {
    const rc = await bringUp(hwnd);
    if (rc !== 2) return rc;
  }
  return tatami(["window", id], 30000);  // no handle yet, or the tab moved: the slow way, through WSL
}

function fromBoard(event) {
  try {
    return board && new URL(event.senderFrame.url).origin === board.origin;
  } catch {
    return false;
  }
}

ipcMain.handle("tatami:new-claude", (event) => { if (fromBoard(event)) newClaude(); });
// + Gemini and the others: any kind of agent in kinds.json; `tatami term` checks it's one it knows.
ipcMain.handle("tatami:new-agent", (event, kind) => {
  if (fromBoard(event) && /^[a-z0-9-]{1,30}$/.test(String(kind))) startSession(String(kind));
});
ipcMain.handle("tatami:focus", (event, id) => (fromBoard(event) ? focusAgent(String(id)) : 2));

// ---- terminals inside the app ----
// Each runs `tatami term` through a wsl.exe of its own: an agent (Claude, Gemini...) in a pseudo-terminal inside WSL,
// relayed over plain pipes and drawn with xterm.js in a panel beside the desk (terminal.html). The agent belongs to a
// holder inside WSL, not to the app: when the app goes away the agent carries on, and the app attaches again when it
// starts, getting the screen back from the holder. Only End (or quitting) stops it.

const sessions = new Map();   // session key -> { proc, log }
const bySession = new Map();  // session key -> its agent on the desk, once it has checked in
const LOG_MAX = 4 << 20;      // what a terminal printed before the panel drew it, kept for it
const HEAD = 64;              // the desk's header stays whole above the panel
let panel = null;
let panelReady = false;
let queued = [];
let shown = null;             // the session in the panel, while it's open
let panelWidth = null;        // set by dragging its edge; until then, a share of the window
let lastShown = null;         // for Ctrl+`, which goes back to it
let tucked = false;           // put away under a picture of it on the desk, while a menu from the bar is open

const END = "\x1b]7373;end\x07";  // what tells a terminal's holder to stop its agent
const KEEP = "\x1b]7373;keep\x07";  // and that it should keep it while the app is away

function startSession(kind = "claude", key = null, show = true) {
  key = key || "app-" + Date.now().toString(36);
  const proc = spawn("wsl.exe", ["-d", CONFIG.distro, "--", CONFIG.tatami, "term", key, kind],
    { windowsHide: true, stdio: ["pipe", "pipe", "ignore"] });
  const s = { proc, log: [], size: 0, kind };
  sessions.set(key, s);
  proc.stdin.write(KEEP);  // this app attaches again after a restart: the agent may carry on while it's away
  proc.stdout.on("data", (chunk) => {
    s.log.push(chunk);
    s.size += chunk.length;
    while (s.size > LOG_MAX && s.log.length > 1) s.size -= s.log.shift().length;
    toPanel("term:data", key, chunk);
  });
  proc.on("error", () => {});
  proc.on("exit", () => {
    sessions.delete(key);
    toPanel("term:end", key);
  });
  if (show) showSession(key);
}

// The terminals still running from before the app last went away (a restart, an update, a crash).
function reattach() {
  execFile("wsl.exe", ["-d", CONFIG.distro, "--", CONFIG.tatami, "term", "--list"], { windowsHide: true, timeout: 30000 },
    (err, stdout) => {
      if (err) return;
      for (const line of String(stdout).split("\n")) {
        try {
          const { session, kind } = JSON.parse(line);
          if (/^[a-z0-9-]{1,40}$/.test(session) && /^[a-z0-9-]{1,30}$/.test(kind) && !sessions.has(session)) {
            startSession(kind, session, false);
          }
        } catch { /* not a line of ours */ }
      }
    });
}

// Ending a terminal stops its agent: its holder hangs up, and the terminal's wsl.exe exits.
function endSession(s) {
  return new Promise((resolve) => {
    if (s.proc.exitCode !== null) return resolve();
    const late = setTimeout(() => { s.proc.kill(); resolve(); }, 3000);
    s.proc.once("exit", () => { clearTimeout(late); resolve(); });
    try { s.proc.stdin.write(END); } catch { s.proc.kill(); }
  });
}

function toPanel(channel, ...args) {
  if (panelReady) panel.webContents.send(channel, ...args);
  else if (channel !== "term:data") queued.push([channel, ...args]);  // data waits in the log
}

function ensurePanel() {
  if (panel) return;
  panel = new WebContentsView({ webPreferences: {
    preload: path.join(__dirname, "terminal-preload.js"), contextIsolation: true, sandbox: true,
    nodeIntegration: false, spellcheck: false, backgroundThrottling: false } });
  panel.setBackgroundColor("#110c17");
  panel.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  panel.webContents.on("will-navigate", (e) => e.preventDefault());
  panel.webContents.once("did-finish-load", () => {
    panelReady = true;
    for (const [channel, ...args] of queued) panel.webContents.send(channel, ...args);
    queued = [];
  });
  panel.webContents.on("before-input-event", toggleKey);
  panel.webContents.loadFile(path.join(__dirname, "terminal.html"));
  win.contentView.addChildView(panel);
  win.on("resize", () => layout());
}

// The panel takes the right side under the desk's header; the desk makes room for it. When it
// opens, it slides in from the edge on a spring that settles without overshooting.
let sliding = null;
function layout(slide = false) {
  if (!panel || win.isDestroyed()) return;
  const [w, h] = win.getContentSize();
  const wanted = panelWidth || Math.max(520, Math.round(w * 0.58));
  const width = shown ? Math.max(0, Math.min(w - 300, Math.max(360, wanted))) : 0;
  const place = (x) => panel.setBounds({ x, y: HEAD, width, height: Math.max(0, h - HEAD) });
  clearInterval(sliding);
  panel.setVisible(!!shown && !tucked);
  win.webContents.send("tatami:panel", width, tucked);
  if (!slide || !width) return place(w - width);
  const start = Date.now();
  place(w);
  sliding = setInterval(() => {
    const t = (Date.now() - start) / 1000, k = 17;   // critically damped: about 0.4 s to settle
    const done = t >= 0.42;
    place(Math.round(w - width * (done ? 1 : 1 - Math.exp(-k * t) * (1 + k * t))));
    if (done) clearInterval(sliding);
  }, 10);
}

function lookOf(key) {
  const a = bySession.get(key);
  const kind = sessions.get(key)?.kind || "claude";   // until its agent checks in: "Gemini"
  return { name: a ? a.id : kind.charAt(0).toUpperCase() + kind.slice(1), tint: (a && a.tint) || "#2a2233",
    girl: a && a.girl ? `${board.origin}/girl/${encodeURIComponent(a.id)}?token=${board.searchParams.get("token")}` : "" };
}

function showSession(key) {
  ensurePanel();
  showDesk();
  const opening = !shown;
  shown = lastShown = key;
  tucked = false;
  layout(opening);
  toPanel("term:show", key, lookOf(key));
  panel.webContents.focus();
}

function hidePanel() {
  shown = null;
  tucked = false;
  layout();
  win.webContents.focus();
}

// Ctrl+` switches between the desk and the terminal you had open last, from either side.
function toggleKey(event, input) {
  if (input.type !== "keyDown" || !input.control || input.key !== "`") return;
  event.preventDefault();
  if (shown) hidePanel();
  else if (lastShown && sessions.has(lastShown)) showSession(lastShown);
}

// F5 or Ctrl+R on the desk loads it again. The terminals beside it, and Claude in them, carry on.
function reloadKey(event, input) {
  if (input.type !== "keyDown" || !(input.key === "F5" || (input.control && input.key.toLowerCase() === "r"))) return;
  event.preventDefault();
  win.webContents.reload();
}

function fromPanel(event) {
  return panel && event.sender === panel.webContents;
}

// The panel is drawn over the desk, above anything the page draws, so a menu dropping from the
// desk's bar would open behind it. Instead the desk puts up a picture of the panel (a JPEG of it
// now, as a data: URL, which the desk's page may show), and the real one is put away under it
// while the menu is open: the menu opens over it.
ipcMain.handle("tatami:panel-picture", async (event) => {
  if (!fromBoard(event) || !panel || !shown || tucked) return null;
  return "data:image/jpeg;base64," + (await panel.webContents.capturePage()).toJPEG(90).toString("base64");
});
ipcMain.on("tatami:tuck", (event, on) => {
  if (!fromBoard(event) || !panel) return;
  tucked = !!on && !!shown;
  panel.setVisible(!!shown && !tucked);
});

ipcMain.on("term:input", (e, key, data) => { if (fromPanel(e)) sessions.get(key)?.proc.stdin.write(String(data)); });
ipcMain.on("term:binary", (e, key, data) => { if (fromPanel(e)) sessions.get(key)?.proc.stdin.write(Buffer.from(String(data), "latin1")); });
ipcMain.on("term:resize", (e, key, cols, rows) => {
  if (fromPanel(e) && Number.isInteger(cols) && Number.isInteger(rows) && cols > 1 && rows > 1) {
    sessions.get(key)?.proc.stdin.write(`\x1b]7373;resize;${cols};${rows}\x07`);
  }
});
ipcMain.handle("term:backlog", (e, key) => (fromPanel(e) && sessions.has(key) ? Buffer.concat(sessions.get(key).log) : null));
ipcMain.handle("term:paste", (e) => (fromPanel(e) ? clipboard.readText() : ""));
ipcMain.on("term:copy", (e, text) => { if (fromPanel(e)) clipboard.writeText(String(text)); });
ipcMain.on("term:hide", (e) => { if (fromPanel(e)) hidePanel(); });
ipcMain.on("term:width", (e, px) => {
  if (!fromPanel(e) || !Number.isFinite(px)) return;
  panelWidth = Math.round(px);
  layout();
});
ipcMain.on("term:end-session", (e, key) => {
  const s = sessions.get(key);
  if (!fromPanel(e) || !s) return;
  const name = bySession.get(key)?.id || "this Claude";
  const choice = dialog.showMessageBoxSync(win, { type: "question", buttons: ["End", "Cancel"], defaultId: 1,
    cancelId: 1, title: "Tatami Room", message: `End ${name}?`, detail: "Claude stops and this terminal closes." });
  if (choice !== 0) return;
  endSession(s);
  hidePanel();
});

// Quitting ends the app's own terminals, so it asks first while any are running.
function quit() {
  if (sessions.size) {
    const n = sessions.size;
    const choice = dialog.showMessageBoxSync(win, { type: "warning", buttons: ["Quit", "Cancel"], defaultId: 1,
      cancelId: 1, title: "Tatami Room", message: "Quit Tatami Room?",
      detail: `${n} terminal${n === 1 ? "" : "s"} in the app will close, and Claude in ${n === 1 ? "it" : "them"} stops.` });
    if (choice !== 0) return;
  }
  quitting = true;
  app.quit();
}

// Restart: the app's newest code from the repo (waifu sync-app), then the app again. Its terminals keep running
// and it attaches to them again. A short wsl.exe of its own holds WSL up while the app is away. `opening`: the app
// is only just starting (see start), so it starts again as it was asked to, once, with --updated.
async function restart(opening = false) {
  if (restarting) return;
  restarting = true;
  await tatami(["sync-app"], 30000);
  spawn("wsl.exe", ["-d", CONFIG.distro, "--", "sleep", "45"], { windowsHide: true, detached: true, stdio: "ignore" }).unref();
  saveBounds();
  const args = process.argv.slice(1).filter((a) => a !== "--updated");
  app.relaunch({ args: opening ? [...args, "--updated"] : args.filter((a) => a !== "--new-claude" && a !== "--background") });
  app.exit(0);
}

// Whether the repo has app code this app isn't running: the board says which files make up the app and their hash
// (see board.py), and the app hashes those files as they were when it started.
const STARTED_WITH = new Map();
function snapshot(dir, rel = "") {
  for (const ent of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
    const name = rel ? `${rel}/${ent.name}` : ent.name;
    if (ent.isDirectory()) snapshot(dir, name);
    else if (ent.isFile()) STARTED_WITH.set(name, fs.readFileSync(path.join(dir, name)));
  }
}
try { snapshot(__dirname); } catch { /* without it, no update notice */ }

function runningHash(files) {
  const h = crypto.createHash("sha256");
  for (const name of files) {
    const data = STARTED_WITH.get(name);
    if (!data) return null;
    h.update(name + "\0").update(data).update("\0");
  }
  return h.digest("hex");
}

function behind(s) {
  return !!s?.app && Array.isArray(s.app.files) && typeof s.app.hash === "string" && STARTED_WITH.size > 0 &&
    runningHash(s.app.files) !== s.app.hash;
}

let updateReady = false;
function checkUpdate(s) {
  if (!s.app || !Array.isArray(s.app.files) || typeof s.app.hash !== "string" || !STARTED_WITH.size) return;
  const ready = behind(s);
  if (ready === updateReady) return;
  updateReady = ready;
  trayMenu();
  if (ready && Notification.isSupported()) {
    const n = new Notification({ title: "Tatami Room has an update", icon: ICON,
      body: "Click to restart it now. Your agents keep running in their terminals." });
    n.on("click", restart);
    n.show();
  }
}

// ---- the window ----

function loadBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(BOUNDS, "utf8"));
    const area = screen.getDisplayMatching(b).workArea;  // still on a screen that's there?
    const visible = b.x < area.x + area.width && b.x + b.width > area.x && b.y < area.y + area.height && b.y + b.height > area.y;
    return visible ? b : { width: b.width, height: b.height };
  } catch {
    return { width: 1180, height: 820 };
  }
}

function saveBounds() {
  if (!win || win.isDestroyed() || win.isMinimized()) return;
  try {
    fs.writeFileSync(BOUNDS, JSON.stringify({ ...win.getNormalBounds(), maximized: win.isMaximized(), panel: panelWidth }));
  } catch { /* not worth failing over */ }
}

function createWindow(background) {
  const b = loadBounds();
  panelWidth = Number.isInteger(b.panel) ? b.panel : null;
  win = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height, minWidth: 440, minHeight: 360,
    title: "Tatami Room", icon: ICON, show: false, backgroundColor: "#110c17",
    // The desk's own header is the title bar; Windows draws the buttons over its right end, on
    // the colour the header's frosted glass comes out as there.
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#14101f", symbolColor: "#f7e9f2", height: 64 },
    webPreferences: {
      preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true,
      nodeIntegration: false, spellcheck: false,
      backgroundThrottling: false,  // keep checking in while it's hidden in the tray
    },
  });
  if (b.maximized) win.maximize();
  // What Windows pins when you pin the running window. Without it the pin takes the .exe's own
  // name and icon, which are Electron's.
  win.setAppDetails({ appId: APP_ID, appIconPath: ICON, appIconIndex: 0,
    relaunchCommand: `"${process.execPath}"`, relaunchDisplayName: "Tatami Room" });
  // Only the board, ever: no other pages, no pop-up windows.
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (e, to) => {
    try { if (new URL(to).origin !== board.origin) e.preventDefault(); } catch { e.preventDefault(); }
  });
  win.webContents.on("before-input-event", toggleKey);
  win.webContents.on("before-input-event", reloadKey);
  win.webContents.on("did-finish-load", () => layout());   // a reloaded desk hears where the panel is
  win.loadURL(board.href);
  // Show it once the desk has painted. On a first start Chromium sets up its caches and that
  // signal can go missing, so the page finishing, or a few seconds, will do as well.
  let shown = false;
  const reveal = () => {
    if (shown || win.isDestroyed()) return;
    shown = true;
    if (background) win.showInactive(); else win.show();
  };
  win.once("ready-to-show", reveal);
  win.webContents.once("did-finish-load", reveal);
  setTimeout(reveal, 4000);
  // Closing the window quits, unless Claude is running in the app's own terminals: then it asks,
  // and they can keep running in the tray instead. (Nothing runs hidden without you choosing it.)
  win.on("close", (e) => {
    saveBounds();
    if (quitting || !sessions.size) {
      quitting = true;
      return;
    }
    e.preventDefault();
    const n = sessions.size;
    const choice = dialog.showMessageBoxSync(win, { type: "question", buttons: ["Quit", "Keep running in the tray", "Cancel"],
      defaultId: 0, cancelId: 2, title: "Tatami Room", message: "Quit Tatami Room?",
      detail: `${n} terminal${n === 1 ? "" : "s"} in the app will close, and Claude in ${n === 1 ? "it" : "them"} stops. ` +
              `Or keep ${n === 1 ? "it" : "them"} running in the tray, where the desk can still tell you who needs you.` });
    if (choice === 0) {
      quitting = true;
      app.quit();
    } else if (choice === 1) {
      win.hide();
      trayHint();
    }
  });
}

function showDesk() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  // Windows may refuse to hand the focus to a program in the background; a moment on top
  // brings the desk over the other windows either way.
  win.setAlwaysOnTop(true);
  win.show();
  win.focus();
  win.moveTop();
  win.setAlwaysOnTop(false);
}

function trayHint() {
  const seen = path.join(app.getPath("userData"), "tray-hint");
  if (fs.existsSync(seen)) return;
  try { fs.writeFileSync(seen, ""); } catch { return; }
  new Notification({ title: "Tatami Room is still here", icon: ICON,
    body: "Claude keeps running in the tray. Click the tray icon to come back, or quit from its menu." }).show();
}

function createTray() {
  tray = new Tray(ICON);
  tray.setToolTip("Tatami Room");
  trayMenu();
  tray.on("click", showDesk);
}

function trayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    ...(updateReady ? [{ label: "Restart to update (agents keep running)", click: restart }, { type: "separator" }] : []),
    { label: "Open the desk", click: showDesk },
    { label: "+ Claude", click: newClaude },
    { label: "+ Claude in a Terminal window", click: newWindow },
    { type: "separator" },
    ...(updateReady ? [] : [{ label: "Restart Tatami Room (agents keep running)", click: restart }]),
    { label: "Quit Tatami Room", click: quit },
  ]));
}

// The Start menu shortcut carries the app's id, which Windows needs for its notifications, and
// which puts a pinned shortcut and the running app on one taskbar button.
function shortcuts() {
  const places = [path.join(app.getPath("appData"), "Microsoft", "Windows", "Start Menu", "Programs"), app.getPath("desktop")];
  places.forEach((dir, i) => {
    const lnk = path.join(dir, "Tatami Room.lnk");
    if (i > 0 && !fs.existsSync(lnk)) return;  // the desktop one only if it's still there
    shell.writeShortcutLink(lnk, "create", { target: process.execPath, appUserModelId: APP_ID, icon: ICON,
      iconIndex: 0, description: "Tatami Room: the desk where your Claude agents team up" });
  });
  // Pins made before the window said what to pin are named "Electron" and show Electron's icon:
  // give them the mark, and the app's id so the running app sits on its pin.
  const same = (a, b) => path.resolve(a || "").toLowerCase() === path.resolve(b).toLowerCase();
  let fixed = false;
  for (const dir of [path.join(app.getPath("appData"), "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar"), places[0]]) {
    let names = [];
    try { names = fs.readdirSync(dir).filter((n) => n.toLowerCase().endsWith(".lnk")); } catch { continue; }
    for (const name of names) {
      const lnk = path.join(dir, name);
      let s;
      try { s = shell.readShortcutLink(lnk); } catch { continue; }
      if (!same(s.target, process.execPath) || (same(s.icon, ICON) && s.appUserModelId === APP_ID)) continue;
      fixed = shell.writeShortcutLink(lnk, "update", { icon: ICON, iconIndex: 0, appUserModelId: APP_ID }) || fixed;
    }
  }
  // Windows shows a shortcut's old picture until its icon cache is refreshed: do that when a pin
  // was fixed or the mark itself changed.
  const seen = path.join(app.getPath("userData"), "icon-seen");
  let mark = "", was = "";
  try { mark = crypto.createHash("sha256").update(fs.readFileSync(ICON)).digest("hex"); } catch { return; }
  try { was = fs.readFileSync(seen, "utf8"); } catch { /* never refreshed yet */ }
  if (fixed || mark !== was) {
    execFile(path.join(process.env.WINDIR || "C:\\Windows", "System32", "ie4uinit.exe"), ["-show"], { windowsHide: true }, () => {});
    try { fs.writeFileSync(seen, mark); } catch { /* it refreshes again next start, no harm */ }
  }
}

// ---- watching the board: who needs you, and whether it's still running ----

// The board's event stream: its state each time something changes.
let misses = 0;
function watch() {
  const req = http.get({ host: "127.0.0.1", port: board.port, path: "/api/events",
    headers: { "X-Tatami-Token": board.searchParams.get("token") } }, (res) => {
    if (res.statusCode !== 200) return res.resume();
    misses = 0;
    let buf = "";
    res.setEncoding("utf8");
    res.on("data", (chunk) => {
      buf += chunk;
      for (let i; (i = buf.indexOf("\n\n")) >= 0; buf = buf.slice(i + 2)) {
        const data = buf.slice(0, i).split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("");
        if (data) try { seen(JSON.parse(data)); } catch { /* a half line: the next one will do */ }
      }
    });
  });
  req.on("error", () => {});
  req.on("close", async () => {  // the stream ended: the board stopped (WSL restarted?)
    if (++misses >= 3) {
      await tatami(["start"]);  // start it again; the page reconnects by itself
      misses = 0;
    }
    setTimeout(watch, 2000);
  });
}

const asking = new Set();
function seen(s) {
  const agents = [...s.alone, ...s.rooms.flatMap((r) => r.members)];
  windows.clear();
  for (const a of agents) if (Number.isInteger(a.hwnd)) windows.set(a.id, a.hwnd);
  for (const a of agents) {  // the app's own terminals: their agent, and its name, color and girl
    if (!a.session || !sessions.has(a.session)) continue;
    const before = JSON.stringify(lookOf(a.session));
    bySession.set(a.session, a);
    if (JSON.stringify(lookOf(a.session)) !== before) toPanel("term:look", a.session, lookOf(a.session));
  }
  for (const a of agents) if (a.status === "asking" && !asking.has(a.id)) notifyAsking(a);
  asking.clear();
  for (const a of agents) if (a.status === "asking") asking.add(a.id);
  checkUpdate(s);
}

function notifyAsking(a) {
  if (!Notification.isSupported()) return;
  const n = new Notification({ title: `${a.id} needs your OK`, icon: ICON,
    body: `${a.folder || "Claude"} is waiting on a permission prompt. Click to bring up its window.` });
  n.on("click", () => focusAgent(a.id));
  n.show();
}

// ---- starting up ----

// Is the board already running? It answers every request, even a refused one, as TatamiRoom, and
// Windows reaches it directly, which is much quicker than asking inside WSL.
function answering(url) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port: url.port, path: "/", timeout: 1500 }, (res) => {
      res.resume();
      resolve(String(res.headers.server || "").startsWith("TatamiRoom"));
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
  });
}

// The board's state, once: null if it doesn't answer in time.
function stateOf() {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port: board.port, path: "/api/state", timeout: 3000,
      headers: { "X-Tatami-Token": board.searchParams.get("token") } }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => { try { resolve(res.statusCode === 200 ? JSON.parse(body) : null); } catch { resolve(null); } });
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(null));
  });
}

async function start() {
  app.setAppUserModelId(APP_ID);
  nativeTheme.themeSource = "dark";
  Menu.setApplicationMenu(null);
  shortcuts();
  board = readBoard();
  if (!board || !(await answering(board))) {  // not running yet: start it inside WSL
    const rc = await tatami(["start"]);
    board = readBoard();
    if ((rc !== 0 && rc !== 3) || !board) {
      dialog.showErrorBox("Tatami Room", "The desk's server didn't start inside WSL. Open Ubuntu and run: tatami");
      app.exit(1);
      return;
    }
  }
  // Opened again after the repo's app code changed (quit, then opened): what opens should be that code, so it goes
  // in first and the app starts again, before anything shows, as Restart does. Only once: if it's still behind
  // after that, the tray offers Restart, as before.
  if (!process.argv.includes("--updated") && behind(await stateOf())) return restart(true);
  startHelper();  // ready before the first click
  holdWsl();
  setInterval(holdWsl, 60000);
  createWindow(process.argv.includes("--background"));
  createTray();
  watch();
  reattach();
  if (process.argv.includes("--new-claude")) startSession();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();  // it's already running: that one comes to the front (see second-instance)
} else {
  app.on("second-instance", (_e, argv) => {
    if (argv.includes("--new-claude")) startSession();
    else if (!argv.includes("--background")) showDesk();
  });
  let ended = false;
  app.on("before-quit", (e) => {
    quitting = true;
    if (!ended && sessions.size) {  // its terminals end with it, like closing their windows: first stop their agents
      e.preventDefault();
      ended = true;
      Promise.all([...sessions.values()].map(endSession)).then(() => app.quit());
      return;
    }
    saveBounds();
    if (hold) hold.kill();
    if (helper) helper.kill();
    for (const s of sessions.values()) s.proc.kill();
  });
  app.on("window-all-closed", () => { if (quitting) app.quit(); });
  app.whenReady().then(start);
}
