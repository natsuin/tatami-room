#!/usr/bin/env python3
"""Tatami Room board: a web page that shows every running agent as a card. Agents on a team
sit in their team's room; the rest wait on their own along the top. Drag a card into a room
and that agent joins the room's team channel; drop one card onto another and the two of them
get a new room.

It only reads and writes the team channel's plain files in ~/.local/state/tatami. It
can't run commands or reach any terminal. It listens on this PC only (127.0.0.1), and
every request needs the secret token that's in the page's address, so other programs,
other devices and other websites can't use it.

  python3 board.py [--port 7373]    (or run ./tatami, which also opens it in your browser)
"""
import hashlib
import hmac
import json
import os
import re
import glob
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import tatami_mcp as channel  # same folder: shares the file layout and helpers
import work  # the room's plan, claims and worktrees

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(channel.HOME, "board.token")
SEEN_FILE = os.path.join(channel.HOME, "board.seen")  # touched while a desk window is open (tatami hold)
READ_FILE = os.path.join(channel.HOME, "read.json")  # agent id -> the turn of its you've seen (its status's ts)
TALK = 40  # how many of a room's newest messages the desk's chat box scrolls through
WAIFU_CONFIG = os.path.expanduser("~/.config/waifu/config.json")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
TINTS = {"cherry": "#4c112c", "rouge": "#491a1f", "wine": "#340417", "plum": "#360d30",
         "mauve": "#4d303e", "orchid": "#42194d", "grape": "#2a1748", "iris": "#161141",
         "indigo": "#051032", "dusk": "#0f2e4d", "haze": "#313955", "matcha": "#15361b"}
# The colours windows were dealt before the sakura palette, for windows still open from then.
OLD_TINTS = {"red": "#411010", "amber": "#412910", "olive": "#414110", "lime": "#294110",
             "green": "#104110", "jade": "#104129", "teal": "#104141", "sky": "#102941",
             "blue": "#101041", "violet": "#291041", "magenta": "#411041", "rose": "#411029"}


def tint(color):
    """A window's background colour, by the name waifu dealt it."""
    return TINTS.get(color or "") or OLD_TINTS.get(color or "", "#2a2233")


def token():
    os.makedirs(channel.HOME, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(24))
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def model_name(model):
    """'claude-opus-5-5' -> 'Opus 5.5', 'claude-haiku-4-5-20251001' -> 'Haiku 4.5'."""
    model = model.split("[")[0]  # 'claude-opus-5-5[1m]'
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", model)
    if m:
        family, major, minor = m.groups()
    else:
        m = re.fullmatch(r"claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?", model)  # 'claude-3-5-sonnet-20241022'
        if not m:
            return model.removeprefix("claude-")[:24]
        major, minor, family = m.groups()
    return f"{family.title()} {major}" + (f".{minor}" if minor else "")


# What's been read of each Claude transcript so far, so each poll reads only what was added.
TRANSCRIPTS = {}
reading = threading.Lock()
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # Claude Code's /effort levels
NO_SESSION = {"model": None, "effort": None, "summary": None}


def said(content, what):
    """What a /model or /effort command said it set: 'Set model to `Opus 5.5` and saved as your
    default...', 'Set effort level to max (this session only): ...'."""
    if not isinstance(content, str) or not content.startswith("<local-command-stdout>Set " + what + " to "):
        return None
    words = re.sub(r"\x1b\[[0-9;]*m|`", "", content).split(" to ", 1)[1]
    return re.match(r"[A-Z][a-z]+ \d+(?:\.\d+)?\b" if what == "model" else r"\w+", words)


