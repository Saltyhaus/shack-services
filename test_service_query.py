"""
Offline tests for service_query.py's parsing logic -- mocks subprocess.run
so these pass with no live systemd instance (useful for CI/dev boxes, and
for developing this away from the actual shack computer).

Run with: python3 test_service_query.py
"""

import datetime
from unittest.mock import patch, MagicMock

from service_query import query_states, format_since, recent_log, restart_service, GOOD, BAD, TRANSITIONING, STOPPED


def _fake_run(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def test_query_states_active_and_failed():
    stdout = (
        "ActiveState=active\nSubState=running\nActiveEnterTimestamp=Mon 2026-08-23 03:00:00 PDT\n"
        "ActiveState=failed\nSubState=failed\nActiveEnterTimestamp=Mon 2026-08-23 02:00:00 PDT\n"
    )
    with patch("subprocess.run", return_value=_fake_run(stdout, returncode=3)):
        result = query_states(["svc-a.service", "svc-b.service"])
    assert result["svc-a.service"].category == GOOD
    assert result["svc-a.service"].active_state == "active"
    assert result["svc-b.service"].category == BAD
    assert result["svc-b.service"].active_state == "failed"


def test_query_states_inactive_and_activating():
    stdout = (
        "ActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=\n"
        "ActiveState=activating\nSubState=start\nActiveEnterTimestamp=Mon 2026-08-23 03:00:00 PDT\n"
    )
    with patch("subprocess.run", return_value=_fake_run(stdout, returncode=3)):
        result = query_states(["svc-c.service", "svc-d.service"])
    assert result["svc-c.service"].category == STOPPED
    assert result["svc-c.service"].since is None  # empty timestamp -- never active
    assert result["svc-d.service"].category == TRANSITIONING


def test_query_states_systemctl_unreachable():
    with patch("subprocess.run", side_effect=FileNotFoundError("no systemctl")):
        result = query_states(["svc-a.service"])
    assert result["svc-a.service"].active_state == "unknown"
    assert result["svc-a.service"].category == STOPPED
    assert result["svc-a.service"].error is not None


def test_query_states_empty_list():
    assert query_states([]) == {}


def test_query_states_wrong_line_count_falls_back_to_unknown_for_all():
    # Total line count isn't an exact multiple of 3 (one line short of what
    # 2 units x 3 properties should produce) -- real systemctl always emits
    # a full 3-line block per unit (even an unknown unit name still gets
    # ActiveState=inactive etc.), so this shape only happens if something
    # is wrong with the whole call. There's no safe way to guess which
    # unit's line is missing, so every unit should come back unknown
    # rather than risk misattributing lines across units.
    stdout = "ActiveState=active\nSubState=running\n" "ActiveState=failed\nSubState=failed\nActiveEnterTimestamp=\n"
    with patch("subprocess.run", return_value=_fake_run(stdout, returncode=3)):
        result = query_states(["svc-a.service", "svc-b.service"])
    assert result["svc-a.service"].category == STOPPED
    assert result["svc-a.service"].error is not None
    assert result["svc-b.service"].category == STOPPED
    assert result["svc-b.service"].error is not None


def test_format_since():
    now = datetime.datetime(2026, 8, 23, 12, 0, 0)
    assert format_since(datetime.datetime(2026, 8, 23, 11, 59, 30), now) == "30s"
    assert format_since(datetime.datetime(2026, 8, 23, 11, 45, 0), now) == "15m"
    assert format_since(datetime.datetime(2026, 8, 23, 9, 46, 0), now) == "2h 14m"
    assert format_since(datetime.datetime(2026, 8, 20, 12, 0, 0), now) == "3d"
    assert format_since(None, now) == "?"


def test_format_since_clock_skew_or_bad_parse_never_shows_negative():
    now = datetime.datetime(2026, 8, 23, 12, 0, 0)
    future = datetime.datetime(2026, 8, 23, 13, 0, 0)
    assert format_since(future, now) == "?"


def test_recent_log_returns_stdout():
    with patch("subprocess.run", return_value=_fake_run("2026-08-23T03:00:00 log line one\n")):
        out = recent_log("svc-a.service")
    assert "log line one" in out


def test_recent_log_handles_exception():
    with patch("subprocess.run", side_effect=FileNotFoundError("no journalctl")):
        out = recent_log("svc-a.service")
    assert "couldn't read logs" in out


def test_restart_service_success():
    with patch("subprocess.run", return_value=_fake_run("", returncode=0)) as m:
        result = restart_service("mercury.service")
    assert result.success is True
    assert m.call_args[0][0] == ["pkexec", "systemctl", "restart", "mercury.service"]


def test_restart_service_auth_cancelled():
    with patch("subprocess.run", return_value=_fake_run("", returncode=126)):
        result = restart_service("mercury.service")
    assert result.success is False
    assert "cancelled" in result.message


def test_restart_service_polkit_denied():
    with patch("subprocess.run", return_value=_fake_run("", returncode=127)):
        result = restart_service("mercury.service")
    assert result.success is False
    assert "denied" in result.message


def test_restart_service_systemctl_failure_shows_stderr():
    m = _fake_run("", returncode=1)
    m.stderr = "Unit foo.service not found."
    with patch("subprocess.run", return_value=m):
        result = restart_service("mercury.service")
    assert result.success is False
    assert "not found" in result.message


def test_restart_service_rejects_implausible_unit_name():
    # defense-in-depth -- this function should never be called with
    # anything but a name from the fixed SERVICES list, but if it somehow
    # is, it should refuse rather than hand something odd to pkexec
    with patch("subprocess.run") as m:
        result = restart_service("mercury.service; rm -rf /")
    assert result.success is False
    assert not m.called  # never even attempted to run anything


def test_restart_service_pkexec_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError("no pkexec")):
        result = restart_service("mercury.service")
    assert result.success is False
    assert "pkexec" in result.message


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    if failed:
        raise SystemExit(f"{failed}/{len(tests)} tests failed")
    print(f"\nAll {len(tests)} tests passed.")
