#!/usr/bin/env python3
"""
hf-alerts -- tells you when someone calls your HF Contact station.

Watches mercury-connector's events file (-e) and, for every guest who runs
an echo test or leaves a message:

  * a desktop notification
  * a sound: the caller's callsign in CW for a message, a short chirp for
    an echo test
  * a push to your phone through ntfy (https://ntfy.sh, or your own server)

Your own stations (the connector's peers) never alert -- the connector only
writes callsigns for guests.

    hf-alerts                    run (as a user service, see hf-alerts.service)
    hf-alerts --test message K1ABC   fire a fake message alert, all channels
    hf-alerts --test echo K1ABC      fire a fake echo-test alert
    hf-alerts --sinks            list sound outputs and which ones are refused

Settings: ~/.config/hf-alerts/hf-alerts.env (KEY=value, see
hf-alerts.env.example). Everything has a default; with no file at all you
get desktop notifications and sound, and no push.

The sound never goes to a radio. A radio's USB sound card is its
transmit audio: anything played there can key it through VOX or go out
on the air. Outputs matching RADIO_SINKS are refused, and if the chosen
output is one of them there is no sound at all, only a log line.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hf-alerts" / "hf-alerts.env"

DEFAULTS = {
    "EVENTS_FILE": str(Path.home() / "hf_spool" / "events.jsonl"),
    # what each kind of event does: any of desktop, sound, push
    "NOTIFY_MESSAGE": "desktop,sound,push",
    "NOTIFY_ECHO": "desktop,sound",
    # ntfy topic URL, e.g. https://ntfy.sh/some-long-random-name -- empty = no push
    "NTFY_URL": "",
    "NTFY_TOKEN": "",          # for a server with access control
    "NTFY_CLICK": "",          # opened when you tap the push, e.g. your HF dashboard
    # sound
    "SOUND_SINK": "",          # empty = the desktop's default output
    "RADIO_SINKS": "CODEC,C-Media,Burr-Brown,Digirig",   # never play here
    "QUIET_HOURS": "22-7",     # no sound between these hours (desktop/push still go); empty = never quiet
    "CW_WPM": "20",
    "CW_HZ": "650",
    "VOLUME": "0.5",           # 0..1
}

CALL_RE = re.compile(r"^[A-Z0-9]{3,10}(-[0-9]{1,2})?$")


# ------------------------------------------------------------ settings

def load_settings(path=CONFIG):
    s = dict(DEFAULTS)
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                s[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return s


def channels(s, kind):
    return {c.strip() for c in s.get("NOTIFY_" + kind.upper(), "").split(",") if c.strip()}


def in_quiet_hours(spec, hour):
    """'22-7' -> quiet from 22:00 to 06:59. Empty or malformed: never quiet."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return False
    a, b = int(m.group(1)) % 24, int(m.group(2)) % 24
    if a == b:
        return False
    return a <= hour < b if a < b else hour >= a or hour < b


# ------------------------------------------------------------ events

def parse_event(line):
    """A guest event worth alerting on, or None. {'kind', 'call', 't', 'snr', 'bps'}"""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("event") not in ("echo", "message"):
        return None
    call = str(e.get("call", "")).upper()
    if not CALL_RE.match(call):
        return None
    t = e.get("t") if isinstance(e.get("t"), int) else int(time.time())
    snr, bps = e.get("snr"), e.get("bps")
    return {"kind": e["event"], "call": call, "t": t,
            "snr": float(snr) if isinstance(snr, (int, float)) and not isinstance(snr, bool) else None,
            "bps": bps if isinstance(bps, int) and not isinstance(bps, bool) else None}


def describe(ev):
    """(title, body) for an event."""
    at = datetime.fromtimestamp(ev["t"]).strftime("%H:%M")
    if ev["kind"] == "message":
        return (f"HF message from {ev['call']}",
                f"{ev['call']} left a message over HF Contact at {at}. It's in your HF inbox.")
    sig = []
    if ev["snr"] is not None:
        sig.append(f"SNR {ev['snr']:+.1f} dB")
    if ev["bps"] is not None:
        sig.append(f"{ev['bps']} bps")
    return (f"Echo test: {ev['call']}",
            f"{ev['call']} ran an echo test at {at}" + (f" -- {', '.join(sig)}" if sig else "") + ".")


