"""
Queries systemd for the status of a fixed list of services, in one cheap
subprocess call per poll -- no persistent connection, no root, no extra
Python packages. Kept separate from the GUI so the parsing logic can be
unit-tested without a live systemd instance.

Resource cost, for reference: `systemctl show` for ~10 units is a single
short-lived subprocess (a few milliseconds of CPU), same mechanism `systemctl
status` uses under the hood. Polling this every few seconds is negligible
next to everything else already running on this box.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess
from typing import Dict, List, Optional

# What systemctl calls a unit's ActiveState, we bucket into 4 display
# categories -- more useful than plain red/green, since a service you
# stopped on purpose shouldn't look as alarming as one that crashed.
#   "active"                                -> good (green)
#   "failed"                                -> bad (red)
#   "activating" / "deactivating" / "reloading" -> transitioning (amber)
#   "inactive" / anything else / unknown    -> stopped (gray)
GOOD = "good"
BAD = "bad"
TRANSITIONING = "transitioning"
STOPPED = "stopped"

_STATE_CATEGORY = {
    "active": GOOD,
    "failed": BAD,
    "activating": TRANSITIONING,
    "deactivating": TRANSITIONING,
    "reloading": TRANSITIONING,
}

PROPERTIES = ["ActiveState", "SubState", "ActiveEnterTimestamp"]


@dataclasses.dataclass
class ServiceStatus:
    name: str
    active_state: str  # raw systemd value, e.g. "active", "failed", "inactive"
    sub_state: str  # e.g. "running", "dead", "auto-restart"
    since: Optional[datetime.datetime]  # when it entered active_state, if known
    category: str  # GOOD / BAD / TRANSITIONING / STOPPED
    error: Optional[str] = None  # set if this unit's status couldn't be read at all


def _category_for(active_state: str) -> str:
    return _STATE_CATEGORY.get(active_state, STOPPED)


def _parse_timestamp(raw: str) -> Optional[datetime.datetime]:
    """systemd prints timestamps like 'Sun 2026-08-23 03:12:45 PDT', or an
    empty string if the unit has never entered that state. Best-effort
    parse for a human-readable "since" display -- if the format doesn't
    match, return None rather than raise; callers just show "?"."""
    raw = raw.strip()
    if not raw or raw == "n/a":
        return None
    # Drop the trailing timezone abbreviation -- Python's %Z can't reliably
    # parse arbitrary zone names, and we only need a rough elapsed time.
    parts = raw.rsplit(" ", 1)
    candidate = parts[0] if len(parts) == 2 else raw
    for fmt in ("%a %Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def format_since(dt: Optional[datetime.datetime], now: Optional[datetime.datetime] = None) -> str:
    """Human-readable elapsed time, e.g. '3m', '2h 14m', '5d'. '?' if unknown."""
    if dt is None:
        return "?"
    now = now or datetime.datetime.now()
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "?"  # clock skew or a parse mismatch -- don't show nonsense negative time
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        rem_m = mins % 60
        return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"
    return f"{hours // 24}d"


def query_states(services: List[str], timeout: float = 5.0) -> Dict[str, ServiceStatus]:
    """One systemctl call for the whole list. Falls back to marking every
    service 'unknown' (not a crash) if systemctl itself can't be reached."""
    if not services:
        return {}

    try:
        result = subprocess.run(
            ["systemctl", "show"] + [f"-p{p}" for p in PROPERTIES] + services,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as e:
        return {s: ServiceStatus(s, "unknown", "unknown", None, STOPPED, error=str(e)) for s in services}

    lines = [ln for ln in result.stdout.split("\n") if ln.strip()]
    n = len(PROPERTIES)

    # systemctl show always emits exactly `n` lines per unit, even for an
    # unknown unit name -- a count that isn't an exact multiple means the
    # output itself is off, and there's no safe way to realign it.
    if len(lines) != n * len(services):
        return {s: ServiceStatus(s, "unknown", "unknown", None, STOPPED,
                                 error="unexpected systemctl output shape") for s in services}

    statuses: Dict[str, ServiceStatus] = {}
    for i, name in enumerate(services):
        values = {}
        for line in lines[i * n:(i + 1) * n]:
            key, sep, val = line.partition("=")
            if sep:
                values[key] = val
        active_state = values.get("ActiveState", "unknown")
        statuses[name] = ServiceStatus(name, active_state, values.get("SubState", "unknown"),
                                       _parse_timestamp(values.get("ActiveEnterTimestamp", "")),
                                       _category_for(active_state))
    return statuses


_VALID_UNIT_NAME = re.compile(r"^[A-Za-z0-9@_.\-]+\.service$")


@dataclasses.dataclass
class RestartResult:
    success: bool
    message: str


def restart_service(service: str, timeout: float = 120.0, askpass_env: Optional[dict] = None) -> RestartResult:
    """Restart a systemd service. Blocks until the password is given or
    cancelled and the command finishes -- run it off the GUI thread.

    askpass_env given (the GTK app): `sudo -k -A systemctl restart`, so the
    password is asked in the app's own dialog -- the same one the radio
    switch uses. -k means it's asked every time, no 15-minute free pass.
    Without it (the old Tk window): pkexec and GNOME's dialog, as before.

    Only ever call this with a name from the configured service list -- the
    regex is defense-in-depth, not a substitute for that.
    """
    if not _VALID_UNIT_NAME.match(service):
        return RestartResult(False, f"refusing to restart -- not a plausible unit name: {service!r}")

    if askpass_env is not None:
        cmd = ["sudo", "-k", "-A", "systemctl", "restart", service]
        env = dict(os.environ, **askpass_env)
    else:
        cmd = ["pkexec", "systemctl", "restart", service]
        env = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return RestartResult(False, "timed out waiting for the restart to complete")
    except FileNotFoundError as e:
        return RestartResult(False, f"{e.filename} isn't installed")
    except Exception as e:
        return RestartResult(False, str(e))

    if result.returncode == 0:
        return RestartResult(True, "restarted successfully")
    detail = (result.stderr or result.stdout or "").strip()
    if askpass_env is None and result.returncode in (126, 127):
        return RestartResult(False, "authentication cancelled or not authorized")
    if re.search(r"no password was provided|incorrect password|a password is required|askpass", detail, re.I):
        return RestartResult(False, "authentication cancelled or wrong password -- nothing was restarted")
    return RestartResult(False, detail or f"systemctl restart failed (exit code {result.returncode})")


def recent_log(service: str, lines: int = 30, timeout: float = 5.0) -> str:
    """Tail of this unit's journal. Needs journal read access (the `adm` or
    `systemd-journal` group on Debian/Ubuntu) -- no sudo."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "").strip()
        if not output and result.stderr:
            return f"(no log output)\n{result.stderr.strip()}"
        return output or "(no log entries)"
    except Exception as e:
        return f"(couldn't read logs: {e})"


def follow_log_cmd(service: str, lines: int = 200) -> List[str]:
    """journalctl command that prints the last `lines` and then keeps
    following -- for the live log window (the caller owns the process)."""
    return ["journalctl", "-u", service, "-n", str(lines), "-f", "--no-pager", "-o", "short-iso"]
