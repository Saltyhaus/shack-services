"""radio-profile dry-run output -> the change list Shack Services shows."""
import os, sys, io, unittest, tempfile
from contextlib import redirect_stdout
sys.path.insert(0, os.path.dirname(__file__))
for d in (os.path.join(os.path.dirname(__file__), "..", "radio-profile"),
          os.path.expanduser("~/hamshack-config/tools/radio-profile")):
    sys.path.insert(0, d)


class ParsePlan(unittest.TestCase):
    def setUp(self):
        import importlib, shack_services
        self.ss = shack_services

    def test_changes(self):
        out = "reading...\n3 change(s) to 'voice':\n  menu 072  DATA PORT   1 -> 0\n  mic gain    030 -> 060\n  mode   DATA-USB -> USB\n(dry run -- nothing written)\n"
        self.assertEqual(len(self.ss.parse_plan(out)), 3)
        self.assertIn("DATA PORT", self.ss.parse_plan(out)[0])

    def test_matches(self):
        self.assertEqual(self.ss.parse_plan("already matches 'data' -- nothing to change\n"), [])

    def test_garbage(self):
        self.assertIsNone(self.ss.parse_plan("rigctld didn't answer in time"))

    def test_against_real_tool(self):
        try:
            from test_radio_profile import FakeRig
        except ImportError:
            self.skipTest("radio-profile source not found")
        tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_DATA_HOME"] = tmp.name; os.environ["XDG_CONFIG_HOME"] = tmp.name
        import importlib, radio_profile as rp
        rp = importlib.reload(rp)
        rig = FakeRig(); rp.RADIOS["ft991a"]["rigctld"] = f"127.0.0.1:{rig.port}"
        def cli(*a):
            b = io.StringIO()
            with redirect_stdout(b):
                try: rp.main(["ft991a", *a])
                except SystemExit: pass
            return b.getvalue()
        cli("save", "data"); rig.menus["072"] = "0"; rig.state["MG"] = "060"; cli("save", "voice")
        rig.menus["072"] = "1"; rig.state["MG"] = "030"
        plan = self.ss.parse_plan(cli("voice", "--dry-run"))
        self.assertEqual(len(plan), 2, plan)
        rig.menus["072"] = "0"; rig.state["MG"] = "060"
        self.assertEqual(self.ss.parse_plan(cli("voice", "--dry-run")), [])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