class Tail:
    """Follows a JSON-lines file from its current end. Survives the file
    being rotated, truncated, deleted and recreated."""

    def __init__(self, path):
        self.path = Path(path)
        self.inode = None
        self.pos = 0
        self.partial = ""
        try:
            st = self.path.stat()
            self.inode, self.pos = st.st_ino, st.st_size     # old events don't alert
        except OSError:
            pass

    def read(self):
        try:
            st = self.path.stat()
        except OSError:
            return []
        if st.st_ino != self.inode or st.st_size < self.pos:
            self.inode, self.pos, self.partial = st.st_ino, 0, ""   # new file: read it all
        if st.st_size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read(1 << 20)
        self.pos += len(data)
        text = self.partial + data.decode("utf-8", "replace")
        lines = text.split("\n")
        self.partial = lines.pop()          # an unfinished last line waits for the rest
        return [l for l in lines if l.strip()]


# ------------------------------------------------------------ desktop

def desktop_notify(title, body, urgent=False):
    if shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "HF Contact", "-i", "network-wireless-symbolic",
               "-u", "critical" if urgent else "normal", title, body]
    elif shutil.which("gdbus"):
        cmd = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
               "--object-path", "/org/freedesktop/Notifications",
               "--method", "org.freedesktop.Notifications.Notify",
               "HF Contact", "0", "network-wireless-symbolic", title, body, "[]", "{}", "10000"]
    else:
        return False, "no notify-send or gdbus"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


# ------------------------------------------------------------ sound

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
    "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
    "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "-": "-....-",
}


def cw_timing(text, wpm=20):
    """[(on, seconds)] for text in Morse, PARIS timing."""
    dit = 1.2 / wpm
    out = []
    for wi, word in enumerate(text.upper().split()):
        if wi:
            out.append((False, 7 * dit))
        for ci, ch in enumerate(word):
            code = MORSE.get(ch)
            if not code:
                continue
            if ci:
                out.append((False, 3 * dit))
            for ei, el in enumerate(code):
                if ei:
                    out.append((False, dit))
                out.append((True, dit if el == "." else 3 * dit))
    return out


def chirp_timing():
    """Echo test: two short rising notes."""
    return [(660, 0.09), (0, 0.05), (880, 0.14)]


