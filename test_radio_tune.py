"""radio-tune against a fake rigctld, fake amp status and fake
aplay/systemctl/sudo -- nothing here keys a real radio."""

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

import radio_tune as rt


class FakeRig:
    """Just enough rigctld: f/F, m/M, l/L, t/T, \\mW2power, \\power2mW."""

    def __init__(self, max_w=100, freq=14105000, mode="PKTUSB", power=1.0, ignore_power=False):
        self.st = {"f": str(freq), "m": mode, "pb": "3000", "p": f"{power:.3f}", "t": "0"}
        self.max_w, self.ignore_power, self.log = max_w, ignore_power, []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.addr = f"127.0.0.1:{self.srv.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        st, f = self.st, c.makefile("r")
        for line in f:
            p = line.split()
            if not p:
                continue
            self.log.append(line.strip())
            k = p[0]
            keyed = st["t"] != "0"
            watts = float(st["p"]) * self.max_w if keyed else 0
            out = {"f": st["f"], "m": f"{st['m']}\n{st['pb']}", "t": st["t"]}.get(k)
            if k == "l":
                out = {"RFPOWER": st["p"], "SWR": "1.1" if keyed else "1", "ALC": "0.2",
                       "RFPOWER_METER_WATTS": f"{watts:.1f}"}.get(p[1], "0")
            elif k == "\\mW2power":
                out = f"{int(p[1]) / 1000 / self.max_w:.6f}"
            elif k == "\\power2mW":
                out = str(int(float(p[1]) * self.max_w * 1000))
            elif k in "FMLT":
                if k == "F":
                    st["f"] = p[1]
                elif k == "M":
                    st["m"], st["pb"] = p[1], p[2]
                elif k == "L" and not self.ignore_power:
                    st["p"] = p[2]
                elif k == "T":
                    st["t"] = p[1]
                out = "RPRT 0"
            c.sendall(((out or "RPRT -1") + "\n").encode())

    def sets(self):
        return [l for l in self.log if l[0] in "FMLT"]

    def close(self):
        self.srv.close()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.units = self.tmp / "units"
        self.units.mkdir()
        self.calls = self.tmp / "calls"
        self._exe("systemctl", f"""#!/bin/sh
d={self.units}
echo "systemctl $*" >> {self.calls}
case "$1" in
 is-active) [ -e "$d/$3" ] ;;
 stop) shift; for u; do rm -f "$d/$u"; done ;;
 start) shift; for u; do touch "$d/$u"; done ;;
esac""")
        self._exe("sudo", f"""#!/bin/sh
[ "$1" = -A ] && shift
[ "$1" = -v ] && exit 0
echo "sudo $*" >> {self.calls}
exec "$@" """)
        self._exe("aplay", "#!/bin/sh\nsleep 30\n")
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        os.environ.pop("SUDO_ASKPASS", None)
        self.amp = self.tmp / "status.json"
        self._old_amp = rt.AMP_STATUS
        rt.AMP_STATUS = self.amp
        self.rig = None

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        rt.AMP_STATUS = self._old_amp
        if self.rig:
            self.rig.close()

    def _exe(self, name, body):
        p = self.bin / name
        p.write_text(body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def config(self, radio, **kw):
        c = self.tmp / "tune.json"
        c.write_text(json.dumps({radio: {"rigctld": self.rig.addr, **kw}}))
        return c

    def set_amp(self, for_w=0, linked=True, age=0):
        self.amp.write_text(json.dumps({"time": time.time() - age, "linked": linked, "for_w": for_w}))


class Freq(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rt.parse_freq("7.1065"), 7106500)
        self.assertEqual(rt.parse_freq("7106.5"), 7106500)
        self.assertEqual(rt.parse_freq("7106500"), 7106500)
        self.assertEqual(rt.parse_freq(" 14.105 MHz"), 14105000)
        with self.assertRaises(rt.TuneError):
            rt.parse_freq("forty")

    def test_bands(self):
        self.assertTrue(rt.in_band(7106500))
        self.assertTrue(rt.in_band(14105000))
        self.assertFalse(rt.in_band(7350000))
        self.assertFalse(rt.in_band(27185000))     # CB


class FT991A(Env):
    def test_stops_mercury_tunes_and_restarts_it(self):
        self.rig = FakeRig()
        (self.units / "mercury").touch()
        (self.units / "pat-listen").touch()
        cfg = self.config("ft991a", listen_port=None)
        r = rt.tune("ft991a", 7106500, secs=1.2, config=cfg)
        self.assertTrue(r["ok"])
        self.assertIn("SWR 1.1 at 10 W", r["summary"])
        sets = self.rig.sets()
        self.assertEqual(sets[:4], ["F 7106500", "M PKTUSB 3000", "L RFPOWER 0.100", "T 1"])
        self.assertEqual(sets[4:], ["T 0", "L RFPOWER 1.000"])             # power back, mode kept
        calls = self.calls.read_text()
        self.assertIn("systemctl stop pat-listen mercury", calls)
        self.assertLess(calls.index("start mercury"), calls.index("start pat-listen"))
        self.assertEqual(sorted(os.listdir(self.units)), ["mercury", "pat-listen"])

    def test_refuses_when_radio_reports_too_much_power(self):
        self.rig = FakeRig(ignore_power=True)                             # stays at 100 W
        with self.assertRaises(rt.TuneError) as cm:
            rt.tune("ft991a", 7106500, secs=1, config=self.config("ft991a"))
        self.assertIn("not keying", str(cm.exception))
        self.assertNotIn("T 1", self.rig.sets())
        self.assertIn("T 0", self.rig.sets())                             # clean-up still ran

    def test_no_key_when_sound_card_busy(self):
        self.rig = FakeRig()
        self._exe("aplay", "#!/bin/sh\necho 'Device or resource busy' >&2; exit 1\n")
        with self.assertRaises(rt.TuneError) as cm:
            rt.tune("ft991a", 7106500, secs=1, config=self.config("ft991a"))
        self.assertIn("resource busy", str(cm.exception))
        self.assertNotIn("T 1", self.rig.sets())

    def test_out_of_band_and_already_transmitting(self):
        self.rig = FakeRig()
        with self.assertRaises(rt.TuneError):
            rt.tune("ft991a", 7350000, config=self.config("ft991a"))
        self.rig.st["t"] = "1"
        with self.assertRaises(rt.TuneError) as cm:
            rt.tune("ft991a", 7106500, config=self.config("ft991a"))
        self.assertIn("transmitting", str(cm.exception))
        self.assertEqual(self.rig.sets(), [])

    def test_dry_run_touches_nothing(self):
        self.rig = FakeRig()
        (self.units / "mercury").touch()
        r = rt.tune("ft991a", 7106500, dry_run=True, config=self.config("ft991a"))
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.rig.sets(), [])
        self.assertFalse(self.calls.exists() and "stop" in self.calls.read_text())


