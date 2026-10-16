#!/usr/bin/env python3
"""Tatami Room status hooks, so the board can show which agents need you.

Claude Code runs this on a few events: when you send a prompt, after each tool it uses,
when it finishes its turn, and when it's waiting for you to OK something (and the mod adds
one of its own, PermissionGranted, as the tool you OK'd starts to run). Each time it
notes the agent's state in one small file (~/.local/state/tatami/status/<agent id>.json):

  working   Claude is busy
  done      it finished its turn: your turn
  asking    it's waiting for your OK (a permission prompt or a question)

It always exits 0. Usually it prints nothing. What it tells Claude is about its room:

  after a tool, or when you send a prompt   a short note: "2 new messages in room X"
  when it's about to finish its turn        if a message is addressed to it, it's asked to
                                            read the room first; if its task on the room's
                                            plan is still in progress, or finished work is
                                            waiting for it to land, it's reminded once

So an agent busy with its work still hears its team, without checking the room every few
minutes. One that's waiting for you can't hear anything until you talk to it; the board
puts a badge on its card.

For Claude the Tatami Room mod runs these from inside Claude Code (tatami mod on: see
mod.py), so they're the one way its status reaches the desk and nothing goes in Claude
Code's settings. Gemini, as Antigravity's CLI (agy), has no mod, so it runs them from
~/.gemini/config/hooks.json. Its events are named differently and don't say which one they
are, so its command names it: before each call to the model it's working, after a tool too,
and when its loop stops it's your turn. It has no event for asking your OK, so its card says
"working" while it asks. Before it edits a file or runs a command, its PreToolUse hook asks
the same guard the mod asks for Claude, so on a team it works in a worktree of its own and
leaves its teammates' files alone. Codex gets that guard too, from ~/.codex/hooks.json
(`tatami hook codex`, which reads Claude Code's hook shape).

  tatami hooks on     the mod for Claude (tatami mod on), and Gemini's and Codex's hooks
                      when they're installed
  tatami hooks off    Gemini's and Codex's hooks off (tatami mod off turns Claude's part off)
  tatami hooks gemini on|off, tatami hooks codex on|off
                      only Gemini's, or only Codex's

Earlier versions put Claude's hooks in ~/.claude/settings.json; on and off both take those
out, so the mod and the settings never both run them.
"""
import json
import os
import re
import shlex
import sys
import time

import tatami_mcp as channel  # same folder: shares the file layout and helpers
import work  # the room's plan, for what's left when a turn ends

SETTINGS = os.path.expanduser("~/.claude/settings.json")
COMMAND = '"$HOME/.local/bin/tatami" hook'  # through the link, so moving the repo can't break it
EVENTS = {"UserPromptSubmit": "working", "PostToolUse": "working", "PermissionGranted": "working", "Stop": "done",
          "Notification": None}
ASKING = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}


def ancestors(pid):
    """This process's parents, up to init: the agent that ran the hook is one of them."""
    out = []
    while pid > 1 and len(out) < 30:
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        out.append(pid)
    return out


def state_of(event):
    name = event.get("hook_event_name")
    if name != "Notification":
        return EVENTS.get(name)
    kind = event.get("notification_type") or ""
    message = str(event.get("message") or "").lower()
    if kind in ASKING or "permission" in message:
        return "asking"
    if kind == "idle_prompt" or "waiting for your input" in message:
        return "done"
    return None  # other notices (logins, quotas) don't change what the agent is doing


def record(event, tell=True):
    """Note the agent's state, and return what to tell it about its room (or None). With tell
    off, it can't be told anything at this point, so nothing is marked told."""
    state, name = state_of(event), event.get("hook_event_name")
    if not state and name not in MAIL:
        return None
    parents = set(ancestors(os.getpid()))
    agent = next((a for a in channel.live_agents() if a.get("pid") in parents), None)
    if not agent:  # a session without the Tatami Room channel
        return None
    path = os.path.join(channel.HOME, "status", agent["id"] + ".json")
    before = channel.load(path, {})
    now = dict(before, state=state, ts=time.time()) if state and before.get("state") != state else dict(before)
    out = mail(event, agent, now) if tell else None
    if now != before:  # most tool calls change nothing
        os.makedirs(os.path.dirname(path), exist_ok=True)
        channel.save(path, now)
    return out


