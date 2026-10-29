"""Unit tests for shack_mode.py's state logic (no systemd / Tk needed)."""
import os
import tempfile
import unittest

import shack_mode as sm


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


if __name__ == "__main__":
    unittest.main()
