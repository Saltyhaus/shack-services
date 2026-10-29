"""
Who owns the FT-991A right now, and switching it -- the GUI side of the
`shack-mode` command (/usr/local/bin/shack-mode). Kept separate from the GUI
so the state logic can be unit-tested without Tk.

Reading the state needs no root: /run/shack-mode is world-readable, process
names come from /proc, and modem73 is a --user unit. Switching runs the real
`shack-mode` command, which does all the safety checks; it asks for the
password through shack_askpass.py (sudo -A), once per switch.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess
from typing import List, Optional, Tuple

STATE_FILE = "/run/shack-mode"
SHACK_MODE = "/usr/local/bin/shack-mode"
OUTBOX = os.path.expanduser("~/hf_spool/outgoing")
# The GTK app's in-window password dialog (falls back to shack_askpass.py
# when there's no app socket, i.e. the old Tk window).
ASKPASS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shack_askpass_client.py")
MODEM73_UNIT = "modem73.service"
APP_NAMES = ("wsjtx", "jt9", "js8call", "js8")   # programs that use the radio
MODES = ("mercury", "modem73", "apps")
MODE_LABEL = {"mercury": "MERCURY", "modem73": "MODEM73", "apps": "APPS"}
MODE_TEXT = {
    "mercury": "Mercury -- HF mail gateway",
    "modem73": "modem73 -- Reticulum on HF",
    "apps": "free for WSJT-X / JS8Call",
    "none": "nobody -- radio idle",
    "conflict": "MORE THAN ONE program can key it",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclasses.dataclass
class StateFile:
    mode: Optional[str]
    since: Optional[datetime.datetime]
    paused: List[str]


@dataclasses.dataclass
class RadioState:
    owner: str                    # mercury / modem73 / apps / none / conflict
    owners: List[str]             # every program that currently has a claim
    mode: Optional[str]           # what shack-mode last set (None = boot default)
    since: Optional[datetime.datetime]
    paused: List[str]             # e.g. ["mercury", "mercury-connector"]
    apps: List[str]               # running WSJT-X / JS8Call process names
    outbox: List[str]             # files waiting to transmit (relative paths)


def parse_state_file(text: str) -> StateFile:
    values = {}
    for line in text.splitlines():
        key, sep, val = line.partition("=")
        if sep:
            values[key.strip()] = val.strip().strip('"')
    since = None
    if values.get("SINCE", "").isdigit():
        since = datetime.datetime.fromtimestamp(int(values["SINCE"]))
    return StateFile(values.get("MODE") or None, since, values.get("PAUSED", "").split())


def read_state_file(path: str = STATE_FILE) -> StateFile:
    try:
        with open(path) as f:
            return parse_state_file(f.read())
    except OSError:
        return StateFile(None, None, [])


def apps_running(proc: str = "/proc") -> List[str]:
    found = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name.lower() in APP_NAMES:
            found.add(name)
    return sorted(found)


def user_unit_active(unit: str = MODEM73_UNIT) -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def outbox_files(root: str = OUTBOX) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.startswith("."):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)


def decide_owner(mercury_active: bool, modem73_active: bool, apps: List[str]) -> Tuple[str, List[str]]:
    owners = []
    if mercury_active:
        owners.append("mercury")
    if modem73_active:
        owners.append("modem73")
    if apps:
        owners.append("apps")
    if len(owners) > 1:
        return "conflict", owners
    return (owners[0] if owners else "none"), owners


def radio_state(mercury_active: bool, state: Optional[StateFile] = None) -> RadioState:
    state = state or read_state_file()
    apps = apps_running()
    owner, owners = decide_owner(mercury_active, user_unit_active(), apps)
    # Mercury stopped on purpose for apps mode, and nothing open yet: the
    # radio is still "apps" -- reserved for WSJT-X / JS8Call.
    if owner == "none" and state.mode == "apps":
        owner = "apps"
    return RadioState(owner, owners, state.mode, state.since, state.paused, apps, outbox_files())


def paused_units(rs: RadioState) -> List[str]:
    """systemd unit names that are stopped on purpose right now."""
    if rs.mode not in ("apps", "modem73"):
        return []
    return [p if p.endswith(".service") else p + ".service" for p in rs.paused]


@dataclasses.dataclass
class SwitchResult:
    success: bool
    message: str


def switch(mode: str, timeout: float = 120.0, extra_env: Optional[dict] = None) -> SwitchResult:
    """Run `shack-mode -y <mode>` under a pseudo-terminal (`script`), so sudo
    asks for the password once per switch, through the GUI askpass window.
    The GUI does the outbox / open-apps questions itself before calling."""
    if mode not in MODES:
        return SwitchResult(False, f"unknown mode {mode!r}")
    if not os.access(SHACK_MODE, os.X_OK):
        return SwitchResult(False, f"{SHACK_MODE} isn't installed")
    if not os.access(ASKPASS, os.X_OK):
        return SwitchResult(False, f"{ASKPASS} isn't executable (chmod +x it)")
    env = dict(os.environ, SUDO_ASKPASS=ASKPASS, SHACK_MODE_ASKPASS="1", **(extra_env or {}))
    try:
        r = subprocess.run(
            ["script", "-qec", f"{SHACK_MODE} -y {mode}", "/dev/null"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return SwitchResult(False, "timed out -- check `shack-mode` in a terminal")
    except Exception as e:
        return SwitchResult(False, str(e))
    text = _ANSI.sub("", (r.stdout or "") + (r.stderr or "")).replace("\r", "").strip()
    if r.returncode == 0:
        return SwitchResult(True, text)
    if re.search(r"no password was provided|incorrect password|a password is required|askpass", text, re.I):
        return SwitchResult(False, "authentication cancelled or wrong password -- nothing was changed")
    return SwitchResult(False, text or f"shack-mode failed (exit {r.returncode})")
