#!/usr/bin/env python3
"""
radio-tune: QSY a shack radio and give its tuner a steady low-power carrier.

  radio_tune.py ft991a 7.1065          # MHz, kHz (7106.5) or Hz all work
  radio_tune.py kx3 14.105 --secs 8
  radio_tune.py ft991a 7.1065 --dry-run

What it does, in order:
  1. checks the frequency is inside a US amateur HF/6 m band
  2. KX3 only: needs DN-AUTO Monitor's live status, so it can watch the amp
  3. stops whatever holds the radio's sound card (FT-991A: Mercury, and the
     connector or Pat mode on it), remembering what was running
  4. QSY, tune mode, power to --watts (10 W), read back and refuses to key if
     the radio says more than half again that
  5. starts the tone into the sound card first, keys only once it's playing
  6. while keyed: prints PTT, ALC, power and SWR every second; KX3: unkeys
     at once if the amp reports more than --amp-max-w forward power
  7. always: unkeys, puts power back as it was, restores the mode (KX3) or
     leaves the tune mode (FT-991A data radio), restarts what it stopped --
     even on Ctrl-C, an error or a dropped SSH session

Settings per radio can be changed in ~/.config/shack-services/tune.json, e.g.
  {"kx3": {"device": "plughw:CARD=Device_1,DEV=0", "level": 0.2}}
"""

import argparse
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

RADIOS = {
    "ft991a": {
        "name": "FT-991A",
        "rigctld": "127.0.0.1:4532",
        "device": "plughw:CARD=CODEC,DEV=0",    # same as Mercury's -o
        "mode": "PKTUSB",
        "keep_mode": True,                       # data radio: stays in DATA-USB
        "units": ["mercury-connector", "pat-listen", "mercury"],   # users first, Mercury last
        "listen_port": 8300,                     # Mercury is up when this listens
        "amp": False,
        "level": 0.15,
        "max_watts": 100,
    },
    "kx3": {
        "name": "KX3",
        "rigctld": "127.0.0.1:4534",
        "device": "plughw:CARD=Device,DEV=0",    # Digirig (CM108)
        "mode": "PKTUSB",                        # DATA A: audio in from the Digirig
        "keep_mode": False,                      # back to whatever it was (voice)
        "units": [],
        "listen_port": None,
        "amp": True,                             # drives the LDMOS amp
        "level": 0.15,
        "max_watts": 15,
    },
}
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shack-services" / "tune.json"
AMP_STATUS = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "dnauto-monitor" / "status.json"

# US amateur allocations, kHz (60 m: the channel span)
BANDS = [(1800, 2000), (3500, 4000), (5330.5, 5406.5), (7000, 7300), (10100, 10150), (14000, 14350),
         (18068, 18168), (21000, 21450), (24890, 24990), (28000, 29700), (50000, 54000)]


class TuneError(Exception):
    pass


def radio_settings(radio, config=None):
    if radio not in RADIOS:
        raise TuneError(f"unknown radio {radio!r} (use {', '.join(RADIOS)})")
    s = dict(RADIOS[radio])
    try:
        s.update(json.loads(Path(config or CONFIG).read_text()).get(radio, {}))
    except (OSError, ValueError):
        pass
    return s


def parse_freq(text):
    """'7.1065' (MHz), '7106.5' (kHz) or '7106500' (Hz) -> Hz."""
    try:
        v = float(str(text).strip().lower().replace("mhz", "").replace("khz", "").replace(",", ""))
    except ValueError:
        raise TuneError(f"can't read {text!r} as a frequency") from None
    hz = v * 1e6 if v < 100 else v * 1e3 if v < 100000 else v
    return int(round(hz))


def in_band(hz):
    khz = hz / 1000
    return any(lo <= khz <= hi for lo, hi in BANDS)


def amp_forward(path=None, now=None, max_age=10):
    """Amp forward watts from DN-AUTO Monitor, or None if it isn't live."""
    try:
        d = json.loads(Path(path or AMP_STATUS).read_text())
    except (OSError, ValueError):
        return None
    if (now or time.time()) - float(d.get("time", 0)) > max_age or not d.get("linked"):
        return None
    try:
        return float(d.get("for_w") or 0)
    except (TypeError, ValueError):
        return 0.0


def say(*args, **kw):
    """print, but never fail: after an SSH drop the clean-up must still finish."""
    try:
        print(*args, **kw, flush=True)
    except (OSError, ValueError):
        pass