MAIL = ("PostToolUse", "UserPromptSubmit", "Stop")


def mail(event, agent, status):
    """Room messages that came in since the agent last read the room, if it hasn't been told
    about them yet: what to tell it, in the shape its hook event takes. Marks them told. When
    it's about to finish its turn, unfinished work on the room's plan holds it up too (once)."""
    name, room = event.get("hook_event_name"), agent["room"]
    if name not in MAIL or not room:
        return None
    if name == "Stop" and event.get("stop_hook_active"):
        return None  # never twice in a row
    msgs = channel.load_jsonl(os.path.join(channel.ROOMS, room + ".jsonl"))
    new = [m for m in channel.unread(agent, room, msgs) if m["ts"] > status.get("told", 0)]
    mine = [m for m in new if channel.said_to(m, agent["id"], channel.born(agent))]
    senders = ", ".join(dict.fromkeys(m.get("from", "?") for m in new))
    if name == "Stop":
        # A message to this agent holds up the end of its turn; so does work it hasn't finished.
        if mine:
            status.update(told=new[-1]["ts"], state="working", ts=time.time())  # it isn't done after all
            many = len(mine) > 1
            return {"decision": "block",
                    "reason": f"Tatami Room: {len(mine)} message{'s' if many else ''} to you from {senders} "
                              f"came in while you worked. Call room_read and answer in the room before you "
                              f"finish."}
        reason = unfinished(agent, room, status)
        if reason:
            status.update(state="working", ts=time.time())
            return {"decision": "block", "reason": reason}
        return None
    if not new:
        return None
    status["told"] = new[-1]["ts"]
    text = (f"Tatami Room: {len(new)} new message{'s' if len(new) > 1 else ''} in room '{room}' from "
            f"{senders}" + (f", {len(mine)} addressed to you" if mine else "")
            + ". Call room_read when you reach a good point.")
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}


def unfinished(agent, room, status):
    """What's left on the room's plan that should hold up the end of an agent's turn, once for
    each state of it: its own task still in progress, or, for whoever lands the room's work,
    finished work waiting to land. Marks it said."""
    items = work.tasks(room)
    lead = channel.lead_of(room)
    doing = [t for t in items if t.get("owner") == agent["id"] and t["status"] == "doing"]
    lands = work.to_land(items, agent["id"], lead)
    said = ",".join(f"{t['id']}@{t['updated']}" for t in doing + lands)
    if not said or said == status.get("gated"):
        return None
    status["gated"] = said
    if doing:
        names = ", ".join(f"{t['id']} ({t['title']})" for t in doing)
        return (f"Tatami Room: your task {names} is still in progress on the room's plan. Before you finish: "
                "if it's done, commit, then room_task done with what changed and how you checked it; if you're "
                "stuck or waiting for the user, room_task update with status blocked and why. If you're only "
                "pausing, you can finish now.")
    names = ", ".join(f"{t['id']} ({t['owner']}: {t['title']}; {work.how_to_land(t, agent['id'])})" for t in lands)
    return (f"Tatami Room: finished work is waiting to land in the user's checkout: {names}. Check it and land "
            f"it, or hand it back with room_task update (status doing, and a note on what to fix).")


# Antigravity's events, as the Claude Code events they match.
AGY_HOOKS = os.path.expanduser("~/.gemini/config/hooks.json")
AGY_EVENTS = {"PreInvocation": "UserPromptSubmit", "PostToolUse": "PostToolUse", "Stop": "Stop"}
AGY_NAME = "tatami-room"  # our entry in its hooks.json, beside any others
# Its tools that change a file (named as TargetFile, or target_file), and the one that runs
# commands (CommandLine, in the folder Cwd): what its PreToolUse hook asks the guard about.
AGY_EDITS = ("write_to_file", "edit_file", "replace_file_content", "multi_replace_file_content")
AGY_GUARDED = "|".join(AGY_EDITS + ("run_command",))  # the hook's matcher: a regex of tool names


