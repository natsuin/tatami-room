#!/usr/bin/env python3
"""A terminal for the Tatami Room app. It runs an agent (Claude, or another kind from kinds.json:
Gemini, Codex) in a pseudo-terminal inside WSL, the way a Claude Waifu window runs Claude, and
relays it over plain pipes: the app starts it through wsl.exe and draws it with xterm.js, so
nothing needs native modules on the Windows side.

The agent doesn't belong to the app, so restarting the app (to update it, or after a crash)
doesn't stop it. A small holder in the background owns the pseudo-terminal, keeps what the
agent printed lately, and waits on a socket in ~/.local/state/tatami/terms; what the app runs
attaches to it, gets that backlog first (so the app can draw the screen again), then relays.
When the app goes away the holder carries on, and the app attaches again when it comes back.
The holder ends when the agent's shell exits, or when the app ends the terminal.

It starts in ~/desk, a folder of its own: Claude Code asks once whether you trust a folder, and
remembers the answer for that folder, where your home folder would ask every time.

  tatami term <session> [kind]    what the app runs: attach to that session's holder, starting
                                  it (and the agent) first if there's none; <kind> says which
                                  agent (claude when it's left out)
  tatami term --list              the sessions whose holders are running, as JSON lines
                                  ({"session", "kind"}), for the app to attach to again

The app resizes the terminal in-band: ESC ] 7373 ; resize ; <cols> ; <rows> BEL, and ends it
with ESC ] 7373 ; end BEL. It says ESC ] 7373 ; keep BEL when it attaches, meaning it will
attach again: only then does the agent carry on when the app goes away. Without it (an app
from before holders), going away ends the agent, as closing a window would. Before it
starts, waifu picks a girl and a color for it, like it does for each new window.
"""
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time

import tatami_mcp  # same folder: where the desk folder is

RESIZE = re.compile(rb"\x1b\]7373;resize;(\d{1,4});(\d{1,4})\x07")
END = b"\x1b]7373;end\x07"
KEEP_ON = b"\x1b]7373;keep\x07"  # the app will attach again: keep the agent while it's away
WAIFU = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "waifu")
TERMS = os.path.join(tatami_mcp.HOME, "terms")
KEEP = 4 << 20  # what a holder keeps of the agent's output, for an app that attaches (the app keeps as much)
SESSION = re.compile(r"[a-z0-9-]{1,40}")


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def look():
    """A girl and a color for this terminal, dealt by waifu like a new window's."""
    try:
        # A few seconds at most: waifu can be busy downloading, and Claude shouldn't wait for a girl.
        out = subprocess.run([WAIFU, "pick"], capture_output=True, text=True, timeout=6).stdout
        pick = json.loads(out.strip().splitlines()[-1])
        return str(pick.get("color") or ""), str(pick.get("girl") or "")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return "", ""


DESK_NOTE = """# The Tatami Room desk

Claude sessions started from the Tatami Room app begin in this folder. It's only a starting
point: the user's projects live in their home folder (~), so work wherever they ask.
"""


def desk():
    """The app's terminals start here; made the first time, with a note for Claude, and for other
    agents in AGENTS.md, the file they read (added once to a desk made before them)."""
    folder = tatami_mcp.DESK_DIR
    made = not os.path.isdir(folder)
    os.makedirs(folder, exist_ok=True)
    for note in ("CLAUDE.md", "AGENTS.md") if made else ("AGENTS.md",):
        try:
            with open(os.path.join(folder, note), "x", encoding="utf-8") as f:
                f.write(DESK_NOTE.replace("Claude sessions", "Agent sessions") if note == "AGENTS.md" else DESK_NOTE)
        except FileExistsError:
            pass
    return folder


def paths(session):
    return os.path.join(TERMS, session + ".sock"), os.path.join(TERMS, session + ".json")