def session_of(rec):
    """What Claude Code says about a Claude agent's session: the model it's on ('Opus 5.5'), its
    effort level ('xhigh'), and the session's title, a few words on what it's for (the one
    /rename gave it, else the one Claude Code wrote after its first prompt). Claude Code keeps a
    file per running Claude process saying which session it's in; the transcript says the rest.
    The model shows from the first prompt on, effort from the first reply, and both change as
    soon as /model or /effort does."""
    started = rec.get("started")
    s = channel.load(os.path.join(CLAUDE_DIR, "sessions", f"{rec.get('pid')}.json"), {})
    sid = str(s.get("sessionId", ""))
    if not re.fullmatch(r"[0-9a-f-]{36}", sid) or (started is not None and str(s.get("procStart")) != str(started)):
        return NO_SESSION  # no session file, or one left by an earlier process with this pid
    with reading:
        seen = next((t for t in TRANSCRIPTS.values() if t["sid"] == sid), None)
        if not seen:
            path = next(iter(glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", sid + ".jsonl"))), None)
            if not path:
                return NO_SESSION
            seen = TRANSCRIPTS[path] = {"sid": sid, "path": path, "at": 0, "model": None, "effort": None,
                                        "title": None, "named": None}
        try:
            with open(seen["path"], "rb") as f:
                f.seek(seen["at"])
                added = f.read()
        except OSError:
            return NO_SESSION
        whole = added[:added.rfind(b"\n") + 1]  # a line still being written waits for the next poll
        seen["at"] += len(whole)
        for line in whole.splitlines():
            if not any(w in line for w in (b'"assistant"', b'"type":"model"', b"Set model to", b"Set effort level to",
                                           b'"ai-title"', b'"custom-title"')):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if d.get("isSidechain"):
                continue
            if kind == "assistant":   # a reply says which model wrote it and at what effort
                model = (d.get("message") or {}).get("model")
                if isinstance(model, str) and model.startswith("claude"):  # not '<synthetic>'
                    seen["model"] = model_name(model)
                    seen["effort"] = d.get("effort") if d.get("effort") in EFFORTS else None  # Haiku has none
            elif kind == "attachment":   # sent with a prompt, before the reply
                model = ((d.get("attachment") or {}).get("identity") or {}).get("modelId")
                if (d.get("attachment") or {}).get("type") == "model" and isinstance(model, str) and model.startswith("claude"):
                    seen["model"] = model_name(model)
            elif kind == "user":
                content = (d.get("message") or {}).get("content")
                if m := said(content, "model"):
                    seen["model"] = m[0]
                elif m := said(content, "effort level"):
                    seen["effort"] = m[0] if m[0] in EFFORTS else None   # 'auto': known after the next reply
            elif kind == "ai-title" and isinstance(d.get("aiTitle"), str):
                seen["title"] = d["aiTitle"]
            elif kind == "custom-title" and isinstance(d.get("customTitle"), str):
                seen["named"] = d["customTitle"]
        return {"model": seen["model"], "effort": seen["effort"],
                "summary": clip(seen["named"] or seen["title"] or "", 60) or None}


def girls_hidden():
    """`waifu off` hides every girl for screen sharing, so the board hides them too."""
    return bool(channel.load(channel.WAIFU_STATE, {}).get("off"))


def agents():
    """The running agents, as the page shows them. A room of None means on its own. With the
    hooks on, `status` says whether each one is working, done (your turn) or asking you, and
    `read` that you've brought one up since its turn ended, so it needn't glow for you.
    `unread` counts the room's messages it hasn't read: one waiting for you won't see them
    until you talk to it."""
    hidden, home = girls_hidden(), os.path.expanduser("~")
    rooms = {}
    def unread(rec):
        if not rec["room"]:
            return 0
        if rec["room"] not in rooms:
            rooms[rec["room"]] = channel.load_jsonl(os.path.join(channel.ROOMS, rec["room"] + ".jsonl"))
        return len(channel.unread(rec, rec["room"], rooms[rec["room"]]))
    seen = channel.load(READ_FILE, {})
    def status(rec):
        s = channel.load(os.path.join(channel.HOME, "status", rec["id"] + ".json"), {})
        # Your turn, and you've already brought it up since it finished: it stops glowing.
        return {"status": s.get("state"), "read": s.get("state") == "done" and seen.get(rec["id"]) == s.get("ts")}
    def task(rec):  # the task it's on, from its room's plan
        t = work.current(work.tasks(rec["room"]), rec["id"]) if rec["room"] else None
        return {"id": t["id"], "title": clip(t["title"], 80)} if t else None
    return [{"id": rec["id"], "agent": rec.get("agent", "claude"), "room": rec["room"],
             "unread": unread(rec), "helper_of": rec.get("invited_by"),
             "task": task(rec), "worktree": worktree(rec["id"]),
             "color": rec.get("color"), "tint": tint(rec.get("color")), "born": channel.born(rec),
             "folder": "~" if rec.get("cwd") == home else os.path.basename(rec.get("cwd", "")),
             "seen": rec.get("seen", 0), "girl": bool(rec.get("girl")) and not hidden,
             # its window's handle, found by the agent itself, which the app uses to bring it up
             "hwnd": rec["hwnd"] if isinstance(rec.get("hwnd"), int) else None,
             # set when the agent runs in a terminal inside the Tatami Room app
             "session": rec["session"] if re.fullmatch(r"[a-z0-9-]{1,40}", str(rec.get("session"))) else None}
            | status(rec) | (session_of(rec) if rec.get("agent", "claude") == "claude" else NO_SESSION)
            for rec in channel.live_agents()]


TREES = {}  # worktree path -> (when it was looked at, commits to land), so git runs every few seconds at most


def worktree(agent_id):
    """The agent's own worktree, as its card shows it: its branch and the commits waiting to land."""
    for main, rec in work.trees().get(agent_id, {}).items():
        seen = TREES.get(rec["path"])
        if not seen or time.time() - seen[0] > 5:
            seen = TREES[rec["path"]] = (time.time(), work.ahead(rec) if os.path.isdir(rec["path"]) else 0)
        return {"branch": rec["branch"], "base": rec["base"], "repo": os.path.basename(main), "ahead": seen[1]}
    return None


def plan_of(name, people):
    """A room's plan, claims and all, as the desk shows it under its cards."""
    items = work.tasks(name)
    landed = [t for t in items if t["status"] == "landed"]
    shown = [t for t in items if t["status"] != "landed"] + landed[-3:]
    claims = []
    for c in work.claims():
        if c["room"] != name:
            continue
        repo = c.get("repo")  # named inside its repository; no git here, this runs several times a second
        short = os.path.relpath(c["path"], repo) + ("/" if c["path"].endswith("/") else "") if repo \
            else os.path.basename(c["path"].rstrip("/")) or c["path"]
        claims.append({"agent": c["agent"], "path": c["path"], "task": c.get("task"), "short": short})
    return {"tasks": [{"id": t["id"], "title": clip(t["title"], 120), "owner": t.get("owner"),
                       "status": t["status"], "note": clip(t.get("note") or "", 200),
                       "done_when": clip(t.get("done_when") or "", 200), "branch": t.get("branch")} for t in shown],
            "landed": len(landed), "claims": claims}


def page_version():
    """Which board.html and kinds.json are on disk. An open desk loads the page again when this
    changes."""
    try:
        return "-".join(str(os.stat(f).st_mtime_ns) for f in (os.path.join(HERE, "board.html"), channel.KINDS_FILE))
    except OSError:
        return None


APP_CODE = {"files": None, "at": None, "out": None}


def app_code():
    """The app's own code in this repo: the files the installed app is made of (waifu's APP_FILES)
    and their hash. The app hashes the files it started with the same way, and offers to restart
    when they differ, so `git pull` reaches it (see app/main.js). Worked out again only when a
    file changes."""
    if APP_CODE["files"] is None:
        try:
            APP_CODE["files"] = list(channel.waifu_cli().APP_FILES)
        except (Exception, SystemExit):  # no waifu next door: no app to update
            APP_CODE["files"] = []
    folder = os.path.join(os.path.dirname(HERE), "app")
    try:
        at = [os.stat(os.path.join(folder, f)).st_mtime_ns for f in APP_CODE["files"]]
    except OSError:
        return None
    if at != APP_CODE["at"]:
        h = hashlib.sha256()
        for f in APP_CODE["files"]:
            with open(os.path.join(folder, f), "rb") as fh:
                h.update(f.encode() + b"\0" + fh.read() + b"\0")
        APP_CODE.update(at=at, out={"files": APP_CODE["files"], "hash": h.hexdigest()} if APP_CODE["files"] else None)
    return APP_CODE["out"]


def makers():
    """Each kind of agent's maker, for the page: its name, and its mark in its colour."""
    page = {k: {"name": v["name"], "color": v["color"], "line": v["line"], "d": v["mark"]} for k, v in channel.kinds().items()}
    return json.dumps(page).replace("</", "<\\/")  # it goes inside a <script>


def state():
    people = agents()
    colors = channel.room_colors(channel.live_rooms(people))  # oldest room first, so rooms stay put
    births = {a["id"]: a["born"] for a in people}

    def flags(m):   # an id said before its live agent took it was an earlier agent's
        return ({"earlier": True} if channel.earlier(m["from"], m["ts"], births) else {}) \
            | ({"to_earlier": True} if channel.earlier(m.get("to"), m["ts"], births) else {})
    rooms = []
    for name in colors:
        msgs = channel.load_jsonl(os.path.join(channel.ROOMS, name + ".jsonl"))
        rooms.append({"name": name, "color": channel.PALETTE[colors[name]],
                      "members": [a for a in people if a["room"] == name], "said": len(msgs),
                      "lead": channel.lead_of(name, people),   # its orchestrator, if it has one
                      "plan": plan_of(name, people),
                      "recent": [{"from": m["from"], "to": m.get("to"), "text": m["text"][:channel.MAX_TEXT],
                                  "ts": m["ts"], "via": m.get("via")} | flags(m) for m in msgs[-TALK:]]})
    return {"rooms": rooms, "alone": [a for a in people if not a["room"]], "now": time.time(), "usage": usage(),
            "page": page_version(), "app": app_code(),
            # the kinds of agent that are installed, which the desk offers to start
            "installed": [k for k in channel.kinds() if channel.command_of(k)]}


def usage():
    """Your Claude usage (the 5-hour and weekly windows), as the Tatami Room status line last
    saw it. A window whose reset time has passed has started over."""
    limits = channel.load(os.path.join(channel.HOME, "usage.json"), {}).get("rate_limits") or {}
    out = {}
    for key in ("five_hour", "seven_day"):
        w = limits.get(key) or {}
        if isinstance(w.get("used_percentage"), (int, float)):
            over = (w.get("resets_at") or 0) and w["resets_at"] < time.time()
            out[key] = {"used": 0 if over else w["used_percentage"], "resets": None if over else w.get("resets_at")}
    return out


def clip(text, n=280):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def room_name(raw):
    raw = str(raw or "")
    return channel.safe_name(raw) if any(c.isalnum() for c in raw) else None


def known_agent(raw):
    aid = channel.safe_name(str(raw or ""))
    return aid if raw and os.path.isfile(os.path.join(channel.AGENTS, aid + ".json")) else None


def change(path, body):
    """Make one change to the channel's files. Returns (status, error) when it can't.
    Call it holding the channel's lock."""
    if path == "/api/room":
        room = room_name(body.get("room"))
        if not room:
            return 400, "A room name needs at least one letter or number."
        rooms = channel.load(channel.ROOMS_FILE, [])
        if body.get("remove"):  # only empty rooms; their messages stay on disk
            if any(a["room"] == room for a in channel.live_agents()):
                return 409, "Move its agents out first."
            channel.save(channel.ROOMS_FILE, [r for r in rooms if r != room])
        elif room not in rooms:
            channel.save(channel.ROOMS_FILE, rooms + [room])
        return None
    agent = known_agent(body.get("agent"))
    if not agent and not (path == "/api/lead" and body.get("agent") is None):
        return 404, "No agent with that id."
    if path == "/api/lead":  # this room's orchestrator, or (no agent) none
        return lead(room_name(body.get("room")), agent if body.get("agent") else None)
    if path == "/api/claim":  # you let go of an agent's claim on a file for it; its room hears it
        return unclaim(agent, str(body.get("path") or ""))
    if path == "/api/read":  # you brought it up: this turn of its is seen, until its next one
        s = channel.load(os.path.join(channel.HOME, "status", agent + ".json"), {})
        if s.get("state") == "done":
            live = {a["id"] for a in channel.live_agents()}
            seen = {k: v for k, v in channel.load(READ_FILE, {}).items() if k in live}
            channel.save(READ_FILE, seen | {agent: s.get("ts")})
        return None
    members = channel.load(channel.MEMBERS, {})
    was = {a["id"]: a["room"] for a in channel.live_agents()}  # so a room hears who joined it
    if path == "/api/move":  # into a room, or with no room: on its own
        room = room_name(body.get("room"))
        if body.get("room") and not room:
            return 400, "A room name needs at least one letter or number."
        members[agent] = room
    else:  # /api/team: one card dropped onto another
        other = known_agent(body.get("with"))
        if not other or other == agent:
            return 404, "No agent with that id."
        people = channel.live_agents()
        room = next((a["room"] for a in people if a["id"] == other), None)
        if not room:  # the other one was on its own too: a new room for the two of them
            room = channel.fresh_room(channel.live_rooms(people))
            members[other] = room
        members[agent] = room
    channel.save(channel.MEMBERS, members)
    leads = channel.load(channel.LEADS_FILE, {})
    left = [r for r, a in leads.items() if a == agent and r != room]   # an orchestrator that leaves its room isn't one
    if left:
        channel.save(channel.LEADS_FILE, {r: a for r, a in leads.items() if r not in left})
        for r in left:
            channel.post(r, {"ts": time.time(), "from": channel.USER, "via": "desk",
                             "text": channel.LEFT_SAYS.format(lead=agent)})
    if room and was.get(agent) != room:
        joined(agent, room, was)
    return None


def joined(agent, room, was):
    """Tell a room the user brought `agent` into it, so no one plans without it: its orchestrator
    directly (that wakes it, or holds up the end of its turn), else everyone. A room no one else
    was in yet has no one to tell. Call it holding the channel's lock."""
    if not any(r == room and a != agent for a, r in was.items()):
        return
    lead = channel.lead_of(room)
    owns = [t["id"] for t in work.tasks(room) if t.get("owner") == agent and t["status"] in work.OPEN]
    msg = {"ts": time.time(), "from": channel.USER, "via": "desk"}
    if lead and lead != agent:
        says = channel.JOINED_BACK if owns else channel.JOINED_SAYS
        msg.update(to=lead, text=says.format(agent=agent, lead=lead, tasks=", ".join(owns)))
    else:
        msg.update(text=channel.JOINED_PEERS.format(agent=agent) + ("" if owns else " It isn't on any task here yet."))
    channel.post(room, msg)


def lead(room, agent):
    """Make `agent` the orchestrator of `room`, or with None, give it none. The room hears it
    from the user, so every agent in it knows. Call it holding the channel's lock."""
    people = channel.live_agents()
    if not room or not any(a["room"] == room for a in people):
        return 404, "No room with that name."
    if agent and not any(a["id"] == agent and a["room"] == room for a in people):
        return 409, "Only an agent in the room can be its orchestrator."
    was = channel.lead_of(room, people)
    if agent == was:
        return None
    leads = {r: a for r, a in channel.load(channel.LEADS_FILE, {}).items() if r != room}
    channel.save(channel.LEADS_FILE, leads | ({room: agent} if agent else {}))
    msg = {"ts": time.time(), "from": channel.USER, "via": "desk"}
    if agent:   # to the new orchestrator, so it reads it before it ends its turn; everyone sees it
        msg.update(to=agent, text=channel.LEAD_SAYS.format(lead=agent))
    else:
        msg.update(text=channel.UNLEAD_SAYS.format(lead=was))
    channel.post(room, msg)
    return None


def unclaim(agent, path):
    """Take a claim off an agent, from the desk. Call it holding the channel's lock."""
    held = next((c for c in channel.load(work.CLAIMS, []) if c["agent"] == agent and c["path"] == path), None)
    if not held:
        return 404, "That claim has gone already."
    work.release(agent, [path])
    channel.post(held["room"], {"ts": time.time(), "from": channel.USER, "via": "desk", "to": agent,
                                "text": f"I took your claim on {work.tilde(path)} off on the desk, so your teammates "
                                        f"can edit it now. Check with them before you change it again."})
    return None


def looked_at():
    """Note that a desk window is open. WSL stops a distro soon after its last terminal
    closes, so `tatami hold` keeps it up for as long as this keeps being touched."""
    try:
        if time.time() - os.path.getmtime(SEEN_FILE) < 10:
            return
    except OSError:
        pass
    with open(SEEN_FILE, "w"):
        pass


def publish(url):
    """Leave the desk's address where the Windows shortcuts can read it (next to waifu's
    launcher scripts), so the Tatami Room shortcut can open it."""
    launcher = channel.load(WAIFU_CONFIG, {}).get("launcher_dir")
    if launcher and os.path.isdir(launcher):
        with open(os.path.join(launcher, "board.url"), "w") as f:
            f.write(url + "\r\n")


def girl_file(agent_id):
    """The dot-art girl for an agent's Claude Waifu window, looked up by id only."""
    rec = channel.load(os.path.join(channel.AGENTS, channel.safe_name(agent_id) + ".json"), {})
    pool = channel.load(WAIFU_CONFIG, {}).get("pool")
    if not rec.get("girl") or not pool or girls_hidden():
        return None
    path = os.path.join(pool, "dots", os.path.basename(rec["girl"]).rsplit(".", 1)[0] + ".png")
    return path if os.path.isfile(path) else None


class Board(BaseHTTPRequestHandler):
    server_version = "TatamiRoom/0.2"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def allowed(self):
        """Only this PC, only our own page, only with the token."""
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False  # blocks DNS-rebinding tricks from other websites
        given = parse_qs(urlparse(self.path).query).get("token", [""])[0] or self.headers.get("X-Tatami-Token", "")
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def send(self, code, body, kind="application/json", nonce=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if nonce:  # the page: only its own script and style run, and it only talks to this server
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                             "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                             "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/":
            nonce, version = secrets.token_urlsafe(16), page_version() or ""
            with open(os.path.join(HERE, "board.html"), encoding="utf-8") as f:
                page = f.read().replace("__TOKEN__", self.server.token).replace("__NONCE__", nonce) \
                    .replace("__PAGE__", version).replace("__MAKERS__", makers())
            return self.send(200, page.encode(), "text/html; charset=utf-8", nonce)
        if path == "/api/state":
            looked_at()
            return self.send(200, state())
        if path == "/api/events":
            return self.events()
        if path.startswith("/girl/"):
            f = girl_file(path[len("/girl/"):])
            if f:
                with open(f, "rb") as fh:
                    return self.send(200, fh.read(), "image/png")
        if path.startswith("/sky/"):  # the sky's paintings (tatami/sky/*.webp)
            name = path[len("/sky/"):]
            f = os.path.join(HERE, "sky", name)
            if re.fullmatch(r"[a-z0-9-]+\.webp", name) and os.path.isfile(f):
                with open(f, "rb") as fh:
                    return self.send(200, fh.read(), "image/webp")
        return self.send(404, {"error": "not found"})

    def events(self):
        """Server-sent events: the desk's state each time it changes, so the page and the app hear
        about a move or a new agent straight away instead of asking every few seconds."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        last, sent = None, time.time()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                looked_at()  # a desk is open, which keeps WSL up (tatami hold)
                s = state()
                now = json.dumps({k: v for k, v in s.items() if k != "now"}, sort_keys=True)
                if now != last:
                    last, sent = now, time.time()
                    self.wfile.write(b"data: " + json.dumps(s).encode() + b"\n\n")
                    self.wfile.flush()
                elif time.time() - sent > 15:  # now and then, to notice a reader that has gone
                    sent = time.time()
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
                time.sleep(0.4)
        except OSError:  # the page or the app went away
            return

    def do_POST(self):
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if not self.allowed() or origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self.send(403, {"error": "forbidden"})
        length = self.headers.get("Content-Length") or "0"
        if not length.isdigit() or int(length) > 10_000:
            return self.send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        if path not in ("/api/room", "/api/move", "/api/team", "/api/read", "/api/lead", "/api/claim"):
            return self.send(404, {"error": "not found"})
        with channel.locked():  # the agents write these files too
            failed = change(path, body)
        if failed:
            return self.send(failed[0], {"error": failed[1]})
        return self.send(200, state())


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7373
    for d in (channel.ROOMS, channel.AGENTS):
        os.makedirs(d, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Board)
    server.token = token()
    url = f"http://127.0.0.1:{port}/?token={server.token}"
    publish(url)
    print(f"Tatami Room: {url}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
