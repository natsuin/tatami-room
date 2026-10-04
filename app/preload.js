// What the desk page may ask of the Tatami Room app, and nothing more. The app checks that the
// request comes from the board's own page.
"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tatami", {
  newClaude: () => ipcRenderer.invoke("tatami:new-claude"),
  newAgent: (kind) => ipcRenderer.invoke("tatami:new-agent", String(kind)),
  focus: (id) => ipcRenderer.invoke("tatami:focus", String(id)),
  onPanel: (cb) => ipcRenderer.on("tatami:panel", (_e, width, tucked) => cb(Number(width) || 0, tucked === true)),
  panelPicture: () => ipcRenderer.invoke("tatami:panel-picture"),
  tuckPanel: (on) => ipcRenderer.send("tatami:tuck", !!on),
});