def agy_guard(call):
    """Antigravity's PreToolUse: the refusal the mod would give Claude for the same edit or
    command (mod.guard), as a "deny" Gemini is shown with its reason. {} otherwise, which leaves
    the tool to ask you or not as it always does: never "allow", which would skip your OK."""
    import mod  # same folder; it imports this module, so not at the top
    name, args = call.get("name"), call.get("args") or {}
    if name == "run_command":
        cmd, cwd = str(args.get("CommandLine") or ""), args.get("Cwd")
        ask = {"tool": "Bash", "command": f"cd {shlex.quote(str(cwd))} && {cmd}" if cwd else cmd}
    elif name in AGY_EDITS and (args.get("TargetFile") or args.get("target_file")):
        ask = {"tool": "Edit", "path": str(args.get("TargetFile") or args.get("target_file"))}
    else:
        return {}
    why = mod.guard(ask).get("deny")
    return {"decision": "deny", "reason": why} if why else {}


def agy(name, payload=None):
    """An Antigravity hook: note its state, and answer in its shape (it always wants a JSON
    object). Room news goes in before the model's next call, and a message to it holds up its
    stop the way it holds up Claude's. After a tool it can't take a note, so the next call does.
    Before an edit or a command, it's kept off its teammates' work the way Claude is."""
    if name == "PreToolUse":
        return agy_guard((payload or {}).get("toolCall") or {})
    event = {"hook_event_name": AGY_EVENTS.get(name)}
    out = record(event, tell=name != "PostToolUse") if event["hook_event_name"] else None
    if out and name == "Stop":
        return {"decision": "continue", "reason": out["reason"]}
    if out and name == "PreInvocation":
        return {"injectSteps": [{"ephemeralMessage": out["hookSpecificOutput"]["additionalContext"]}]}
    return {}


def install_agy(on):
    """Our entry in Antigravity's hooks.json, when it's installed: added, or taken out."""
    if not os.path.isdir(os.path.dirname(AGY_HOOKS)):
        return False
    hooks = channel.load(AGY_HOOKS, {})
    if not isinstance(hooks, dict):
        return False
    hooks.pop(AGY_NAME, None)
    if on:
        run = lambda event: {"type": "command", "command": f"{COMMAND} agy {event}", "timeout": 10}
        hooks[AGY_NAME] = {"PreToolUse": [{"matcher": AGY_GUARDED, "hooks": [dict(run("PreToolUse"), timeout=15)]}],
                           "PreInvocation": [run("PreInvocation")],
                           "PostToolUse": [{"matcher": "*", "hooks": [run("PostToolUse")]}],
                           "Stop": [run("Stop")]}
    channel.save(AGY_HOOKS, hooks)
    return True


# Codex's hooks (~/.codex/hooks.json) take Claude Code's shape. Its PreToolUse names a shell
# command "Bash" (tool_input.command) and a file edit "apply_patch", whose tool_input.command is
# the patch: each file it touches is on a "*** Add/Update/Delete File:" or "*** Move to:" line,
# relative to the event's cwd.
CODEX_HOOKS = os.path.expanduser("~/.codex/hooks.json")
CODEX_COMMAND = f"{COMMAND} codex"
PATCHED = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M)


def codex_guard(event):
    """Codex's PreToolUse: the refusal the mod would give Claude for the same command or edit,
    as Codex's deny, which it shows the model with the reason. None to let it go ahead."""
    import mod  # same folder; it imports this module, so not at the top
    name, given, cwd = event.get("tool_name"), event.get("tool_input"), str(event.get("cwd") or "")
    text = given.get("command") if isinstance(given, dict) else None
    text = shlex.join(map(str, text)) if isinstance(text, list) else str(text or "")
    if name == "Bash":
        asks = [{"tool": "Bash", "command": f"cd {shlex.quote(cwd)} && {text}" if cwd else text}]
    elif name == "apply_patch":
        asks = [{"tool": "Edit", "path": os.path.join(cwd, p)} for p in PATCHED.findall(text)]
    else:
        return None
    why = next((w for w in (mod.guard(a).get("deny") for a in asks) if w), None)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}} if why else None