class Rig:
    def __init__(self, addr):
        host, port = addr.rsplit(":", 1)
        try:
            self.s = socket.create_connection((host, int(port)), timeout=5)
        except OSError as e:
            raise TuneError(f"can't reach rigctld at {addr}: {e}") from None
        self.f = self.s.makefile("r")

    def cmd(self, line):
        self.s.sendall((line + "\n").encode())
        if line[0].isupper():                     # set commands answer RPRT n
            r = self.f.readline().strip()
            if r != "RPRT 0":
                raise TuneError(f"rigctld refused '{line}': {r}")
            return None
        if line.startswith("m"):                 # mode + passband
            return self.f.readline().strip(), self.f.readline().strip()
        r = self.f.readline().strip()
        if r.startswith("RPRT -"):
            raise TuneError(f"rigctld: '{line}' -> {r}")
        return r

    def try_cmd(self, line, default="?"):
        try:
            return self.cmd(line)
        except Exception:
            return default

    def watts_to_level(self, watts, hz, mode, max_watts):
        """Hamlib's own conversion for this radio; plain fraction of max
        power if the backend can't do it."""
        try:
            return float(self.cmd(f"\\mW2power {int(watts * 1000)} {hz} {mode}"))
        except (TuneError, ValueError):
            return watts / max_watts

    def level_to_watts(self, level, hz, mode, max_watts):
        try:
            return int(self.cmd(f"\\power2mW {level} {hz} {mode}")) / 1000
        except (TuneError, ValueError):
            return level * max_watts


def active_units(units):
    return [u for u in units if subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0]


def sudo(*cmd):
    pre = ["sudo", "-A"] if os.environ.get("SUDO_ASKPASS") else ["sudo"]
    r = subprocess.run([*pre, *cmd], capture_output=True, text=True)
    if r.returncode:
        raise TuneError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip() or 'failed'}")


def listening(port, tables=("/proc/net/tcp", "/proc/net/tcp6")):
    for t in tables:
        try:
            rows = Path(t).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            p = row.split()
            if len(p) > 3 and p[3] == "0A":
                try:
                    if int(p[1].rsplit(":", 1)[1], 16) == port:
                        return True
                except (IndexError, ValueError):
                    pass
    return False


def make_tone(path, secs, hz, level):
    rate = 48000
    amp = int(32767 * max(0.0, min(level, 1.0)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * hz * i / rate)))
                               for i in range(int(rate * secs))))


