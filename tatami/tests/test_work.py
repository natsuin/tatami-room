"""Tests for the room's work (work.py): tasks, claims, worktrees and landing, on throwaway git
repositories with a scratch state folder.

  python3 -m unittest discover -s tatami/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRATCH = tempfile.mkdtemp(prefix="tatami-test-")
os.environ["TATAMI_HOME"] = os.path.join(SCRATCH, "state")
os.environ["TATAMI_WORKTREES"] = os.path.join(SCRATCH, "trees")
os.environ["TATAMI_DESK_DIR"] = os.path.join(SCRATCH, "desk")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402
import hooks  # noqa: E402
import mod  # noqa: E402
import tatami_mcp as channel  # noqa: E402
import work  # noqa: E402


def contents(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Who:
    """What the tools take as the agent calling them."""
    def __init__(self, aid):
        self.id = aid


class Team(unittest.TestCase):
    """A scratch state folder and a one-commit repository, for each test."""

    def setUp(self):
        for d in ("state", "trees"):
            subprocess.run(["rm", "-rf", os.path.join(SCRATCH, d)], check=True)
        for d in (channel.AGENTS, channel.ROOMS, os.path.join(channel.HOME, "status")):
            os.makedirs(d)
        self.repo = os.path.join(SCRATCH, "proj")
        subprocess.run(["rm", "-rf", self.repo], check=True)
        os.makedirs(self.repo)
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "config", "user.email", "t@example.com", cwd=self.repo)
        sh("git", "config", "user.name", "Test", cwd=self.repo)
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("one\ntwo\nthree\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("local.cfg\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "start", cwd=self.repo)
        self.agents = {}

    def agent(self, aid, room=None, born=None):
        """A live agent record (this test's own process stands in for it). `born` is when it
        took its id; without one it's an agent from before agents noted that."""
        rec = {"id": aid, "agent": "claude", "cwd": SCRATCH, "room": room, "pid": os.getpid(),
               "started": channel.proc_start(os.getpid()), "seen": time.time(), "read_upto": {}}
        if born:
            rec["born"] = born
        channel.save(os.path.join(channel.AGENTS, aid + ".json"), rec)
        members = channel.load(channel.MEMBERS, {})
        members[aid] = room
        channel.save(channel.MEMBERS, members)
        return self.me(aid)

    def me(self, aid):
        return next(a for a in channel.live_agents() if a["id"] == aid)

    def lead(self, room, aid):
        channel.save(channel.LEADS_FILE, {room: aid})

    def msgs(self, room):
        return channel.load_jsonl(os.path.join(channel.ROOMS, room + ".jsonl"))

    def commit(self, path, text, msg="change"):
        with open(path, "w") as f:
            f.write(text)
        cwd = os.path.dirname(path)
        sh("git", "add", os.path.basename(path), cwd=cwd)
        sh("git", "commit", "-q", "-m", msg, cwd=cwd)


class Room(Team):
    # ---- the guard ----

    def test_alone_edits_the_checkout(self):
        solo = self.agent("solo")
        self.assertIsNone(work.guard(solo, os.path.join(self.repo, "a.txt")))
        self.assertEqual(work.trees(), {})

    def test_teammates_get_worktrees(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        said = work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)
        self.assertIsNotNone(tree)
        self.assertIn(tree["path"], said)
        self.assertIn(os.path.join(tree["path"], "a.txt"), said)
        self.assertEqual(tree["branch"], "tatami/rose")
        self.assertEqual(tree["base"], "main")
        self.assertTrue(os.path.isfile(os.path.join(tree["path"], "a.txt")))
        # in its own worktree it may edit, and the file is claimed for it
        self.assertIsNone(work.guard(rose, os.path.join(tree["path"], "a.txt")))
        self.assertEqual([c["path"] for c in work.claims()], [os.path.join(self.repo, "a.txt")])
        # the checkout stays the user's, even once it's on its own again
        self.assertIn("is the user's checkout", work.guard(rose, os.path.join(self.repo, "new.txt")))

    def test_a_worktree_catches_up(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.commit(os.path.join(self.repo, "new.txt"), "n\n", "the user's")  # the user's branch moves on
        said = work.guard(rose, os.path.join(self.repo, "new.txt"))
        self.assertNotIn("behind", said)  # nothing of its own yet: brought up to date
        self.assertTrue(os.path.exists(os.path.join(tree, "new.txt")))
        self.commit(os.path.join(tree, "mine.txt"), "m\n", "rose's")
        self.commit(os.path.join(self.repo, "later.txt"), "l\n", "the user's again")
        said = work.guard(rose, os.path.join(self.repo, "later.txt"))
        self.assertIn("1 commit behind main", said)  # work of its own: it rebases itself
        self.assertIn(f"git -C {tree} rebase main", said)

    def test_ignored_files_stay_in_the_checkout(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        self.assertIsNone(work.guard(rose, os.path.join(self.repo, "local.cfg")))

    def test_a_teammates_file_is_refused(self):
        sky = self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(sky, os.path.join(self.repo, "a.txt"))
        sky_tree = work.tree_of("sky", self.repo)["path"]
        self.assertIsNone(work.guard(sky, os.path.join(sky_tree, "a.txt")))
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        rose_tree = work.tree_of("rose", self.repo)["path"]
        said = work.guard(rose, os.path.join(rose_tree, "a.txt"))
        self.assertIn("isn't yours to edit", said)
        self.assertIn("sky", said)
        # nor may it edit in sky's worktree
        self.assertIn("is sky's worktree", work.guard(rose, os.path.join(sky_tree, "b.txt")))
        # once sky lets go, it can
        with channel.locked():
            work.release("sky")
        self.assertIsNone(work.guard(rose, os.path.join(rose_tree, "a.txt")))

    def test_claims_lapse_when_the_holder_leaves_the_room(self):
        sky = self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        outside = os.path.join(SCRATCH, "notes.md")
        self.assertIsNone(work.guard(sky, outside))
        self.assertEqual(work.claims(), [])  # a file outside any repository isn't claimed as it's edited
        with channel.locked():
            work.claim("sky", "fuji", [outside])  # but it can be claimed on purpose
        self.assertIn("isn't yours", work.guard(rose, outside))
        self.agent("sky", "ume")  # dragged to another room
        self.agent("pine", "fuji")  # rose still has a teammate
        self.assertIsNone(work.guard(rose, outside))

    def test_a_closed_agents_work_isnt_the_next_ones(self):
        self.agent("lead", "fuji")
        rose = self.agent("rose", "fuji")
        self.lead("fuji", "lead")
        f = os.path.join(self.repo, "a.txt")
        work.answer(Who("lead"), "room_task", {"action": "add", "title": "Fix a", "owner": "rose", "files": [f]}, "fuji")
        work.answer(Who("lead"), "room_task", {"action": "add", "title": "Fix b", "owner": "rose"}, "fuji")
        work.answer(Who("rose"), "room_task", {"action": "take", "id": "t1"}, "fuji")
        work.guard(rose, f)
        self.commit(os.path.join(work.tree_of("rose", self.repo)["path"], "a.txt"), "A\n")
        work.answer(Who("rose"), "room_task", {"action": "done", "id": "t1", "note": "done"}, "fuji")
        work.answer(Who("rose"), "room_task", {"action": "take", "id": "t2"}, "fuji")
        with channel.locked():
            work.retire("rose")  # rose's window closed
        self.assertIsNone(work.tree_of("rose", self.repo))  # a new rose starts without it
        self.assertEqual(work.claims(), [])
        t1, t2 = work.find(work.tasks("fuji"), "t1"), work.find(work.tasks("fuji"), "t2")
        self.assertEqual((t2["owner"], t2["status"]), (None, "open"))
        self.assertTrue(t1["owner"].startswith("rose."))
        text, err = work.answer(Who("lead"), "room_land", {"agent": t1["owner"]}, "fuji")  # its finished work still lands
        self.assertFalse(err, text)
        self.assertEqual(contents(f), "A\n")
        self.assertEqual(work.find(work.tasks("fuji"), "t1")["status"], "landed")

    def test_bash_git_in_the_checkout(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.assertIn("user's checkout", work.guard_bash(rose, f"cd {self.repo} && git commit -am x"))
        self.assertIn("user's checkout", work.guard_bash(rose, f"git -C {self.repo} add a.txt"))
        self.assertIsNone(work.guard_bash(rose, f"git -C {self.repo} log --oneline"))
        self.assertIsNone(work.guard_bash(rose, f"git -C {tree} commit -am x"))

    # ---- the plan ----

    def test_orchestrator_hands_out_work(self):
        self.agent("lead", "fuji")
        self.agent("sky", "fuji")
        self.agent("rose", "fuji")
        self.lead("fuji", "lead")
        f = os.path.join(self.repo, "a.txt")
        text, err = work.answer(Who("sky"), "room_task", {"action": "add", "title": "x", "owner": "rose"}, "fuji")
        self.assertTrue(err)
        self.assertIn("orchestrator", text)
        text, err = work.answer(Who("lead"), "room_task", {"action": "add", "title": "Fix a", "owner": "rose",
                                                           "done_when": "tests pass", "files": [f]}, "fuji")
        self.assertFalse(err, text)
        last = self.msgs("fuji")[-1]
        self.assertEqual((last["from"], last["to"]), ("lead", "rose"))
        self.assertIn("t1", last["text"])
        # the task's files are rose's now
        sky_tree = (work.guard(self.me("sky"), f), work.tree_of("sky", self.repo)["path"])[1]
        self.assertIn("rose's task t1", work.guard(self.me("sky"), os.path.join(sky_tree, "a.txt")))
        # sky can't take rose's task, rose can
        self.assertTrue(work.answer(Who("sky"), "room_task", {"action": "take", "id": "t1"}, "fuji")[1])
        self.assertFalse(work.answer(Who("rose"), "room_task", {"action": "take", "id": "t1"}, "fuji")[1])
        self.assertEqual(work.find(work.tasks("fuji"), "t1")["status"], "doing")
        self.assertIn("t1 [doing] Fix a", work.plan("fuji"))

    def test_done_then_land_by_the_orchestrator(self):
        self.agent("lead", "fuji")
        rose = self.agent("rose", "fuji")
        self.lead("fuji", "lead")
        f = os.path.join(self.repo, "a.txt")
        work.answer(Who("lead"), "room_task", {"action": "add", "title": "Fix a", "owner": "rose", "files": [f]}, "fuji")
        work.answer(Who("rose"), "room_task", {"action": "take", "id": "t1"}, "fuji")
        work.guard(rose, f)
        tree = work.tree_of("rose", self.repo)["path"]
        with open(os.path.join(tree, "a.txt"), "w") as fh:
            fh.write("one\nTWO\nthree\n")
        text, err = work.answer(Who("rose"), "room_task", {"action": "done", "id": "t1", "note": "changed two"}, "fuji")
        self.assertTrue(err)
        self.assertIn("haven't committed", text)
        sh("git", "commit", "-qam", "two", cwd=tree)
        self.assertTrue(work.answer(Who("rose"), "room_task", {"action": "done", "id": "t1"}, "fuji")[1])  # no note
        text, err = work.answer(Who("rose"), "room_task", {"action": "done", "id": "t1", "note": "changed two"}, "fuji")
        self.assertFalse(err, text)
        self.assertEqual((self.msgs("fuji")[-1]["to"]), "lead")
        # rose doesn't land its own work while there's an orchestrator
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertTrue(err)
        # the user committed something else meanwhile: landing rebases onto it
        self.commit(os.path.join(self.repo, "b.txt"), "b\n", "user's own")
        text, err = work.answer(Who("lead"), "room_land", {"agent": "rose"}, "fuji")
        self.assertFalse(err, text)
        self.assertEqual(contents(f), "one\nTWO\nthree\n")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")))
        self.assertEqual(sh("git", "log", "--format=%s", "-3", cwd=self.repo).split("\n"), ["two", "user's own", "start"])
        self.assertEqual(work.find(work.tasks("fuji"), "t1")["status"], "landed")
        self.assertEqual(work.claims(), [])

    def test_peers_land_their_own(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.commit(os.path.join(tree, "c.txt"), "c\n")
        self.assertTrue(work.answer(Who("sky"), "room_land", {"agent": "rose"}, "fuji")[1])
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertFalse(err, text)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "c.txt")))

    def test_landing_never_overwrites_the_users_changes(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.commit(os.path.join(tree, "a.txt"), "one\nROSE\nthree\n")
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:  # the user, typing
            fh.write("one\nmine\nthree\n")
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertTrue(err)
        self.assertIn("Nothing was overwritten", text)
        self.assertEqual(contents(os.path.join(self.repo, "a.txt")), "one\nmine\nthree\n")

    def test_a_conflict_stays_in_the_worktree(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.commit(os.path.join(tree, "a.txt"), "one\nROSE\nthree\n")
        self.commit(os.path.join(self.repo, "a.txt"), "one\nUSER\nthree\n")
        before = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertTrue(err)
        self.assertIn("a.txt", text)
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.repo), before)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=tree), "")  # the rebase was undone

    # ---- the check ----

    def check(self, conf):
        """The user names a check for this repository (.tatami.json in their checkout)."""
        with open(os.path.join(self.repo, ".tatami.json"), "w") as f:
            f.write(conf if isinstance(conf, str) else json.dumps(conf))

    def test_the_check_runs_on_the_rebased_work(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        tree = work.tree_of("rose", self.repo)["path"]
        self.commit(os.path.join(tree, "c.txt"), "bad\n")
        self.commit(os.path.join(self.repo, "b.txt"), "b\n", "user's own")  # after rose's worktree was made
        self.check({"check": "cat b.txt c.txt && grep -q good c.txt"})
        before = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertTrue(err)
        self.assertIn("rebased cleanly onto main, but the check failed on it there, so it didn't land", text)
        self.assertIn("exited 1. The end of what it said:\nb\nbad\n", text)  # rose's work, on top of the user's
        self.assertIn(f"Fix it in {tree}, commit, then land again.", text)
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.repo), before)
        self.commit(os.path.join(tree, "c.txt"), "good\n")
        text, err = work.answer(Who("rose"), "room_land", {}, "fuji")
        self.assertFalse(err, text)
        self.assertIn(", and `cat b.txt c.txt && grep -q good c.txt` passed on it first.", text)
        self.assertEqual(contents(os.path.join(self.repo, "c.txt")), "good\n")

    def test_a_check_that_hangs_or_is_broken(self):
        self.agent("lead", "fuji")
        rose = self.agent("rose", "fuji")
        self.lead("fuji", "lead")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        self.commit(os.path.join(work.tree_of("rose", self.repo)["path"], "c.txt"), "c\n")
        self.check({"check": "sleep 30 & sleep 30", "timeout": 0.5})
        began = time.time()
        text, err = work.answer(Who("lead"), "room_land", {"agent": "rose"}, "fuji")
        self.assertTrue(err)
        self.assertLess(time.time() - began, 5)  # the sleep it left running went too
        self.assertIn("`sleep 30 & sleep 30` was still running after 0.5s. Nothing changed", text)
        self.assertIn("Hand it back to rose with room_task update", text)
        self.check('{"check": "make test",}')
        text, err = work.answer(Who("lead"), "room_land", {"agent": "rose"}, "fuji")
        self.assertTrue(err)
        self.assertIn(".tatami.json isn't what landing expects", text)  # not taken for "no check"
        self.assertFalse(os.path.exists(os.path.join(self.repo, "c.txt")))

    def test_helpers_take_their_task(self):
        self.agent("sky", "fuji")
        with channel.locked():
            tid = work.invite_task("fuji", "sky", "ab" * 16, "Port the glow. Then tidy up.")
        self.assertIn("a helper that's starting", work.plan("fuji"))
        self.agent("helper", "fuji")
        with channel.locked():
            self.assertEqual(work.adopt("helper", "fuji", "ab" * 16), tid)
        t = work.find(work.tasks("fuji"), tid)
        self.assertEqual((t["owner"], t["status"], t["title"]), ("helper", "doing", "Port the glow."))

    def test_closed_agents_leave_only_waiting_work(self):
        self.agent("sky", "fuji")
        rose = self.agent("rose", "fuji")
        pine = self.agent("pine", "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        work.guard(pine, os.path.join(self.repo, "a.txt"))
        self.commit(os.path.join(work.tree_of("pine", self.repo)["path"], "p.txt"), "p\n")
        work.tidy({"sky"})
        self.assertIsNone(work.tree_of("rose", self.repo))  # nothing in it: gone, branch too
        self.assertNotIn("tatami/rose", sh("git", "branch", cwd=self.repo))
        self.assertIsNotNone(work.tree_of("pine", self.repo))  # a commit to land: kept


class Channel(Team):
    """The channel itself, as Claude Code runs it: the new tools are listed and answer."""

    def test_over_stdio(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.agent("sky", "fuji")
        env = dict(os.environ, TATAMI_ID="rose", TATAMI_ROOM="fuji")
        env.pop("WSL_INTEROP", None)  # no window to look for
        calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "room_task", "arguments": {"action": "add", "title": "Fix a", "owner": "me"}}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "room_members"}}]
        out = subprocess.run([sys.executable, os.path.join(here, "tatami_mcp.py")], env=env, cwd=SCRATCH,
                             input="".join(json.dumps(c) + "\n" for c in calls), capture_output=True, text=True,
                             timeout=30)
        replies = {r["id"]: r for r in map(json.loads, out.stdout.splitlines())}
        self.assertIn("room_task", replies[1]["result"]["instructions"])
        self.assertEqual({t["name"] for t in replies[2]["result"]["tools"]},
                         {"room_post", "room_read", "room_members", "room_invite", "room_task", "room_claim", "room_land"})
        self.assertIn("Added t1", replies[3]["result"]["content"][0]["text"])
        self.assertIn("t1 [doing] Fix a (owner: rose", replies[4]["result"]["content"][0]["text"])