def at_socket(sock, how, path):
    """sock.bind or sock.connect to `path`, from inside its folder: a socket's address can't be
    longer than about 100 bytes, which a long state folder would pass."""
    here = os.getcwd()
    os.chdir(os.path.dirname(path))
    try:
        getattr(sock, how)(os.path.basename(path))
    finally:
        os.chdir(here)


def running(session):
    """The session's holder record, while its holder is running."""
    rec = tatami_mcp.load(paths(session)[1], {})
    return rec if rec and tatami_mcp.alive(rec) and os.path.exists(paths(session)[0]) else None


# ---- the holder: owns the agent's pseudo-terminal ----

def hold(session, kind):
    """Run the agent and keep it, whether or not the app is attached."""
    sock_path, rec_path = paths(session)
    os.makedirs(TERMS, mode=0o700, exist_ok=True)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # the app's wsl.exe going away is no reason to stop
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # but a kill tidies up and hangs up the agent
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(sock_path):
        os.remove(sock_path)
    old = os.umask(0o077)
    at_socket(server, "bind", sock_path)
    os.umask(old)
    server.listen(2)
    tatami_mcp.save(rec_path, {"session": session, "kind": kind, "pid": os.getpid(),
                               "started": tatami_mcp.proc_start(os.getpid()), "since": time.time()})
    kept, size = [], 0

    def keep(data):
        nonlocal size
        kept.append(data)
        size += len(data)
        while size > KEEP and len(kept) > 1:
            size -= len(kept.pop(0))

    client, keeping = None, False

    def drop():
        nonlocal client, keeping
        if client:
            client.close()
        client, keeping = None, False

    def send(data):
        if client:
            try:
                client.sendall(data)
            except OSError:
                drop()

    name = tatami_mcp.kinds().get(kind, {}).get("name", kind)
    command = tatami_mcp.command_of(kind)  # a plain word from kinds.json: safe to put in the shell line
    keep(f"\x1b[2mStarting {name}…\x1b[0m".encode())  # something to see straight away
    # The first attach comes while waifu deals the look; take it now, so it sees the line above.
    if select.select([server], [], [], 2)[0]:
        client, _ = server.accept()
        send(b"".join(kept))
    color, girl = look()
    start = desk()
    pid, fd = pty.fork()
    if pid == 0:  # the terminal's side: the agent, then a shell once it exits, like a Claude Waifu window
        os.environ.update(TERM="xterm-256color", COLORTERM="truecolor", TATAMI_SESSION=session,
                          TATAMI_COLOR=color, TATAMI_GIRL=girl)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        os.chdir(start)
        if not command:
            print(f"\r\x1b[2K{name} isn't installed here, or kinds.json doesn't know it.\r")
            os.execvp("bash", ["bash", "-li"])
        # TATAMI_AGENT reaches the agent's Tatami Room channel (its MCP server), which puts it on
        # the desk as this kind. Only this agent's: another started later in the shell is its own.
        # Ctrl+Z suspends Claude, and the shell would go on to `exec bash`, which leaves it stopped
        # where `fg` can't reach it: the terminal would show a prompt, its card an agent that's
        # gone. So a suspended agent goes straight on again.
        os.execvp("bash", ["bash", "-lic", f"TATAMI_AGENT={kind} {command}; "
                                           "while jobs %% >/dev/null 2>&1; do fg >/dev/null; done; exec bash"])
    set_size(fd, 100, 30)
    keep(b"\r\x1b[2K")  # Claude draws from here
    send(b"\r\x1b[2K")
    try:
        while True:
            ready, _, _ = select.select([server, fd] + ([client] if client else []), [], [])
            if server in ready:  # the app, back: it gets the backlog, and takes over from any other
                drop()
                client, _ = server.accept()
                send(b"".join(kept))
            if client and client in ready:
                try:
                    data = client.recv(65536)
                except OSError:
                    data = b""
                if not data:  # the app went away: carry on without it, if it said it'd be back
                    if not keeping:
                        break
                    drop()
                elif END in data:  # the app ended the terminal
                    break
                else:
                    if KEEP_ON in data:
                        keeping = True
                        data = data.replace(KEEP_ON, b"")
                    for cols, rows in RESIZE.findall(data):
                        set_size(fd, max(2, int(cols)), max(2, int(rows)))
                    data = RESIZE.sub(b"", data)
                    if data:
                        os.write(fd, data)
            if fd in ready:
                try:
                    out = os.read(fd, 65536)
                except OSError:  # the shell exited
                    break
                if not out:
                    break
                keep(out)
                send(out)
    finally:
        drop()
        for p in (sock_path, rec_path):
            try:
                os.remove(p)
            except OSError:
                pass
        hang_up(pid, fd)


