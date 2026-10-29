import json
import os
import tempfile
import unittest
import wave
import io
from pathlib import Path

import hf_alerts as h


class Events(unittest.TestCase):
    def test_guest_events_only(self):
        self.assertEqual(h.parse_event('{"t":1,"event":"echo","call":"K7SVI","snr":-1.0,"bps":68}'),
                         {"kind": "echo", "call": "K7SVI", "t": 1, "snr": -1.0, "bps": 68})
        self.assertEqual(h.parse_event('{"t":1,"event":"message","call":"k1abc-9"}')["call"], "K1ABC-9")
        for line in ('{"t":1,"event":"link_up","who":"guest","call":"K7SVI"}', '{"t":1,"event":"link_down"}',
                     '{"t":1,"event":"message","call":""}', '{"event":"echo","call":"../etc"}', "not json", "[]"):
            self.assertIsNone(h.parse_event(line), line)

    def test_describe(self):
        t, b = h.describe({"kind": "echo", "call": "K7SVI", "t": 1793304494, "snr": -1.0, "bps": 68})
        self.assertEqual(t, "Echo test: K7SVI")
        self.assertIn("SNR -1.0 dB, 68 bps", b)
        t, b = h.describe({"kind": "message", "call": "K7SVI", "t": 1793304668, "snr": None, "bps": None})
        self.assertEqual(t, "HF message from K7SVI")


class TailTest(unittest.TestCase):
    def test_follows_new_lines_partial_and_rotation(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "events.jsonl"
        p.write_text('{"old":1}\n')
        t = h.Tail(p)
        self.assertEqual(t.read(), [])                     # existing lines don't alert
        with open(p, "a") as f:
            f.write('{"a":1}\n{"b"')
        self.assertEqual(t.read(), ['{"a":1}'])
        with open(p, "a") as f:
            f.write(':2}\n')
        self.assertEqual(t.read(), ['{"b":2}'])
        os.rename(p, Path(d) / "events.jsonl.1")           # rotated
        p.write_text('{"c":3}\n')
        self.assertEqual(t.read(), ['{"c":3}'])
        p.write_text("")                                    # truncated
        self.assertEqual(t.read(), [])
        p.write_text('{"d":4}\n')
        self.assertEqual(t.read(), ['{"d":4}'])

    def test_missing_file_then_created(self):
        p = Path(tempfile.mkdtemp()) / "events.jsonl"
        t = h.Tail(p)
        self.assertEqual(t.read(), [])
        p.write_text('{"x":1}\n')
        self.assertEqual(t.read(), ['{"x":1}'])


class Sound(unittest.TestCase):
    def test_cw_timing(self):
        dit = 1.2 / 20
        parts = h.cw_timing("K", 20)                       # -.-
        self.assertEqual([on for on, _ in parts], [True, False, True, False, True])
        self.assertAlmostEqual(sum(d for _, d in parts), 3 * dit + dit + dit + dit + 3 * dit)

    def test_wav_is_valid(self):
        w = wave.open(io.BytesIO(h.render_wav(h.cw_timing("K7SVI"))))
        self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 22050))
        self.assertGreater(w.getnframes(), 22050)          # a callsign is over a second
        h.render_wav(h.chirp_timing())

    def test_never_plays_on_a_radio(self):
        s = dict(h.DEFAULTS)
        sinks = [("alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo", "USB Audio CODEC"),
                 ("alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.analog-stereo", "USB Audio Device"),
                 ("alsa_output.pci-0000_00_1f.3.analog-stereo", "Built-in Audio Analog Stereo")]
        self.assertEqual(h.choose_sink(s, sinks, default=sinks[2][0]), (sinks[2][0], None))
        for bad in sinks[:2]:
            sink, why = h.choose_sink(s, sinks, default=bad[0])
            self.assertIsNone(sink)
            self.assertIn("radio", why)
            sink, why = h.choose_sink(dict(s, SOUND_SINK=bad[0]), sinks, default=sinks[2][0])
            self.assertIsNone(sink, "an explicit radio sink is refused too")

    def test_quiet_hours(self):
        for hour, quiet in ((21, False), (22, True), (2, True), (6, True), (7, False), (12, False)):
            self.assertEqual(h.in_quiet_hours("22-7", hour), quiet, hour)
        self.assertTrue(h.in_quiet_hours("1-5", 3))
        self.assertFalse(h.in_quiet_hours("", 3))
        self.assertFalse(h.in_quiet_hours("bogus", 3))


class Push(unittest.TestCase):
    def test_request(self):
        s = dict(h.DEFAULTS)
        self.assertIsNone(h.push_request(s, "t", "b", "message"))
        s.update(NTFY_URL="https://ntfy.example/hf-xyz", NTFY_TOKEN="tk", NTFY_CLICK="https://dash/hf")
        r = h.push_request(s, "HF message from K7SVI", "body ünïcode", "message")
        self.assertEqual(r.full_url, "https://ntfy.example/hf-xyz")
        self.assertEqual(r.get_header("Priority"), "high")
        self.assertEqual(r.get_header("Authorization"), "Bearer tk")
        self.assertEqual(r.get_header("Click"), "https://dash/hf")
        self.assertEqual(r.data, "body ünïcode".encode())
        self.assertEqual(h.push_request(s, "Echo test: K7SVI", "b", "echo").get_header("Priority"), "low")

    def test_settings_file(self):
        p = Path(tempfile.mkdtemp()) / "hf-alerts.env"
        p.write_text("# c\nNTFY_URL=https://ntfy.sh/abc   # mine\nNOTIFY_ECHO=desktop\nQUIET_HOURS=\n")
        s = h.load_settings(p)
        self.assertEqual(s["NTFY_URL"], "https://ntfy.sh/abc")
        self.assertEqual(h.channels(s, "echo"), {"desktop"})
        self.assertEqual(h.channels(s, "message"), {"desktop", "sound", "push"})
        self.assertEqual(s["QUIET_HOURS"], "")


class Alert(unittest.TestCase):
    def test_channels_run_and_failures_dont_raise(self):
        calls = []
        real = (h.desktop_notify, h.play, h.push, h.choose_sink)
        h.desktop_notify = lambda t, b, urgent=False: (calls.append("desktop"), (True, ""))[1]
        h.play = lambda wav, sink: (calls.append("sound"), (False, "boom"))[1]
        h.push = lambda s, t, b, k: (calls.append("push"), (False, "offline"))[1]
        h.choose_sink = lambda s: ("speakers", None)
        try:
            ev = {"kind": "message", "call": "K7SVI", "t": 1, "snr": None, "bps": None}
            h.alert(ev, dict(h.DEFAULTS), now_hour=12)
            self.assertEqual(calls, ["desktop", "sound", "push"])
            calls.clear()
            h.alert(ev, dict(h.DEFAULTS), now_hour=23)      # quiet hours: no sound
            self.assertEqual(calls, ["desktop", "push"])
            calls.clear()
            h.alert(dict(ev, kind="echo"), dict(h.DEFAULTS), now_hour=12)
            self.assertEqual(calls, ["desktop", "sound"])   # echoes don't push by default
        finally:
            h.desktop_notify, h.play, h.push, h.choose_sink = real


if __name__ == "__main__":
    unittest.main()