class ModGuard(Team):
    """`tatami mod guard`, as the mod runs it from inside Claude: it finds its agent by process."""

    def test_guard_command(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        other = subprocess.Popen(["sleep", "60"])  # a teammate's process
        self.addCleanup(lambda: (other.kill(), other.wait()))
        channel.save(os.path.join(channel.AGENTS, "sky.json"),
                     {"id": "sky", "agent": "claude", "cwd": SCRATCH, "room": "fuji", "pid": other.pid,
                      "started": channel.proc_start(other.pid), "seen": time.time(), "read_upto": {}})
        self.agent("rose", "fuji")
        channel.save(channel.MEMBERS, {"sky": "fuji", "rose": "fuji"})

        def ask(q):
            out = subprocess.run([sys.executable, os.path.join(here, "mod.py"), "guard"], input=json.dumps(q),
                                 capture_output=True, text=True, timeout=30)
            return json.loads(out.stdout)
        said = ask({"tool": "Edit", "path": os.path.join(self.repo, "a.txt")})
        self.assertIn("worktree of your own", said["deny"])
        tree = work.tree_of("rose", self.repo)["path"]
        self.assertEqual(ask({"tool": "Edit", "path": os.path.join(tree, "a.txt")}), {})
        self.assertIn("user's checkout", ask({"tool": "Bash", "command": f"git -C {self.repo} commit -am x"})["deny"])
        poll = json.loads(subprocess.run([sys.executable, os.path.join(here, "mod.py"), "poll"],
                                         capture_output=True, text=True, timeout=30).stdout)
        self.assertTrue(poll["guarded"])
        self.assertEqual(poll["worktrees"][0]["path"], tree)

    def test_gemini_is_kept_apart_too(self):
        """Antigravity runs `tatami hook agy PreToolUse` before a tool, with the call on stdin
        (its built-in hook docs: toolCall {name, args}; it wants a decision back)."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        other = subprocess.Popen(["sleep", "60"])  # a teammate's process
        self.addCleanup(lambda: (other.kill(), other.wait()))
        channel.save(os.path.join(channel.AGENTS, "sky.json"),
                     {"id": "sky", "agent": "claude", "cwd": SCRATCH, "room": "fuji", "pid": other.pid,
                      "started": channel.proc_start(other.pid), "seen": time.time(), "read_upto": {}})
        self.agent("rose", "fuji")
        channel.save(channel.MEMBERS, {"sky": "fuji", "rose": "fuji"})

        def hook(name, args, raw=None):
            out = subprocess.run([sys.executable, os.path.join(here, "hooks.py"), "agy", "PreToolUse"],
                                 input=raw if raw is not None else json.dumps({"toolCall": {"name": name, "args": args},
                                                                               "stepIdx": 3}),
                                 capture_output=True, text=True, timeout=30)
            return json.loads(out.stdout)
        a = os.path.join(self.repo, "a.txt")
        said = hook("replace_file_content", {"TargetFile": a, "TargetContent": "two", "ReplacementContent": "TWO"})
        self.assertEqual(said["decision"], "deny")
        self.assertIn("worktree of your own", said["reason"])
        tree = work.tree_of("rose", self.repo)["path"]
        self.assertEqual(hook("write_to_file", {"TargetFile": os.path.join(tree, "a.txt"), "CodeContent": "x"}), {})
        self.assertEqual(hook("edit_file", {"target_file": a})["decision"], "deny")  # its other spelling
        self.assertEqual(hook("view_file", {"AbsolutePath": a}), {})  # reading is fine
        said = hook("run_command", {"CommandLine": "git commit -am x", "Cwd": self.repo})
        self.assertIn("user's checkout", said["reason"])
        self.assertEqual(hook("run_command", {"CommandLine": "git commit -am x", "Cwd": tree}), {})
        self.assertEqual(hook("run_command", {"CommandLine": "ls", "Cwd": self.repo}), {})
        work.claim("sky", "fuji", [os.path.join(self.repo, "b.txt")])
        said = hook("write_to_file", {"TargetFile": os.path.join(tree, "b.txt"), "CodeContent": "x"})
        self.assertIn("isn't yours to edit", said["reason"])  # sky's, in rose's worktree too
        self.assertEqual(hook("write_to_file", {}, raw="not json"), {})  # never in Gemini's way

    def test_codex_is_kept_apart_too(self):
        """Codex runs `tatami hook codex` before a tool, with Claude Code's hook shape on stdin
        (developers.openai.com/codex/hooks): "Bash", or "apply_patch" with the patch."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        other = subprocess.Popen(["sleep", "60"])  # a teammate's process
        self.addCleanup(lambda: (other.kill(), other.wait()))
        channel.save(os.path.join(channel.AGENTS, "sky.json"),
                     {"id": "sky", "agent": "claude", "cwd": SCRATCH, "room": "fuji", "pid": other.pid,
                      "started": channel.proc_start(other.pid), "seen": time.time(), "read_upto": {}})
        self.agent("rose", "fuji")
        channel.save(channel.MEMBERS, {"sky": "fuji", "rose": "fuji"})

        def hook(name, command, cwd, event="PreToolUse"):
            q = {"session_id": "s", "turn_id": "t", "hook_event_name": event, "cwd": cwd, "model": "gpt",
                 "permission_mode": "default", "tool_name": name, "tool_use_id": "c1", "tool_input": {"command": command}}
            out = subprocess.run([sys.executable, os.path.join(here, "hooks.py"), "codex"], input=json.dumps(q),
                                 capture_output=True, text=True, timeout=30).stdout
            said = json.loads(out)["hookSpecificOutput"] if out.strip() else None
            if said:
                self.assertEqual((said["hookEventName"], said["permissionDecision"]), ("PreToolUse", "deny"))
            return said and said["permissionDecisionReason"]
        patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n one\n-two\n+TWO\n*** End Patch\n"
        self.assertIn("worktree of your own", hook("apply_patch", patch, self.repo))
        tree = work.tree_of("rose", self.repo)["path"]
        self.assertIsNone(hook("apply_patch", patch, tree))  # in its own worktree: fine
        self.assertIsNone(hook("apply_patch", patch, self.repo, event="PostToolUse"))
        work.claim("sky", "fuji", [os.path.join(self.repo, "b.txt")])
        two = "*** Begin Patch\n*** Add File: new.txt\n+hi\n*** Update File: b.txt\n@@\n-b\n+B\n*** End Patch\n"
        self.assertIn("isn't yours to edit", hook("apply_patch", two, tree))  # sky's b.txt, the second file
        moved = "*** Begin Patch\n*** Update File: new.txt\n*** Move to: b.txt\n*** End Patch\n"
        self.assertIn("isn't yours to edit", hook("apply_patch", moved, tree))
        self.assertIn("user's checkout", hook("Bash", "git commit -am x", self.repo))
        self.assertIn("user's checkout", hook("Bash", ["bash", "-lc", "git commit -am x"], self.repo))
        self.assertIsNone(hook("Bash", "git commit -am x", tree))
        self.assertIsNone(hook("Bash", "ls", self.repo))
        self.assertIsNone(hook("update_plan", "x", self.repo))

    def test_codex_hook_installs(self):
        path = os.path.join(SCRATCH, "codex", "hooks.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/audit.sh"}]}
        channel.save(path, {"description": "mine", "hooks": {"PreToolUse": [theirs], "Stop": []}})
        old, hooks.CODEX_HOOKS = hooks.CODEX_HOOKS, path
        self.addCleanup(setattr, hooks, "CODEX_HOOKS", old)
        self.assertTrue(hooks.install_codex(True))
        self.assertTrue(hooks.install_codex(True))  # twice is still once
        pre = channel.load(path, {})["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[0], theirs)
        self.assertEqual(pre[1]["matcher"], "Bash|apply_patch")
        self.assertEqual(pre[1]["hooks"][0]["command"], '"$HOME/.local/bin/tatami" hook codex')
        hooks.install_codex(False)
        self.assertEqual(channel.load(path, {}), {"description": "mine", "hooks": {"PreToolUse": [theirs], "Stop": []}})

    def test_gemini_hooks_install(self):
        path = os.path.join(SCRATCH, "gemini", "config", "hooks.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        channel.save(path, {"theirs": {"Stop": []}})
        old, hooks.AGY_HOOKS = hooks.AGY_HOOKS, path
        self.addCleanup(setattr, hooks, "AGY_HOOKS", old)
        self.assertTrue(hooks.install_agy(True))
        ours = channel.load(path, {})["tatami-room"]
        [group] = ours["PreToolUse"]
        self.assertEqual(group["matcher"], hooks.AGY_GUARDED)
        self.assertTrue(group["hooks"][0]["command"].endswith(" hook agy PreToolUse"))
        self.assertEqual(sorted(ours), ["PostToolUse", "PreInvocation", "PreToolUse", "Stop"])
        hooks.install_agy(False)
        self.assertEqual(channel.load(path, {}), {"theirs": {"Stop": []}})


class OnePath(unittest.TestCase):
    """Claude's status comes from the mod: the hooks earlier versions put in settings come out."""

    def test_old_settings_hooks_come_out(self):
        settings = os.path.join(SCRATCH, "settings.json")
        mine = {"type": "command", "command": "python3 ~/my-hook.py"}
        ours = {"type": "command", "command": '"$HOME/.local/bin/tatami" hook'}
        with open(settings, "w") as f:
            json.dump({"model": "opus", "hooks": {"Stop": [{"hooks": [ours, mine]}],
                                                  "PostToolUse": [{"hooks": [ours]}]}}, f)
        was, hooks.SETTINGS = hooks.SETTINGS, settings
        try:
            self.assertTrue(hooks.remove_settings_hooks())
            self.assertFalse(hooks.remove_settings_hooks())  # nothing left of ours
        finally:
            hooks.SETTINGS = was
        with open(settings) as f:
            self.assertEqual(json.load(f), {"model": "opus", "hooks": {"Stop": [{"hooks": [mine]}]}})


class Namesake(Team):
    """Ids are window colours, so a new agent can get the id an earlier agent in its room had.
    What was said by or to that one isn't the new one's, and nobody should take it for it."""

    def setUp(self):
        super().setUp()
        self.agent("wine", "fuji")
        then = time.time() - 600  # the earlier rouge, gone now
        channel.post("fuji", {"ts": then, "from": "rouge", "text": "I'll take the header."})
        channel.post("fuji", {"ts": then + 1, "from": "wine", "to": "rouge", "text": "Thanks, t1 is yours."})

    def test_not_its_own_and_no_wake(self):
        rouge = self.agent("rouge", "fuji", born=time.time())
        msgs = self.msgs("fuji")
        self.assertEqual(len(channel.unread(rouge, "fuji", msgs)), 2)  # the earlier one's message is news to it
        self.assertIsNone(mod.wake(rouge, "fuji", msgs))  # wine wrote to the earlier rouge, not this one
        self.assertIsNone(StopGate.stop(self, "rouge"))
        channel.post("fuji", {"ts": time.time(), "from": "wine", "to": "rouge", "text": "Welcome, rouge."})
        self.assertIn("wine sent you a message", mod.wake(rouge, "fuji", self.msgs("fuji")))
        os.remove(mod.status_path(rouge))  # waking it told it; the Stop gate would hold it up as well
        self.assertEqual(StopGate.stop(self, "rouge")["decision"], "block")

    def test_room_read_says_so(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, TATAMI_ID="rouge", TATAMI_ROOM="fuji")
        env.pop("WSL_INTEROP", None)  # no window to look for
        calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "room_read"}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "room_members"}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "room_post", "arguments": {"text": "Hi, I'm new here."}}}]
        out = subprocess.run([sys.executable, os.path.join(here, "tatami_mcp.py")], env=env, cwd=SCRATCH,
                             input="".join(json.dumps(c) + "\n" for c in calls), capture_output=True, text=True,
                             timeout=30)
        said = {r["id"]: r["result"]["content"][0]["text"] for r in map(json.loads, out.stdout.splitlines()) if r["id"] > 1}
        self.assertIn("Another agent was called rouge before you", said[2])
        self.assertIn("rouge (an earlier rouge, not you): I'll take the header.", said[2])
        self.assertIn("wine -> rouge (an earlier rouge, not you): Thanks", said[2])
        self.assertIn("you took the name at", said[3])
        self.assertIn("wine hasn't read the room yet", said[4])  # not "your last 2 messages": one was the earlier rouge's

        # wine sees the same: the rouge here now isn't the one it gave t1 to
        births = {a["id"]: channel.born(a) for a in channel.live_agents()}
        self.assertEqual(channel.fmt(self.msgs("fuji")[1], births, "wine")[8:],
                         "wine -> rouge (an earlier rouge, not the one here now): Thanks, t1 is yours.")
        self.assertTrue(channel.fmt(self.msgs("fuji")[2], births, "wine").endswith("rouge: Hi, I'm new here."))

    def test_desk_and_pane_mark_it(self):
        self.agent("rouge", "fuji", born=time.time())
        recent = next(r for r in board.state()["rooms"] if r["name"] == "fuji")["recent"]
        self.assertEqual([(m.get("earlier", False), m.get("to_earlier", False)) for m in recent],
                         [(True, False), (False, True)])
        channel.post("fuji", {"ts": time.time(), "from": channel.USER, "via": "desk", "text": "Hello."})
        recent = next(r for r in board.state()["rooms"] if r["name"] == "fuji")["recent"]
        self.assertEqual([m["via"] for m in recent], [None, None, "desk"])  # where the user said it
        was, mod.me = mod.me, lambda: self.me("rouge")
        try:
            shown = mod.poll(False)["messages"]
        finally:
            mod.me = was
        self.assertEqual([(m["from"], m["to"], m["mine"]) for m in shown[:2]],
                         [("rouge (earlier)", None, False), ("wine", "rouge (earlier)", False)])

    def test_pane_colours(self):
        """The /room pane colours each name in a light shade of its window's colour and stars the
        orchestrator; the earlier rouge gets no colour."""
        self.agent("rouge", "fuji", born=time.time() - 60)
        self.lead("fuji", "wine")
        channel.post("fuji", {"ts": time.time(), "from": "wine", "to": "rouge", "text": "Welcome, rouge."})
        channel.post("fuji", {"ts": time.time(), "from": channel.USER, "via": "rouge", "text": "Hello."})
        was, mod.me = mod.me, lambda: self.me("rouge")
        try:
            snap = mod.poll(False)
        finally:
            mod.me = was
        rouge, wine = mod.ink("rouge"), mod.ink("wine")
        self.assertEqual((wine, mod.ink("cherry"), rouge), ("#f66fa4", "#f1aacb", "#edbdc2"))  # three pinks, apart
        self.assertEqual([(m["from"], m["color"], m["to"], m["toColor"], m["lead"]) for m in snap["messages"]],
                         [("rouge (earlier)", None, None, None, False),
                          ("wine", wine, "rouge (earlier)", None, True),
                          ("wine", wine, "rouge", rouge, True),
                          ("the user", None, None, None, False)])
        self.assertEqual(sorted((m["id"], m["color"], m["lead"]) for m in snap["members"]),
                         [("rouge", rouge, False), ("wine", wine, True)])
        self.assertIsNone(mod.ink("no-such-colour"))

    def test_an_agent_from_before_births(self):
        rouge = self.agent("rouge", "fuji")  # no born: nothing is marked, as before
        self.assertEqual(channel.unread(rouge, "fuji", self.msgs("fuji"))[0]["to"], "rouge")
        self.assertTrue(all("earlier" not in m for r in board.state()["rooms"] for m in r["recent"]))


