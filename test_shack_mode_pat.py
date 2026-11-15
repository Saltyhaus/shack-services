"""shack-mode's pat mode, run for real against fake systemctl / sudo /
systemd-run / rigctl, so the switching logic is checked without a radio."""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "shack-mode")
REVERT = os.path.join(HERE, "systemd", "shack-pat-revert")

FAKE_SYSTEMCTL = r'''#!/usr/bin/env bash
# units are files in $FAKE_UNITS; Conflicts/Requires like pat-listen.service
D=$FAKE_UNITS; echo "systemctl $*" >> "$FAKE_LOG"
[ "$1" = --user ] && { [ "$2" = is-active ] && exit 3; exit 0; }
cmd=$1; shift
units=(); for a in "$@"; do case $a in --*) ;; *) units+=("${a%.service}");; esac; done
case $cmd in
  is-active) [ -f "$D/${units[0]}" ] && { [ "$*" != "${*#--quiet}" ] || echo active; exit 0; } || { [ "$*" != "${*#--quiet}" ] || echo inactive; exit 3; } ;;
  cat) [ "${units[0]}" = pat-listen ] && [ -n "$FAKE_PAT_INSTALLED" ] && exit 0; exit 1 ;;
  start) for u in "${units[@]}"; do
           case $u in
             pat-listen) rm -f "$D/mercury-connector"; touch "$D/mercury"; [ -n "$FAKE_PAT_DIES" ] || touch "$D/pat-listen" ;;
             mercury-connector) rm -f "$D/pat-listen"; [ -n "$FAKE_NO_CONFLICT" ] && true; touch "$D/mercury-connector" ;;
             *) touch "$D/$u" ;;
           esac
         done ;;
  stop) for u in "${units[@]}"; do rm -f "$D/$u"; [ "$u" = mercury ] && rm -f "$D/pat-listen" "$D/mercury-connector"; done ;;
  restart|reset-failed) ;;
esac
exit 0
'''
FAKE_SUDO = '#!/usr/bin/env bash\n[ "$1" = -A ] && shift\n[ "$1" = -v ] && exit 0\nexec "$@"\n'
FAKE_RUN = '#!/usr/bin/env bash\necho "systemd-run $*" >> "$FAKE_LOG"\n'
FAKE_RIGCTL = '#!/usr/bin/env bash\necho 0\n'
FAKE_FUSER = ('#!/usr/bin/env bash\necho "USER PID ACCESS COMMAND"\n'
              '[ -f "$FAKE_UNITS/mercury" ] && echo "/dev/snd/pcmC1D0c: ag7jc 123 F.... mercury"\nexit 0\n')
FAKE_QUIET = '#!/usr/bin/env bash\nexit 1\n'


class PatMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.units = os.path.join(self.tmp, "units")
        os.makedirs(self.bin); os.makedirs(self.units)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("sudo", FAKE_SUDO), ("systemd-run", FAKE_RUN),
                           ("rigctl", FAKE_RIGCTL), ("fuser", FAKE_FUSER), ("pactl", "#!/bin/sh\nexit 0\n"),
                           ("pgrep", FAKE_QUIET), ("logger", "#!/bin/sh\n"),
                           ("id", '#!/bin/sh\necho 1000\n')):     # shack-mode refuses to run as root
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        asound = os.path.join(self.tmp, "asound")
        os.makedirs(asound)
        os.symlink("card1", os.path.join(asound, "CODEC"))
        self.state = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "log")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_UNITS=self.units,
                        FAKE_LOG=self.log, SHACK_MODE_FILE=self.state, SHACK_ASOUND=asound,
                        SHACK_PAT_REVERT=REVERT, FAKE_PAT_INSTALLED="1", HOME=self.tmp)
        os.makedirs(os.path.join(self.tmp, "hf_spool", "outgoing"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def up(self, *units):
        for u in units:
            open(os.path.join(self.units, u), "w").close()

    def active(self):
        return sorted(os.listdir(self.units))

    def run_mode(self, *args, env=None):
        return subprocess.run(["bash", SCRIPT, "-y", *args], capture_output=True, text=True, timeout=60,
                              env=dict(self.env, **(env or {})))

    def state_vals(self):
        out = {}
        for line in open(self.state).read().splitlines():
            k, _, v = line.partition("=")
            out[k] = v.strip('"')
        return out

    def test_from_mercury_to_pat_and_back(self):
        self.up("mercury", "mercury-connector", "hf-sync")
        t0 = int(time.time())
        r = self.run_mode("pat", "2h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.active(), ["hf-sync", "mercury", "pat-listen"])
        st = self.state_vals()
        self.assertEqual((st["MODE"], st["PAUSED"]), ("pat", "mercury-connector"))
        self.assertAlmostEqual(int(st["UNTIL"]) - t0, 7200, delta=5)
        log = open(self.log).read()
        self.assertIn("--unit=shack-pat-revert --on-active=7200s", log)
        self.assertIn("Pat is listening", r.stdout)
        # back to mercury: connector again, pat stopped, timer cancelled
        r = self.run_mode("mercury")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.active(), ["hf-sync", "mercury", "mercury-connector"])
        self.assertEqual(self.state_vals()["MODE"], "mercury")
        self.assertNotIn("UNTIL", self.state_vals())
        self.assertIn("systemctl stop shack-pat-revert.timer", open(self.log).read())

    def test_until_a_time_and_changing_the_end(self):
        self.up("mercury", "mercury-connector", "hf-sync")
        when = time.strftime("%H:%M", time.localtime(time.time() + 3 * 3600))
        r = self.run_mode("pat", when)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = int(self.state_vals()["UNTIL"])
        self.assertAlmostEqual(first - time.time(), 3 * 3600, delta=90)
        r = self.run_mode("pat", "30m")           # already in pat mode: just a new end
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertAlmostEqual(int(self.state_vals()["UNTIL"]) - time.time(), 1800, delta=5)
        self.assertEqual(open(self.log).read().count("systemd-run"), 2)

    def test_time_already_past_means_tomorrow(self):
        self.up("mercury", "mercury-connector")
        when = time.strftime("%H:%M", time.localtime(time.time() - 3600))
        self.assertEqual(self.run_mode("pat", when).returncode, 0)
        self.assertAlmostEqual(int(self.state_vals()["UNTIL"]) - time.time(), 23 * 3600, delta=90)

    def test_bad_durations(self):
        self.up("mercury", "mercury-connector")
        for bad in ("soon", "0h", "25:00", "12:75", "-2h", "2d"):
            r = self.run_mode("pat", bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("can't read", r.stdout + r.stderr, bad)
        r = self.run_mode("pat", "25h")
        self.assertIn("24 hours at most", r.stdout + r.stderr)
        self.assertEqual(self.active(), ["mercury", "mercury-connector"])      # nothing touched

    def test_from_apps_mode_starts_mercury_first(self):
        self.up("hf-sync")
        r = self.run_mode("pat", "1h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.active(), ["hf-sync", "mercury", "pat-listen"])

    def test_not_installed(self):
        self.up("mercury", "mercury-connector")
        r = self.run_mode("pat", env={"FAKE_PAT_INSTALLED": ""})
        self.assertIn("isn't installed", r.stdout + r.stderr)
        self.assertEqual(self.active(), ["mercury", "mercury-connector"])

    def test_pat_that_dies_is_reported(self):
        self.up("mercury", "mercury-connector")
        r = self.run_mode("pat", env={"FAKE_PAT_DIES": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Pat didn't stay up", r.stdout + r.stderr)

    def test_other_modes_stop_pat_and_cancel(self):
        self.up("mercury", "hf-sync")
        self.assertEqual(self.run_mode("pat").returncode, 0)
        r = self.run_mode("apps")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.active(), ["hf-sync"])
        self.assertIn("stop shack-pat-revert.timer", open(self.log).read())

    def test_status_shows_pat(self):
        self.up("mercury", "mercury-connector")
        self.run_mode("pat", "2h")
        r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=self.env, timeout=30)
        self.assertIn("shack mode: pat", r.stdout)
        self.assertIn("back to mercury at", r.stdout)
        self.assertRegex(r.stdout, r"pat \(listening\)\s+active")


class Revert(unittest.TestCase):
    """systemd/shack-pat-revert, run by the timer."""

    def setUp(self):
        self.t = PatMode("test_status_shows_pat")
        self.t.setUp()

    def tearDown(self):
        self.t.tearDown()

    def revert(self):
        return subprocess.run(["sh", REVERT], capture_output=True, text=True, env=self.t.env, timeout=30)

    def test_hands_mercury_back(self):
        self.t.up("mercury", "mercury-connector")
        self.t.run_mode("pat", "1h")
        self.assertEqual(self.revert().returncode, 0)
        self.assertEqual(self.t.active(), ["hf-sync", "mercury", "mercury-connector"])
        self.assertEqual(self.t.state_vals()["MODE"], "mercury")

    def test_leaves_other_modes_alone(self):
        self.t.up("hf-sync")
        with open(self.t.state, "w") as f:
            f.write('MODE=apps\nSINCE=1\nPAUSED="mercury mercury-connector"\n')
        self.revert()
        self.assertEqual(self.t.active(), ["hf-sync"])
        self.assertEqual(self.t.state_vals()["MODE"], "apps")


if __name__ == "__main__":
    unittest.main()