def in_session(sid):
    """The processes still running in the terminal's session (its shell's, which pty.fork began)."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                state, _ppid, _pgrp, session = f.read().rsplit(b")", 1)[1].split()[:4]
        except (OSError, ValueError):
            continue
        if int(session) == sid and state not in (b"Z", b"X"):
            out.append(int(name))
    return out


def hang_up(shell, fd):
    """End the terminal, as closing a window would: hang up its shell and the agent, and anything
    else still in it, such as an agent stopped in the background, which a hang-up alone never
    reaches. A stopped one is let go on to hear it. Whatever is left after a few seconds is
    killed: nothing that was in the terminal outlives it, to stay on the desk with nowhere to show."""
    try:
        os.close(fd)
    except OSError:
        pass

    def signal_all(*sigs):
        for p in in_session(shell):
            for sig in sigs:
                try:
                    os.kill(p, sig)
                except OSError:
                    pass

    signal_all(signal.SIGHUP, signal.SIGCONT)
    for _ in range(50):
        if not in_session(shell):
            return
        time.sleep(0.1)
    signal_all(signal.SIGKILL)


# ---- what the app runs: attach, starting the holder if need be ----

def attach(session, kind):
    sock_path = paths(session)[0]
    log = os.path.join(TERMS, session + ".log")  # what a holder that fails to start says
    if not running(session):
        os.makedirs(TERMS, mode=0o700, exist_ok=True)
        with open(log, "w") as err:
            subprocess.Popen([sys.executable, os.path.realpath(__file__), "--hold", session, kind],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
    conn = None
    for _ in range(100):  # the holder's socket is up within a moment
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            at_socket(conn, "connect", sock_path)
            break
        except OSError:
            conn.close()
            conn = None
            time.sleep(0.05)
    if not conn:
        try:
            with open(log) as f:
                why = f.read().strip().splitlines()[-1:]
        except OSError:
            why = []
        sys.exit(f"The terminal's holder didn't start{': ' + why[0] if why else '.'}")
    try:
        while True:
            ready, _, _ = select.select([0, conn], [], [])
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:  # the app closed: leave the agent running
                    break
                conn.sendall(data)
            if conn in ready:
                out = conn.recv(65536)
                if not out:  # the agent's shell exited, or the app ended it
                    break
                os.write(1, out)
    except OSError:
        pass
    finally:
        conn.close()


def sessions():
    """The sessions whose holders are running."""
    out = []
    for fn in sorted(os.listdir(TERMS)) if os.path.isdir(TERMS) else []:
        if fn.endswith(".json") and SESSION.fullmatch(fn[:-5]):
            rec = running(fn[:-5])
            if rec:
                out.append({"session": rec["session"], "kind": rec.get("kind") or "claude"})
    return out


def main():
    args = sys.argv[1:]
    if args[:1] == ["--list"]:
        for s in sessions():
            print(json.dumps(s))
        return
    held = args[:1] == ["--hold"]
    args = args[1:] if held else args
    session = args[0] if args else ""
    kind = args[1] if len(args) > 1 and args[1] else "claude"
    if not SESSION.fullmatch(session) or not re.fullmatch(r"[a-z0-9-]{1,30}", kind):
        sys.exit("usage: tatami term <session> [kind]")
    if held:
        hold(session, kind)
    else:
        attach(session, kind)


if __name__ == "__main__":
    main()