class StopGate(Team):
    """The status hooks hold up the end of a turn, once, for unfinished business."""

    def stop(self, aid, active=False):
        agent = self.me(aid)
        status = channel.load(os.path.join(channel.HOME, "status", aid + ".json"), {})
        out = hooks.mail({"hook_event_name": "Stop", "stop_hook_active": active}, agent, status)
        channel.save(os.path.join(channel.HOME, "status", aid + ".json"), status)
        return out

    def test_a_task_in_progress(self):
        self.agent("sky", "fuji")
        self.agent("rose", "fuji")
        work.answer(Who("rose"), "room_task", {"action": "add", "title": "Fix a", "owner": "me"}, "fuji")
        out = self.stop("rose")
        self.assertEqual(out["decision"], "block")
        self.assertIn("t1", out["reason"])
        self.assertIsNone(self.stop("rose"))  # once
        work.answer(Who("rose"), "room_task", {"action": "update", "id": "t1", "status": "blocked", "note": "?"}, "fuji")
        self.assertIsNone(self.stop("rose"))

    def test_work_waiting_for_the_orchestrator(self):
        self.agent("lead", "fuji")
        rose = self.agent("rose", "fuji")
        self.lead("fuji", "lead")
        work.answer(Who("lead"), "room_task", {"action": "add", "title": "Fix a", "owner": "rose"}, "fuji")
        work.answer(Who("rose"), "room_task", {"action": "take", "id": "t1"}, "fuji")
        work.guard(rose, os.path.join(self.repo, "a.txt"))
        self.commit(os.path.join(work.tree_of("rose", self.repo)["path"], "a.txt"), "x\n")
        work.answer(Who("rose"), "room_task", {"action": "done", "id": "t1", "note": "x"}, "fuji")
        channel.save(os.path.join(channel.AGENTS, "lead.json"),
                     dict(channel.load(os.path.join(channel.AGENTS, "lead.json"), {}), read_upto={"fuji": time.time()}))
        out = self.stop("lead")
        self.assertEqual(out["decision"], "block")
        self.assertIn("room_land", out["reason"])
        self.assertIsNone(self.stop("lead"))


if __name__ == "__main__":
    unittest.main()
