"""Unit tests for shack_mode.py's state logic (no systemd / Tk needed)."""
import os
import tempfile
import unittest
from unittest import mock

import shack_mode as sm
import shack_mode


class StateFileTests(unittest.TestCase):
    def test_parse(self):
        st = sm.parse_state_file('MODE=apps\nSINCE=1792730327\nPAUSED="mercury mercury-connector"\n')
        self.assertEqual(st.mode, "apps")
        self.assertIsNotNone(st.since)
        self.assertEqual(st.paused, ["mercury", "mercury-connector"])

    def test_empty_paused(self):
        st = sm.parse_state_file('MODE=mercury\nSINCE=1\nPAUSED=""\n')
        self.assertEqual(st.paused, [])

    def test_missing_file(self):
        st = sm.read_state_file("/nonexistent/shack-mode")
        self.assertIsNone(st.mode)


class OwnerTests(unittest.TestCase):
    def test_single(self):
        self.assertEqual(sm.decide_owner(True, False, []), ("mercury", ["mercury"]))
        self.assertEqual(sm.decide_owner(False, True, []), ("modem73", ["modem73"]))
        self.assertEqual(sm.decide_owner(False, False, ["wsjtx"]), ("apps", ["apps"]))

    def test_conflict_and_none(self):
        self.assertEqual(sm.decide_owner(True, False, ["wsjtx"])[0], "conflict")
        self.assertEqual(sm.decide_owner(False, False, [])[0], "none")

    def test_paused_units_only_in_apps_or_modem73(self):
        rs = sm.RadioState("apps", [], "apps", None, ["mercury", "mercury-connector"], [], [])
        self.assertEqual(sm.paused_units(rs), ["mercury.service", "mercury-connector.service"])
        rs.mode = "mercury"
        self.assertEqual(sm.paused_units(rs), [])


class OutboxTests(unittest.TestCase):
    def test_lists_files_skips_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "AG7JC-2"))
            open(os.path.join(d, "AG7JC-2", "msg1"), "w").close()
            open(os.path.join(d, "AG7JC-2", ".tmp"), "w").close()
            self.assertEqual(sm.outbox_files(d), ["AG7JC-2/msg1"])


class AppsTests(unittest.TestCase):
    def test_finds_by_comm(self):
        with tempfile.TemporaryDirectory() as d:
            for pid, comm in (("10", "wsjtx"), ("11", "bash"), ("12", "JS8Call")):
                os.makedirs(os.path.join(d, pid))
                with open(os.path.join(d, pid, "comm"), "w") as f:
                    f.write(comm + "\n")
            self.assertEqual(sm.apps_running(d), ["JS8Call", "wsjtx"])



class PatModeTests(unittest.TestCase):
    def test_until_parsed(self):
        st = shack_mode.parse_state_file('MODE=pat\nSINCE=1700000000\nPAUSED="mercury-connector"\nUNTIL=1700007200\n')
        self.assertEqual(st.mode, "pat")
        self.assertEqual((st.until - st.since).total_seconds(), 7200)
        self.assertIsNone(shack_mode.parse_state_file("MODE=mercury\n").until)

    def test_connector_shows_paused_in_pat_mode(self):
        st = shack_mode.parse_state_file('MODE=pat\nSINCE=1\nPAUSED="mercury-connector"\nUNTIL=2\n')
        rs = shack_mode.RadioState("pat", ["mercury"], st.mode, st.since, st.paused, [], [], st.until)
        self.assertEqual(shack_mode.paused_units(rs), ["mercury-connector.service"])

    def test_owner_is_pat_when_pat_answers(self):
        st = shack_mode.parse_state_file('MODE=pat\nSINCE=1\nPAUSED="mercury-connector"\nUNTIL=2\n')
        with mock.patch.object(shack_mode, "apps_running", return_value=[]), \
             mock.patch.object(shack_mode, "user_unit_active", return_value=False), \
             mock.patch.object(shack_mode, "outbox_files", return_value=[]), \
             mock.patch.object(shack_mode, "system_unit_active", return_value=True):
            rs = shack_mode.radio_state(True, st)
        self.assertEqual((rs.owner, rs.until), ("pat", st.until))
        with mock.patch.object(shack_mode, "apps_running", return_value=[]), \
             mock.patch.object(shack_mode, "user_unit_active", return_value=False), \
             mock.patch.object(shack_mode, "outbox_files", return_value=[]), \
             mock.patch.object(shack_mode, "system_unit_active", return_value=False):
            rs = shack_mode.radio_state(True, shack_mode.parse_state_file("MODE=mercury\n"))
        self.assertEqual((rs.owner, rs.until), ("mercury", None))

    def test_when_is_checked_before_running_anything(self):
        for bad in ("soon", "2d", "25:00", "21:5", "; rm -rf /", ""):
            r = shack_mode.switch("pat", when=bad)
            self.assertFalse(r.success, bad)
            self.assertIn("can't read", r.message)
        self.assertFalse(shack_mode.switch("mercury", when="2h").success)
        for good in ("2h", "90m", "1h30m", "21:00", "7:05"):
            self.assertTrue(shack_mode.PAT_WHEN_RE.fullmatch(good), good)
if __name__ == "__main__":
    unittest.main()
