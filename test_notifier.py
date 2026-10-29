"""Notification rules (no GNOME needed)."""
import unittest
import service_query as sq
from shack_services import Notifier


def st(name, active, sub="running"):
    return sq.ServiceStatus(name, active, sub, None, sq._category_for(active))


class NotifierTests(unittest.TestCase):
    def test_failure_alerts_once_and_clears(self):
        n = Notifier()
        self.assertEqual(n.update({"a": st("a", "active")}, set()), [])
        out = n.update({"a": st("a", "failed", "failed")}, set())
        self.assertEqual([(o[0], o[1]) for o in out], [("send", "a")])
        self.assertTrue(out[0][4])                                   # urgent
        self.assertEqual(n.update({"a": st("a", "failed", "failed")}, set()), [])   # no repeat
        out = n.update({"a": st("a", "active")}, set())
        self.assertEqual([(o[0], o[1]) for o in out], [("withdraw", "a"), ("send", "a:ok")])

    def test_stopped_alerts_but_not_paused(self):
        n = Notifier()
        n.update({"a": st("a", "active"), "m": st("m", "active")}, set())
        out = n.update({"a": st("a", "inactive", "dead"), "m": st("m", "inactive", "dead")}, {"m"})
        self.assertEqual([(o[0], o[1]) for o in out], [("send", "a")])

    def test_already_stopped_at_start_is_quiet_but_failed_is_not(self):
        n = Notifier()
        out = n.update({"a": st("a", "inactive", "dead"), "b": st("b", "failed", "failed")}, set())
        self.assertEqual([o[1] for o in out], ["b"])

    def test_radio_conflict(self):
        n = Notifier()
        self.assertEqual([o[1] for o in n.update({}, set(), "conflict")], ["radio"])
        self.assertEqual(n.update({}, set(), "conflict"), [])
        self.assertEqual([(o[0], o[1]) for o in n.update({}, set(), "mercury")], [("withdraw", "radio")])


class ConfigTests(unittest.TestCase):
    def test_parse(self):
        import tempfile, pathlib, shack_services as s
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x" / "services.txt"
            self.assertEqual(s.load_services(p), s.DEFAULT_SERVICES)        # written on first run
            p.write_text("# c\nmercury.service\nmercury   # same again\nfoo.timer\n\n")
            self.assertEqual(s.load_services(p), ["mercury.service", "foo.timer"])


class AmpRowTests(unittest.TestCase):
    def _w(self, d, **kw):
        import json, pathlib
        p = pathlib.Path(d) / "s.json"
        p.write_text(json.dumps(dict({"time": 100, "linked": True, "amp_band": "20", "temp_f": 104.0,
                                      "for_w": 0, "swr": None, "alerts": []}, **kw)))
        return p

    def test_states(self):
        import tempfile
        from shack_services import amp_summary
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(amp_summary(self._w(d), 101)[0], "green")
            self.assertIn("20 m", amp_summary(self._w(d), 101)[1])
            self.assertEqual(amp_summary(self._w(d), 200)[0], "dim")             # stale file
            self.assertEqual(amp_summary(self._w(d, linked=False), 101)[0], "amber")
            c, t = amp_summary(self._w(d, alerts=["MISMATCH"], rig_band="40"), 101)
            self.assertEqual(c, "red")
            self.assertIn("radio on 40 m", t)
            self.assertEqual(amp_summary(d + "/missing.json", 101)[0], "dim")


if __name__ == "__main__":
    unittest.main()
