import json
import tempfile
import unittest
from pathlib import Path

try:
    import shack_services as ss
except (ImportError, ValueError):          # no GTK 4 here
    ss = None


@unittest.skipIf(ss is None, "GTK 4 / libadwaita not installed")
class HfContactRow(unittest.TestCase):
    def file(self, *events):
        p = Path(tempfile.mkdtemp()) / "events.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in events))
        return p

    def test_no_file(self):
        self.assertEqual(ss.hf_contact_summary(Path("/nonexistent/events.jsonl"))[0], "dim")

    def test_quiet(self):
        p = self.file({"t": 1000, "event": "echo", "call": "K7SVI"})
        self.assertEqual(ss.hf_contact_summary(p, now=1000 + 90000), ("dim", "no callers in the last 24 h"))

    def test_messages_are_amber(self):
        now = 1793305000
        p = self.file({"t": now - 900, "event": "link_up", "who": "guest", "call": "K7SVI"},
                      {"t": now - 880, "event": "echo", "call": "K7SVI", "snr": -1.0, "bps": 68},
                      {"t": now - 600, "event": "message", "call": "K7SVI"},
                      {"t": now - 500, "event": "link_down"})
        color, text = ss.hf_contact_summary(p, now=now)
        self.assertEqual(color, "amber")
        self.assertEqual(text, "1 message · 1 echo test in 24 h  ·  last K7SVI (message, 10 min ago)")

    def test_echoes_only_green(self):
        now = 1793305000
        p = self.file({"t": now - 30, "event": "echo", "call": "K1ABC"}, {"t": now - 20, "event": "link_down"})
        self.assertEqual(ss.hf_contact_summary(p, now=now), ("green", "1 echo test in 24 h  ·  last K1ABC (echo, just now)"))

    def test_linked_now(self):
        now = 1793305000
        p = self.file({"t": now - 60, "event": "link_up", "who": "guest", "call": "K7SVI"})
        self.assertEqual(ss.hf_contact_summary(p, now=now), ("green", "on the air with K7SVI now"))
        p = self.file({"t": now - 60, "event": "link_up", "who": "peer"})       # our own station
        self.assertEqual(ss.hf_contact_summary(p, now=now)[0], "dim")


if __name__ == "__main__":
    unittest.main()
