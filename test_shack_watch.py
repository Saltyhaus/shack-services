"""shack-watch against canned `ss` output, a fake /proc and a fake ntfy."""

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import shack_watch as sw

# the real lines from the morning Pat (pat@ag7jc) held Mercury all night
SS_PAT = ('0      0           127.0.0.1:60574      127.0.0.1:8300  users:(("pat",pid=905747,fd=431))\n'
          '0      0           127.0.0.1:45276      127.0.0.1:8301  users:(("pat",pid=905747,fd=432))\n'
          '0      0           127.0.0.1:8300       127.0.0.1:60574 users:(("mercury",pid=1596807,fd=13))\n')
SS_CONNECTOR = ('0 0 127.0.0.1:41000 127.0.0.1:8300 users:(("mercury-connec",pid=1893190,fd=5))\n'
                '0 0 127.0.0.1:8300 127.0.0.1:41000 users:(("mercury",pid=1596807,fd=13))\n')
SS_LISTEN = ('0 0 127.0.0.1:52000 127.0.0.1:8300 users:(("pat",pid=1596838,fd=7))\n')


def fake_proc():
    root = Path(tempfile.mkdtemp())
    for pid, unit in ((905747, "pat@ag7jc.service"), (1893190, "mercury-connector.service"),
                      (1596838, "pat-listen.service")):
        (root / str(pid)).mkdir()
        (root / str(pid) / "cgroup").write_text(f"0::/system.slice/system-pat.slice/{unit}\n")
    return str(root)