def install_codex(on):
    """Our PreToolUse hook in Codex's hooks.json, when Codex is installed: added, or taken out,
    leaving everyone else's hooks as they were."""
    if not os.path.isdir(os.path.dirname(CODEX_HOOKS)):
        return False
    conf = channel.load(CODEX_HOOKS, {})
    if not isinstance(conf, dict) or not isinstance(conf.get("hooks", {}), dict):
        return False
    events = conf.setdefault("hooks", {})
    mine = lambda group: any(h.get("command", "").startswith(CODEX_COMMAND) for h in group.get("hooks", []))
    events["PreToolUse"] = [g for g in events.get("PreToolUse", []) if not mine(g)]
    if on:
        events["PreToolUse"].append({"matcher": "Bash|apply_patch", "hooks": [
            {"type": "command", "command": CODEX_COMMAND, "timeout": 15, "statusMessage": "Tatami Room"}]})
    if not events["PreToolUse"]:
        events.pop("PreToolUse")
    channel.save(CODEX_HOOKS, conf)
    return True


def ours(hook):
    return "tatami" in hook.get("command", "") and hook.get("command", "").rstrip().endswith(" hook")


def remove_settings_hooks():
    """Take the hooks earlier versions added to Claude Code's settings out again, leaving every
    other setting and hook as it was. Returns whether there were any."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except (FileNotFoundError, ValueError):
        return False  # none, or not plain JSON: not ours to touch
    hooks = settings.get("hooks") or {}
    found = False
    for event in list(hooks):
        groups = []
        for group in hooks.get(event, []):
            kept = [h for h in group.get("hooks", []) if not ours(h)]
            found = found or len(kept) != len(group.get("hooks", []))
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not found:
        return False
    if not hooks:
        settings.pop("hooks", None)
    with open(SETTINGS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(SETTINGS + ".tmp", SETTINGS)
    return True


def install(on):
    """tatami hooks on|off: the mod for Claude (on only), Gemini's hooks, and the old settings
    hooks out either way."""
    removed = remove_settings_hooks()
    gemini, codex = install_agy(on), install_codex(on)
    old = " (and took the old Tatami hooks out of Claude Code's settings)" if removed else ""
    others = " and ".join(n for n, did in (("Gemini's", gemini), ("Codex's", codex)) if did)
    if on:
        import mod  # same folder; it imports this module, so not at the top
        mod.switch(True)
        print("Claude's status reaches the desk through the Tatami Room mod" + old + "."
              + (f" {others} hooks are on too, from their next start." if others else ""))
    else:
        print((f"{others} Tatami Room hooks are off." if others else "There are no Gemini or Codex hooks here.")
              + old + " Claude's status comes from the mod: tatami mod off turns that off.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "event":
        try:
            out = record(json.load(sys.stdin))
        except Exception:  # never get in Claude's way
            out = None
        if out:
            print(json.dumps(out))
        return
    if cmd == "agy":  # Antigravity's: `tatami hook agy <event>`, the event's details on stdin
        try:
            text = sys.stdin.read()
            out = agy(sys.argv[2] if len(sys.argv) > 2 else "", json.loads(text) if text.strip() else {})
        except Exception:  # never get in Gemini's way either
            out = {}
        print(json.dumps(out))
        return
    if cmd == "codex" and sys.argv[2:3] not in (["on"], ["off"]):  # Codex's: `tatami hook codex`
        try:
            event = json.load(sys.stdin)
            out = codex_guard(event) if event.get("hook_event_name") == "PreToolUse" else None
        except Exception:  # never get in Codex's way
            out = None
        if out:
            print(json.dumps(out))
        return
    if cmd == "codex":
        on = sys.argv[2] == "on"
        if not install_codex(on):
            sys.exit("Codex isn't set up here: there's no ~/.codex (or its hooks.json isn't one Tatami can add to).")
        print("Codex's Tatami Room hook is on: from its next start, on a team it works in a worktree of its own and "
              "leaves its teammates' files alone." if on else "Codex's Tatami Room hook is off.")
        return
    if cmd in ("on", "off"):
        install(cmd == "on")
        return
    if cmd == "gemini" and sys.argv[2:3] in (["on"], ["off"]):
        on = sys.argv[2] == "on"
        if not install_agy(on):
            sys.exit("Antigravity (agy) isn't set up here: there's no ~/.gemini/config.")
        print("Gemini's Tatami Room hooks are on: its card shows when it's working and when it's your turn "
              "(from its next start), and it hears about room messages." if on else "Gemini's Tatami Room hooks are off.")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
