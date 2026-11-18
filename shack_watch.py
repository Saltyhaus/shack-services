#!/usr/bin/env python3
"""
shack-watch: make sure the right program holds Mercury, and tell your phone
when it doesn't.

Mercury 1.9.24+ serves one program at a time. In MERCURY mode that must be
mercury-connector (HF Contact, HF mail); in PAT mode, Pat listening
(pat-listen.service). If anything else gets there first -- say another Pat
pointed at Mercury, the moment Pat mode ends -- the connector is refused and
restarts every few seconds, and the station is quietly off the air.

  shack-watch check-handback [--wait 45]   after a switch: wait for the right
                                           holder; push an alert if it's wrong
  shack-watch watch                        from a timer every 2 min: alert once
                                           after 10 min off the air, again when
                                           it's back
  shack-watch status                       who holds Mercury right now

Runs as root (it needs `ss -p` to see other users' programs). Push goes to
the ntfy topic in /etc/shack-services/alerts.env (NTFY_URL=, optional
NTFY_TOKEN=, NTFY_CLICK=); without it, alerts only go to the journal.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8300
STATE = Path(os.environ.get("SHACK_MODE_FILE", "/run/shack-mode"))
WATCH_STATE = Path(os.environ.get("SHACK_WATCH_STATE", "/run/shack-watch.json"))
ALERTS_ENV = Path(os.environ.get("SHACK_ALERTS_ENV", "/etc/shack-services/alerts.env"))
PROC = os.environ.get("SHACK_PROC", "/proc")
GRACE_S = 600                 # off the air this long before the watch alerts
PAT_UNIT = "pat-listen"
CONNECTOR_UNIT = "mercury-connector"

SS_RE = re.compile(r'\S+:(\d+)\s+\S+:(\d+)\s+users:\(\("([^"]+)",pid=(\d+)')


# ------------------------------------------------------------ facts

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def holders(port=PORT, ss_out=None):
    """[(name, pid)] of programs connected to Mercury's control port."""
    if ss_out is None:
        ss_out = run(["ss", "-tnpH", "state", "established"])
    found = []
    for line in ss_out.splitlines():
        m = SS_RE.search(line)
        if m and int(m.group(2)) == port:               # their far end is Mercury
            found.append((m.group(3), int(m.group(4))))
    return sorted(set(found), key=lambda x: x[1])


def unit_of(pid, proc=None):
    """systemd unit a pid runs in, e.g. pat@ag7jc.service, or ''."""
    try:
        for line in Path(proc or PROC, str(pid), "cgroup").read_text().splitlines():
            m = re.search(r"/([^/]+\.(?:service|scope))$", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def main_pid(unit):
    out = run(["systemctl", "show", "-p", "MainPID", "--value", unit]).strip()
    return int(out) if out.isdigit() else 0


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def restarts(unit):
    out = run(["systemctl", "show", "-p", "NRestarts", "--value", unit]).strip()
    return int(out) if out.isdigit() else 0


def mode(path=None):
    try:
        for line in Path(path or STATE).read_text().splitlines():
            if line.startswith("MODE="):
                return line[5:].strip().strip('"') or "mercury"
    except OSError:
        pass
    return "mercury"                 # no state file: boot default


def expected(mode_):
    """(unit that should hold Mercury, short name) or None if Mercury isn't
    meant to be serving anyone in this mode."""
    return {"mercury": (CONNECTOR_UNIT, "mercury-connector"), "pat": (PAT_UNIT, "Pat")}.get(mode_)


def describe(hs, proc=None):
    if not hs:
        return "nothing"
    parts = []
    for name, pid in hs:
        u = unit_of(pid, proc)
        parts.append(f"{name} (pid {pid}{', ' + u if u else ''})")
    return ", ".join(parts)


def check(mode_=None, ss_out=None, proc=None):
    """(ok, text). ok is None when Mercury isn't meant to serve anyone."""
    mode_ = mode_ or mode()
    want = expected(mode_)
    if not want:
        return None, f"mode {mode_}: Mercury isn't in use"
    unit, nice = want
    if not active("mercury"):
        return False, f"{mode_.upper()} mode, but Mercury isn't running"
    hs = holders(ss_out=ss_out)
    pid = main_pid(unit)
    if pid and any(p == pid for _, p in hs):
        return True, f"{nice} has Mercury"
    if not hs:
        why = f"{nice} isn't connected"
        if unit == CONNECTOR_UNIT:
            why += f" ({restarts(unit)} restarts so far)"
        return False, f"Mercury is free but {why}"
    return False, f"Mercury is held by {describe(hs, proc)}, not {nice}"


# ------------------------------------------------------------ push

def load_env(path=None):
    s = {}
    try:
        for line in Path(path or ALERTS_ENV).read_text().splitlines():
            line = line.split(" #", 1)[0].strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                s[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return s


def push(title, body, high=True, env=None):
    env = load_env() if env is None else env
    url = env.get("NTFY_URL", "").strip()
    log(f"{title}: {body}")
    if not url:
        return False
    headers = {"Title": title.encode("ascii", "replace").decode(),
               "Tags": "rotating_light" if high else "white_check_mark",
               "Priority": "high" if high else "default"}
    if env.get("NTFY_CLICK"):
        headers["Click"] = env["NTFY_CLICK"]
    if env.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + env["NTFY_TOKEN"]
    try:
        req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log(f"  push failed: {e}")
        return False


def log(msg):
    print(msg, flush=True)
    subprocess.run(["logger", "-t", "shack-watch", msg], capture_output=True)


# ------------------------------------------------------------ commands

def check_handback(wait=45, poll=1.0):
    t0 = time.monotonic()
    while True:
        ok, text = check()
        if ok is None or ok:
            log(text)
            return 0
        if time.monotonic() - t0 >= wait:
            push(f"Switch to {mode().upper()} failed", text +
                 ". The station is off the air until that's fixed.")
            return 1
        time.sleep(poll)


def watch(now=None, state_path=None):
    state_path = state_path or WATCH_STATE
    now = time.time() if now is None else now
    try:
        st = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        st = {}
    ok, text = check()
    if ok is None or ok:
        if st.get("alerted"):
            push("HF station back on the air", text, high=False)
        st = {}
    else:
        st.setdefault("bad_since", now)
        if not st.get("alerted") and now - st["bad_since"] >= GRACE_S:
            mins = int((now - st["bad_since"]) // 60)
            push("HF station off the air", f"{text} (for {mins} min).")
            st["alerted"] = True
    Path(state_path).write_text(json.dumps(st))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Make sure the right program holds Mercury.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("check-handback")
    h.add_argument("--wait", type=float, default=45)
    sub.add_parser("watch")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "check-handback":
        return check_handback(a.wait)
    if a.cmd == "watch":
        return watch()
    ok, text = check()
    print(text)
    return 0 if ok in (None, True) else 1


if __name__ == "__main__":
    sys.exit(main())