class Base(unittest.TestCase):
    def setUp(self):
        self.proc = fake_proc()
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "shack-mode"
        self.state.write_text("MODE=mercury\n")
        self.ss = SS_CONNECTOR
        self.pids = {"mercury-connector": 1893190, "pat-listen": 0}
        self.active = {"mercury": True}
        self.pushes = []
        patches = [
            mock.patch.object(sw, "STATE", self.state),
            mock.patch.object(sw, "PROC", self.proc),
            mock.patch.object(sw, "run", side_effect=lambda cmd: self.ss if cmd[0] == "ss" else ""),
            mock.patch.object(sw, "main_pid", side_effect=lambda u: self.pids.get(u, 0)),
            mock.patch.object(sw, "active", side_effect=lambda u: self.active.get(u, False)),
            mock.patch.object(sw, "restarts", return_value=3625),
            mock.patch.object(sw, "push", side_effect=lambda t, b, high=True, env=None:
                              self.pushes.append((t, b, high)) or True),
            mock.patch.object(sw, "log", lambda m: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Holders(unittest.TestCase):
    def test_parses_real_ss_lines(self):
        self.assertEqual(sw.holders(ss_out=SS_PAT), [("pat", 905747)])     # 8301 and Mercury's own end ignored
        self.assertEqual(sw.holders(ss_out=SS_CONNECTOR), [("mercury-connec", 1893190)])
        self.assertEqual(sw.holders(ss_out=""), [])

    def test_unit_from_cgroup(self):
        self.assertEqual(sw.unit_of(905747, fake_proc()), "pat@ag7jc.service")
        self.assertEqual(sw.unit_of(1, "/nonexistent"), "")


class Check(Base):
    def test_connector_has_it(self):
        ok, text = sw.check(proc=self.proc)
        self.assertTrue(ok, text)

    def test_the_overnight_case(self):
        self.ss = SS_PAT
        ok, text = sw.check(proc=self.proc)
        self.assertFalse(ok)
        self.assertIn("pat (pid 905747, pat@ag7jc.service)", text)
        self.assertIn("not mercury-connector", text)

    def test_free_but_connector_not_connected(self):
        self.ss = ""
        ok, text = sw.check(proc=self.proc)
        self.assertFalse(ok)
        self.assertIn("3625 restarts", text)

    def test_mercury_down(self):
        self.active["mercury"] = False
        ok, text = sw.check(proc=self.proc)
        self.assertFalse(ok)
        self.assertIn("isn't running", text)

    def test_pat_mode_wants_pat_listen_not_any_pat(self):
        self.state.write_text("MODE=pat\n")
        self.pids["pat-listen"] = 1596838
        self.ss = SS_LISTEN
        self.assertTrue(sw.check(proc=self.proc)[0])
        self.ss = SS_PAT                                       # the everyday Pat: wrong one
        self.assertFalse(sw.check(proc=self.proc)[0])

    def test_other_modes_dont_care(self):
        for m in ("apps", "modem73"):
            self.state.write_text(f"MODE={m}\n")
            self.assertIsNone(sw.check(proc=self.proc)[0])

    def test_no_state_file_means_mercury(self):
        self.state.unlink()
        self.assertEqual(sw.mode(self.state), "mercury")


class Handback(Base):
    def test_waits_until_the_connector_gets_it(self):
        seq = iter(["", SS_CONNECTOR])
        sw.run.side_effect = lambda cmd: next(seq, SS_CONNECTOR) if cmd[0] == "ss" else ""
        self.assertEqual(sw.check_handback(wait=5, poll=0.01), 0)
        self.assertEqual(self.pushes, [])

    def test_pushes_when_someone_else_keeps_it(self):
        self.ss = SS_PAT
        self.assertEqual(sw.check_handback(wait=0.05, poll=0.01), 1)
        self.assertEqual(len(self.pushes), 1)
        title, body, high = self.pushes[0]
        self.assertEqual(title, "Switch to MERCURY failed")
        self.assertIn("pat@ag7jc.service", body)
        self.assertTrue(high)


class Watch(Base):
    def test_alerts_once_after_grace_then_when_back(self):
        st = self.tmp / "watch.json"
        self.ss = SS_PAT
        sw.watch(now=1000, state_path=st)
        sw.watch(now=1000 + 300, state_path=st)
        self.assertEqual(self.pushes, [])                      # under 10 minutes: quiet
        sw.watch(now=1000 + 600, state_path=st)
        sw.watch(now=1000 + 720, state_path=st)
        self.assertEqual(len(self.pushes), 1)                  # once, not every 2 minutes
        self.assertEqual(self.pushes[0][0], "HF station off the air")
        self.assertIn("for 10 min", self.pushes[0][1])
        self.ss = SS_CONNECTOR
        sw.watch(now=2000, state_path=st)
        self.assertEqual(self.pushes[-1][0], "HF station back on the air")
        self.assertFalse(self.pushes[-1][2])
        self.assertEqual(json.loads(st.read_text()), {})

    def test_short_blip_never_alerts(self):
        st = self.tmp / "watch.json"
        self.ss = ""
        sw.watch(now=0, state_path=st)
        self.ss = SS_CONNECTOR
        sw.watch(now=120, state_path=st)
        self.assertEqual(self.pushes, [])

    def test_pat_mode_with_pat_listening_is_fine(self):
        st = self.tmp / "watch.json"
        self.state.write_text("MODE=pat\nUNTIL=9999999999\n")
        self.pids["pat-listen"] = 1596838
        self.ss = SS_LISTEN
        sw.watch(now=0, state_path=st)
        sw.watch(now=5000, state_path=st)
        self.assertEqual(self.pushes, [])


class Push(unittest.TestCase):
    def test_ntfy_request(self):
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got["headers"] = dict(self.headers)
                got["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        env = {"NTFY_URL": f"http://127.0.0.1:{srv.server_port}/topic", "NTFY_TOKEN": "tk",
               "NTFY_CLICK": "https://ag7jc.com"}
        with mock.patch.object(sw, "log", lambda m: None):
            self.assertTrue(sw.push("HF station off the air", "held by pat", env=env))
        self.assertEqual(got["body"], "held by pat")
        self.assertEqual(got["headers"]["Title"], "HF station off the air")
        self.assertEqual(got["headers"]["Priority"], "high")
        self.assertEqual(got["headers"]["Authorization"], "Bearer tk")
        srv.server_close()

    def test_no_url_no_push(self):
        with mock.patch.object(sw, "log", lambda m: None):
            self.assertFalse(sw.push("t", "b", env={}))

    def test_env_file(self):
        p = Path(tempfile.mkdtemp()) / "alerts.env"
        p.write_text("# c\nNTFY_URL=https://ntfy.sh/abc   # mine\n\nNTFY_CLICK=\"https://x\"\n")
        self.assertEqual(sw.load_env(p), {"NTFY_URL": "https://ntfy.sh/abc", "NTFY_CLICK": "https://x"})


if __name__ == "__main__":
    unittest.main()