def render_wav(parts, rate=22050, hz=650, volume=0.5):
    """parts: [(on/False or freq, seconds)] -> WAV bytes. 5 ms ramps, no clicks."""
    frames = bytearray()
    ramp = int(rate * 0.005)
    for tone, dur in parts:
        n = int(rate * dur)
        f = hz if tone is True else (tone or 0)
        for i in range(n):
            if not f:
                frames += b"\x00\x00"
                continue
            env = min(1.0, i / ramp, (n - 1 - i) / ramp) if ramp else 1.0
            v = int(32767 * volume * env * math.sin(2 * math.pi * f * i / rate))
            frames += struct.pack("<h", v)
    frames += b"\x00\x00" * int(rate * 0.1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def list_sinks():
    """[(name, description)] from PipeWire/PulseAudio."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sinks"], capture_output=True,
                             text=True, timeout=5).stdout
        return [(s.get("name", ""), s.get("description", "")) for s in json.loads(out or "[]")]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:   # older pactl without -f json
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                             text=True, timeout=5).stdout
        return [(l.split("\t")[1], "") for l in out.splitlines() if "\t" in l]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return []


def default_sink():
    try:
        return subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_radio_sink(name, desc, patterns):
    hay = f"{name} {desc}".lower()
    return any(p.strip().lower() in hay for p in patterns.split(",") if p.strip())


def choose_sink(s, sinks=None, default=None):
    """(sink name, None) to play on, or (None, reason)."""
    sinks = list_sinks() if sinks is None else sinks
    want = s.get("SOUND_SINK") or (default_sink() if default is None else default)
    if not want:
        return None, "no sound output found"
    desc = dict(sinks).get(want, "")
    if is_radio_sink(want, desc, s.get("RADIO_SINKS", "")):
        return None, f"refusing to play on {want} -- that looks like a radio's sound card (RADIO_SINKS)"
    return want, None


def play(wav, sink):
    player = shutil.which("paplay") or shutil.which("pw-play")
    if not player:
        return False, "no paplay or pw-play"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
        path = f.name
    try:
        flag = "--device=" if player.endswith("paplay") else "--target="
        r = subprocess.run([player, flag + sink, path], capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    finally:
        os.unlink(path)


def sound_for(ev, s):
    vol = max(0.0, min(1.0, float(s.get("VOLUME") or 0.5)))
    if ev["kind"] == "message":
        return render_wav(cw_timing(ev["call"], int(s.get("CW_WPM") or 20)),
                          hz=int(s.get("CW_HZ") or 650), volume=vol)
    return render_wav(chirp_timing(), volume=vol)


# ------------------------------------------------------------ push

def push_request(s, title, body, kind):
    """The urllib Request for ntfy, or None when push is off."""
    url = (s.get("NTFY_URL") or "").strip()
    if not url:
        return None
    headers = {
        "Title": title.encode("ascii", "replace").decode(),     # HTTP headers: plain ASCII
        "Tags": "radio" if kind == "message" else "satellite",
        "Priority": "high" if kind == "message" else "low",
    }
    if s.get("NTFY_CLICK"):
        headers["Click"] = s["NTFY_CLICK"]
    if s.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + s["NTFY_TOKEN"]
    return urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")


def push(s, title, body, kind):
    req = push_request(s, title, body, kind)
    if req is None:
        return False, "NTFY_URL not set"
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except Exception as e:                      # no network etc.: log it, keep watching
        return False, str(e)


# ------------------------------------------------------------ main

def log(msg):
    print(msg, flush=True)


def alert(ev, s, now_hour=None):
    title, body = describe(ev)
    want = channels(s, ev["kind"])
    log(f"{ev['kind']} from {ev['call']}: {', '.join(sorted(want)) or 'nothing to do'}")
    if "desktop" in want:
        ok, err = desktop_notify(title, body, urgent=False)
        if not ok:
            log(f"  desktop: {err}")
    if "sound" in want:
        hour = datetime.now().hour if now_hour is None else now_hour
        if in_quiet_hours(s.get("QUIET_HOURS", ""), hour):
            log("  sound: quiet hours")
        else:
            sink, why = choose_sink(s)
            if not sink:
                log(f"  sound: {why}")
            else:
                ok, err = play(sound_for(ev, s), sink)
                if not ok:
                    log(f"  sound: {err}")
    if "push" in want:
        ok, info = push(s, title, body, ev["kind"])
        log(f"  push: {'sent' if ok else 'failed'} ({info})")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Alerts for HF Contact guests (echo tests and messages).")
    ap.add_argument("--test", nargs=2, metavar=("KIND", "CALL"), help="fire one alert: message|echo CALLSIGN")
    ap.add_argument("--sinks", action="store_true", help="list sound outputs and which are refused")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    s = load_settings(a.config)

    if a.sinks:
        default = default_sink()
        for name, desc in list_sinks():
            tag = "REFUSED (radio)" if is_radio_sink(name, desc, s["RADIO_SINKS"]) else "ok"
            mark = "*" if name == default else " "
            print(f"{mark} {tag:16} {name}  {desc}")
        sink, why = choose_sink(s)
        print(f"\nalerts play on: {sink or '(nothing) -- ' + why}")
        return 0

    if a.test:
        kind, call = a.test[0].lower(), a.test[1].upper()
        if kind not in ("message", "echo"):
            ap.error("KIND is message or echo")
        alert({"kind": kind, "call": call, "t": int(time.time()), "snr": -1.0, "bps": 68}, s,
              now_hour=12)                              # a test always makes a sound
        return 0

    tail = Tail(s["EVENTS_FILE"])
    log(f"watching {s['EVENTS_FILE']} -- push {'on' if s.get('NTFY_URL') else 'off'}")
    while True:
        for line in tail.read():
            ev = parse_event(line)
            if ev:
                try:
                    alert(ev, s)
                except Exception as e:          # one bad alert never stops the watcher
                    log(f"  alert failed: {e!r}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