def tune(radio, hz, secs=10, watts=10, level=None, tone_hz=1500, dry_run=False,
         amp_watchdog=True, amp_max_w=50, config=None):
    s = radio_settings(radio, config or CONFIG)
    name = s["name"]
    if not in_band(hz):
        raise TuneError(f"{hz / 1e6:.4f} MHz isn't inside a US amateur band -- not transmitting there")
    if not 1 <= secs <= 30:
        raise TuneError("tune time must be 1-30 seconds")
    if not 1 <= watts <= min(20, s["max_watts"]):
        raise TuneError(f"tune power must be 1-{min(20, s['max_watts'])} W")
    level = s["level"] if level is None else level

    watch = s["amp"] and amp_watchdog
    if watch and amp_forward() is None:
        raise TuneError("DN-AUTO Monitor isn't running (or can't see the amp), so the amp can't be "
                        "watched. Start it, or tune without the watchdog (--no-amp-watchdog) once "
                        "you're sure the amp is in STANDBY.")

    rig = Rig(s["rigctld"])
    old_freq = int(float(rig.cmd("f")))
    old_mode, old_pb = rig.cmd("m")
    old_level = rig.cmd("l RFPOWER")
    was = active_units(s["units"])
    say(f"{name} now: {old_freq / 1e6:.4f} MHz {old_mode}, power level {old_level}"
        + (f"; stopping for the tune: {', '.join(was)}" if was else ""))
    if rig.cmd("t") not in ("0", ""):
        raise TuneError(f"the {name} is transmitting right now -- not touching it")
    if dry_run:
        say(f"would: {hz / 1e6:.4f} MHz {s['mode']}, {watts} W, {secs:.0f} s tone into {s['device']}"
            + (", amp watchdog on" if watch else ""))
        return {"ok": True, "dry_run": True}

    if was:
        sudo("-v")                                 # password first, before anything changes

    def bail(sig, _f):
        raise KeyboardInterrupt(f"signal {sig}")
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, bail)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    make_tone(tmp.name, secs + 0.5, tone_hz, level)
    play, result = None, {"ok": False, "swr": [], "watts": [], "stopped_by": None}
    try:
        if was:
            say(f"stopping {', '.join(was)} (it holds the sound card)")
            sudo("systemctl", "stop", *was)
            time.sleep(1)
        rig.cmd(f"F {hz}")
        rig.cmd(f"M {s['mode']} 3000")
        want = rig.watts_to_level(watts, hz, s["mode"], s["max_watts"])
        rig.cmd(f"L RFPOWER {want:.3f}")
        got = rig.level_to_watts(float(rig.cmd("l RFPOWER")), hz, s["mode"], s["max_watts"])
        if got > watts * 1.5:
            raise TuneError(f"asked for {watts} W but the {name} reports {got:.0f} W -- not keying")
        if watch:
            fw = amp_forward()
            if fw is None or fw > amp_max_w:
                raise TuneError("amp status went away or shows output before keying -- not keying")

        play = subprocess.Popen(["aplay", "-q", "-D", s["device"], tmp.name],
                                stderr=subprocess.PIPE, text=True)
        time.sleep(0.4)
        if play.poll() is not None:
            raise TuneError(f"couldn't play into {s['device']}: {play.stderr.read().strip()} "
                            "(another program using the sound card?)")
        say(f"{hz / 1e6:.4f} MHz {s['mode']}, {got:.0f} W -- keying for {secs:.0f} s")
        rig.cmd("T 1")
        end = time.monotonic() + secs
        nxt = 0
        while time.monotonic() < end and play.poll() is None:
            if watch:
                fw = amp_forward(max_age=5)
                if fw is None or fw > amp_max_w:
                    result["stopped_by"] = ("amp status stopped updating" if fw is None
                                            else f"amp shows {fw:.0f} W forward -- is it in OPERATE?")
                    say(f"!! {result['stopped_by']} -- unkeying")
                    break
            if time.monotonic() >= nxt:
                nxt = time.monotonic() + 1
                swr, po = rig.try_cmd("l SWR"), rig.try_cmd("l RFPOWER_METER_WATTS")
                say(f"  PTT {rig.try_cmd('t')}  ALC {rig.try_cmd('l ALC')}  {po} W  SWR {swr}")
                for k, v in (("swr", swr), ("watts", po)):
                    try:
                        result[k].append(float(v))
                    except ValueError:
                        pass
            time.sleep(0.25)
        result["ok"] = result["stopped_by"] is None
    finally:
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)     # finish the clean-up whatever happens
        if play and play.poll() is None:
            play.kill()
        steps = ["T 0", f"L RFPOWER {old_level}"]
        if not s["keep_mode"]:
            steps.append(f"M {old_mode} {old_pb}")
        for step in steps:
            for _ in range(3):
                try:
                    rig.cmd(step)
                    break
                except Exception as e:
                    say(f"retrying '{step}': {e}", file=sys.stderr)
                    time.sleep(0.5)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        say(f"unkeyed (PTT {rig.try_cmd('t')}), power level {rig.try_cmd('l RFPOWER')}, "
            f"{int(float(rig.try_cmd('f', '0'))) / 1e6:.4f} MHz {rig.try_cmd('m', ('?',))[0]}")
        if was:
            for u in reversed(was):               # Mercury first, then what uses it
                try:
                    sudo("systemctl", "start", u)
                except TuneError as e:
                    say(f"could not start {u}: {e} -- start it by hand: sudo systemctl start {u}",
                        file=sys.stderr)
                if u == "mercury" and s.get("listen_port"):
                    t0 = time.monotonic()           # Pat/connector need Mercury listening first
                    while time.monotonic() - t0 < 20 and not listening(s["listen_port"]):
                        time.sleep(0.5)
            time.sleep(1)
            say(f"running again: {', '.join(active_units(s['units'])) or 'nothing'}")
    loaded = [w for w in result["watts"] if w > 0.5]
    swrs = [x for x, w in zip(result["swr"], result["watts"]) if w > 0.5] or result["swr"]
    if loaded:
        result["summary"] = f"SWR {swrs[-1]:.1f} at {loaded[-1]:.0f} W (first {swrs[0]:.1f})"
    else:
        result["summary"] = "no power out seen -- check the tone level and the radio's DATA input"
    say(("TUNED: " if result["ok"] and loaded else "TUNE: ") + result["summary"])
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="QSY and tune: a low-power tone for the ATU.")
    ap.add_argument("radio", choices=list(RADIOS))
    ap.add_argument("freq", help="MHz (7.1065), kHz (7106.5) or Hz")
    ap.add_argument("--secs", type=float, default=10)
    ap.add_argument("--watts", type=int, default=10)
    ap.add_argument("--level", type=float, help="tone level 0-1 (default from the radio's settings)")
    ap.add_argument("--hz", type=float, default=1500, help="tone pitch")
    ap.add_argument("--no-amp-watchdog", action="store_true",
                    help="KX3: tune even if DN-AUTO Monitor can't watch the amp")
    ap.add_argument("--amp-max-w", type=float, default=50)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = tune(a.radio, parse_freq(a.freq), secs=a.secs, watts=a.watts, level=a.level, tone_hz=a.hz,
                 dry_run=a.dry_run, amp_watchdog=not a.no_amp_watchdog, amp_max_w=a.amp_max_w,
                 config=CONFIG)
    except TuneError as e:
        say(f"radio-tune: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