class KX3(Env):
    def test_needs_the_amp_watched(self):
        self.rig = FakeRig(max_w=15, mode="USB")
        with self.assertRaises(rt.TuneError) as cm:
            rt.tune("kx3", 14105000, config=self.config("kx3"))
        self.assertIn("DN-AUTO Monitor", str(cm.exception))
        self.set_amp(age=60)                                             # stale
        with self.assertRaises(rt.TuneError):
            rt.tune("kx3", 14105000, config=self.config("kx3"))
        self.assertEqual(self.rig.sets(), [])

    def test_tunes_and_puts_the_mode_back(self):
        self.rig = FakeRig(max_w=15, mode="USB", power=0.5)
        self.set_amp(0)
        r = rt.tune("kx3", 14105000, secs=1.2, config=self.config("kx3"))
        self.assertTrue(r["ok"], r)
        sets = self.rig.sets()
        self.assertEqual(sets[2], f"L RFPOWER {10 / 15:.3f}")              # hamlib's watts -> level
        self.assertEqual(sets[-3:], ["T 0", "L RFPOWER 0.500", "M USB 3000"])

    def test_amp_in_operate_unkeys_at_once(self):
        self.rig = FakeRig(max_w=15, mode="LSB")
        self.set_amp(0)

        def amp_wakes():
            time.sleep(0.8)
            self.set_amp(850)
        threading.Thread(target=amp_wakes, daemon=True).start()
        t0 = time.monotonic()
        r = rt.tune("kx3", 7106500, secs=10, config=self.config("kx3"))
        self.assertLess(time.monotonic() - t0, 3)
        self.assertFalse(r["ok"])
        self.assertIn("OPERATE", r["stopped_by"])
        self.assertEqual(self.rig.st["t"], "0")
        self.assertEqual(self.rig.st["m"], "LSB")

    def test_without_watchdog_when_asked(self):
        self.rig = FakeRig(max_w=15)
        r = rt.tune("kx3", 14105000, secs=1, amp_watchdog=False, config=self.config("kx3"))
        self.assertTrue(r["ok"])


class Cli(Env):
    def test_exit_codes(self):
        self.rig = FakeRig()
        cfg = self.config("ft991a")
        old = rt.CONFIG
        rt.CONFIG = cfg
        try:
            self.assertEqual(rt.main(["ft991a", "7.1065", "--dry-run"]), 0)
            self.assertEqual(rt.main(["ft991a", "27.185"]), 2)
        finally:
            rt.CONFIG = old


if __name__ == "__main__":
    unittest.main()
