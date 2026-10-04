// What the terminal panel may ask of the Tatami Room app, and nothing more.
"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("term", {
  onShow: (cb) => ipcRenderer.on("term:show", (_e, key, look) => cb(key, look)),
  onLook: (cb) => ipcRenderer.on("term:look", (_e, key, look) => cb(key, look)),
  onData: (cb) => ipcRenderer.on("term:data", (_e, key, chunk) => cb(key, chunk)),
  onEnd: (cb) => ipcRenderer.on("term:end", (_e, key) => cb(key)),
  backlog: (key) => ipcRenderer.invoke("term:backlog", key),
  input: (key, data) => ipcRenderer.send("term:input", key, String(data)),
  binary: (key, data) => ipcRenderer.send("term:binary", key, String(data)),
  resize: (key, cols, rows) => ipcRenderer.send("term:resize", key, cols, rows),
  paste: () => ipcRenderer.invoke("term:paste"),
  copy: (text) => ipcRenderer.send("term:copy", String(text)),
  hide: () => ipcRenderer.send("term:hide"),
  end: (key) => ipcRenderer.send("term:end-session", key),
  width: (px) => ipcRenderer.send("term:width", Number(px)),
  onSky: (cb) => ipcRenderer.on("term:sky", (_e, pass, x, y) => cb(pass, Number(x) || 0, Number(y) || 0)),
  onAt: (cb) => ipcRenderer.on("term:at", (_e, x, y) => cb(Number(x) || 0, Number(y) || 0)),
});
