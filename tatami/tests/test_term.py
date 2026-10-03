"""Tests for the app's terminals (term.py): the agent outlives the app, and the app gets its
screen back when it attaches again; Ctrl+Z doesn't strand it, and ending the terminal ends all of
it. A stand-in `claude` echoes what's typed.

  python3 -m unittest discover -s tatami/tests
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERM = os.path.join(HERE, "term.py")

# Like Claude where it matters: Ctrl+Z suspends it (Claude sends its process group SIGTSTP). And
# this one won't hang up, and leaves something stopped in a process group of its own, as an agent
# wedged in the background would be: what ending the terminal has to reach all the same.
STUBBORN = r'''
import os, signal, sys, time, tty
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
def back(*_):  # as Claude does
    try:
        tty.setraw(0)
        os.write(1, b"back again\r\n")
    except Exception:
        pass
signal.signal(signal.SIGCONT, back)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGCONT, signal.SIG_DFL)
    os.setpgid(0, 0)
    os.kill(0, signal.SIGSTOP)
    while True:
        time.sleep(1)
with open(os.environ["STUBBORN_PIDS"], "w") as f:
    f.write(f"{os.getpid()} {child}")
tty.setraw(0)
os.write(1, f"agent up as {os.environ['TATAMI_SESSION']}\r\n".encode())
while True:
    try:
        c = os.read(0, 1)
    except OSError:
        time.sleep(0.1)
        continue
    if c == b"\x1a":
        os.write(1, b"suspended\r\n")
        os.kill(0, signal.SIGTSTP)
    elif c:
        os.write(1, c.upper())  # not what a shell would echo
'''


def ended(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] in (b"Z", b"X")
    except OSError:
        return True


class Terminal(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp(prefix="tatami-term-")
        home, bin_dir = os.path.join(self.scratch, "home"), os.path.join(self.scratch, "bin")
        os.makedirs(home)
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "claude"), "w") as f:
            f.write('#!/bin/sh\necho "agent up in $(pwd) as $TATAMI_SESSION"\nexec cat\n')
        os.chmod(os.path.join(bin_dir, "claude"), 0o755)
        self.env = dict(os.environ, HOME=home, PATH=bin_dir + os.pathsep + os.environ["PATH"],
                        TATAMI_HOME=os.path.join(self.scratch, "state"),
                        TATAMI_DESK_DIR=os.path.join(self.scratch, "desk"))
        self.addCleanup(self.end_holders)

    def end_holders(self):
        """A test that failed half-way mustn't leave a holder behind."""
        terms = os.path.join(self.scratch, "state", "terms")
        for fn in os.listdir(terms) if os.path.isdir(terms) else []:
            if fn.endswith(".json"):
                with open(os.path.join(terms, fn)) as f:
                    try:
                        os.kill(json.load(f)["pid"], 15)
                    except (OSError, ValueError, KeyError):
                        pass

    def attach(self, session):
        proc = subprocess.Popen([sys.executable, TERM, session, "claude"], env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.stdin.close(), proc.stdout.close(), proc.wait(10)))
        return proc

    def read_until(self, proc, text, timeout=15):
        got, end = b"", time.time() + timeout
        while text.encode() not in got:
            left = end - time.time()
            self.assertGreater(left, 0, f"never saw {text!r} in {got[-300:]!r}")
            if select.select([proc.stdout], [], [], left)[0]:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    self.fail(f"ended before {text!r}: {got[-300:]!r}")
                got += chunk
        return got

    def listed(self):
        out = subprocess.run([sys.executable, TERM, "--list"], env=self.env, capture_output=True, text=True)
        return [json.loads(l) for l in out.stdout.splitlines()]

    def test_without_keep_the_agent_ends_with_the_app(self):
        app = self.attach("app-test2")  # an app from before holders never says keep
        self.read_until(app, "agent up")
        app.stdin.close()
        app.wait(timeout=10)
        for _ in range(50):
            if not self.listed():
                break
            time.sleep(0.1)
        self.assertEqual(self.listed(), [])

    def test_the_agent_outlives_the_app(self):
        app = self.attach("app-test1")
        app.stdin.write(b"\x1b]7373;keep\x07")
        app.stdin.flush()
        first = self.read_until(app, "as app-test1")
        self.assertIn(b"Starting Claude", first)
        self.assertIn(f"agent up in {os.path.join(self.scratch, 'desk')}".encode(), first)
        app.stdin.write(b"hello there\n")
        app.stdin.flush()
        self.read_until(app, "hello there")
        app.stdin.close()  # the app quits
        app.wait(timeout=10)
        self.assertEqual(self.listed(), [{"session": "app-test1", "kind": "claude"}])

        again = self.attach("app-test1")  # the app is back: the screen so far, then live
        again.stdin.write(b"\x1b]7373;keep\x07")
        again.stdin.flush()
        back = self.read_until(again, "hello there")
        self.assertIn(b"agent up", back)
        again.stdin.write(b"\x1b]7373;resize;120;40\x07still here\n")
        again.stdin.flush()
        self.assertNotIn(b"7373", self.read_until(again, "still here"))

        again.stdin.write(b"\x1b]7373;end\x07")  # End in the app
        again.stdin.flush()
        again.wait(timeout=10)
        for _ in range(50):
            if not self.listed():
                break
            time.sleep(0.1)
        self.assertEqual(self.listed(), [])

    def stubborn(self):
        """The `claude` here becomes STUBBORN; its pid and its stopped child's, once it's up."""
        with open(os.path.join(self.scratch, "bin", "claude"), "w") as f:
            f.write(f"#!{sys.executable}\n{STUBBORN}")
        pids = self.env["STUBBORN_PIDS"] = os.path.join(self.scratch, "pids")

        def up():
            with open(pids) as f:
                return [int(p) for p in f.read().split()]

        def kill_them():
            for pid in up() if os.path.exists(pids) else []:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        self.addCleanup(kill_them)
        return up

    def test_ctrl_z_doesnt_strand_the_agent(self):
        up = self.stubborn()
        app = self.attach("app-test3")
        app.stdin.write(b"\x1b]7373;keep\x07")
        app.stdin.flush()
        self.read_until(app, "agent up")
        app.stdin.write(b"\x1a")
        app.stdin.flush()
        self.read_until(app, "suspended")
        self.read_until(app, "back again")  # straight on again, not stopped behind a prompt
        app.stdin.write(b"typing on")
        app.stdin.flush()
        self.read_until(app, "TYPING ON")  # the agent's still the one reading
        self.assertFalse(ended(up()[0]))
        app.stdin.write(b"\x1b]7373;end\x07")
        app.stdin.flush()

    def test_ending_the_terminal_ends_all_of_it(self):
        up = self.stubborn()
        app = self.attach("app-test4")
        app.stdin.write(b"\x1b]7373;keep\x07")
        app.stdin.flush()
        self.read_until(app, "agent up")
        agent, stopped = up()
        self.assertFalse(ended(agent) or ended(stopped))
        app.stdin.write(b"\x1b]7373;end\x07")  # End in the app
        app.stdin.flush()
        app.wait(timeout=10)
        for _ in range(100):
            if ended(agent) and ended(stopped):
                break
            time.sleep(0.1)
        self.assertTrue(ended(agent), "the agent outlived its terminal")
        self.assertTrue(ended(stopped), "what it left stopped outlived its terminal")
        self.assertEqual(self.listed(), [])


if __name__ == "__main__":
    unittest.main()
