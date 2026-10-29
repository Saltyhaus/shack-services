#!/usr/bin/env python3
"""
Shack Services (GTK 4) -- red/amber/green/gray glance at the hamshack's key
systemd services, and which program owns the FT-991A.

Same backend as the Tk window (service_query.py, shack_mode.py); new:
  * native Wayland window (GTK 4 / libadwaita)
  * one password dialog for everything: RESTART and the radio switch both
    ask inside this window (sudo -A through shack_askpass_client.py), and
    ask every time -- no standing "always allow"
  * desktop notifications when a service fails or stops (and when it's back),
    so a buried window still tells you; services shack-mode paused on
    purpose don't alert
  * live log: the log window follows the journal as lines arrive
  * the service list lives in ~/.config/shack-services/services.txt
    (one unit per line, # comments) -- menu > Edit service list
  * closing the window keeps it watching in the background; open Shack
    Services again to bring the window back, menu > Quit to stop it

Rollback: SSM_UI=tk ./service_monitor_launcher.sh starts the old Tk window.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import shack_mode  # noqa: E402
import service_query as sq  # noqa: E402

APP_ID = "net.ag7jc.ShackServices"
HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shack-services" / "services.txt"
REFRESH_S = 5

# DN-AUTO Monitor writes its state here every couple of seconds (AMP row)
AMP_STATUS = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "dnauto-monitor" / "status.json"
AMP_DESKTOP_ID = "net.ag7jc.DNAutoMonitor.desktop"


def amp_summary(path=AMP_STATUS, now=None):
    """(light colour, one-line text) for the AMP row, from DN-AUTO Monitor's
    status file. Pure apart from reading the file, so it's testable."""
    import time as _t
    now = now or _t.time()
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "dim", "DN-AUTO Monitor isn't running -- tap to start it"
    if now - float(d.get("time", 0)) > 10:
        return "dim", "DN-AUTO Monitor isn't running -- tap to start it"
    demo = "  (demo data)" if d.get("demo") else ""
    if not d.get("linked"):
        return "amber", "no data from the amp -- switched off, or the serial cable?" + demo
    parts = []
    if d.get("amp_band"):
        parts.append(f"{d['amp_band']} m")
    if d.get("temp_f") is not None:
        parts.append(f"{d['temp_f']:.0f} °F")
    if d.get("for_w"):
        parts.append(f"{d['for_w']} W")
    if d.get("swr") is not None:
        parts.append(f"SWR {d['swr']:.1f}")
    alerts = d.get("alerts") or []
    if alerts:
        extra = ""
        if "MISMATCH" in alerts and d.get("rig_band"):
            extra = f" (radio on {d['rig_band']} m)"
        return "red", "⚠ " + ", ".join(alerts) + extra + "  ·  " + "  ·  ".join(parts) + demo
    return "green", "OK  ·  " + "  ·  ".join(parts or ["idle"]) + demo



# HF Contact guests, from mercury-connector's -e events file
HF_EVENTS = Path(os.environ.get("HF_EVENTS_FILE", Path.home() / "hf_spool" / "events.jsonl"))
# what tapping the HF CONTACT row opens: a URL (your HF dashboard) in this
# file, or else the connector's incoming folder
HF_INBOX_URL_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shack-services" / "hf-inbox.url"
HF_INBOX_DIR = Path.home() / "hf_spool" / "incoming"


def _ago(seconds):
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def hf_contact_summary(path=HF_EVENTS, now=None, window=86400, busy_max=1200):
    """(light colour, one-line text) for the HF CONTACT row: who's called in
    the last 24 h. Green = a guest is linked right now, amber = messages
    waiting to be read (any in the window), dim = quiet."""
    import time as _t
    now = int(now or _t.time())
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return "dim", "no events file -- is the connector running with -e?"
    events = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("t"), int):
            events.append(e)
    # linked right now?
    for e in reversed(events):
        if e.get("event") == "link_down":
            break
        if e.get("event") == "link_up":
            if now - e["t"] < busy_max and e.get("who") == "guest":
                return "green", f"on the air with {e.get('call', 'a guest')} now"
            break
    recent = [e for e in events if e.get("event") in ("echo", "message") and now - e["t"] < window
              and isinstance(e.get("call"), str)]
    if not recent:
        return "dim", "no callers in the last 24 h"
    msgs = [e for e in recent if e["event"] == "message"]
    echoes = [e for e in recent if e["event"] == "echo"]
    parts = []
    if msgs:
        parts.append(f"{len(msgs)} message{'s' if len(msgs) > 1 else ''}")
    if echoes:
        parts.append(f"{len(echoes)} echo test{'s' if len(echoes) > 1 else ''}")
    last = recent[-1]
    what = "message" if last["event"] == "message" else "echo"
    text = f"{' · '.join(parts)} in 24 h  ·  last {last['call']} ({what}, {_ago(now - last['t'])})"
    return ("amber" if msgs else "green"), text

# FT-991A data/voice setups, switched by the radio-profile tool
RADIO_PROFILE = "/usr/local/bin/radio-profile"
PROFILE_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "radio-profile"
SETUPS = ("data", "voice")
SETUP_RADIOS = {"ft991a": "FT-991A", "kx3": "KX3"}


def profile_state(radio):
    """(saved setups, current setup name or None, since-time or None)."""
    d = PROFILE_HOME / radio
    saved = [n for n in SETUPS if (d / f"{n}.json").exists()]
    try:
        cur = json.loads((d / "current.json").read_text())
        return saved, cur.get("name"), cur.get("time")
    except (OSError, ValueError):
        return saved, None, None


def parse_plan(text):
    """The change lines from `radio-profile ... --dry-run` output."""
    if "nothing to change" in text:
        return []
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if "change(s) to" in l) + 1
    except StopIteration:
        return None
    out = []
    for l in lines[start:]:
        if l.startswith("(dry run"):
            break
        if l.strip():
            out.append(" ".join(l.split()))
    return out


DEFAULT_SERVICES = [
    "hf-sync.service",
    "kx3-band-bridge.service",
    "kx3-rigctld.service",
    "kx3-sdrconnect-sync.service",
    "mercury-connector.service",
    "mercury.service",
    "pat@ag7jc.service",
    "radio-control-api.service",
    "rigctld.service",
    "sdrconnect-server.service",
]

# same palette as the Tk window / DN-AUTO Monitor
BG = "#05080d"
PANEL_BG = "#0b1220"
TEAL = "#28e0e0"
AMBER = "#ffb734"
GREEN = "#39ff6a"
RED = "#ff4d4d"
DIM = "#4a5568"
WHITE = "#e8edf4"
BORDER = "#1c2740"

COLORS = ("dim", "green", "red", "amber", "teal")
CATEGORY_COLOR = {sq.GOOD: "green", sq.BAD: "red", sq.TRANSITIONING: "amber", sq.STOPPED: "dim"}
CATEGORY_LABEL = {sq.GOOD: "RUNNING", sq.BAD: "FAILED", sq.TRANSITIONING: "CHANGING", sq.STOPPED: "STOPPED"}
OWNER_COLOR = {"mercury": "green", "modem73": "teal", "apps": "amber", "none": "dim", "conflict": "red"}

CSS = f"""
@define-color accent_color {TEAL};
@define-color accent_bg_color #129a9a;
@define-color accent_fg_color #ffffff;
@define-color window_bg_color {BG};
@define-color window_fg_color {WHITE};
@define-color view_bg_color {PANEL_BG};
@define-color view_fg_color {WHITE};
@define-color headerbar_bg_color {BG};
@define-color headerbar_fg_color {WHITE};
@define-color card_bg_color {PANEL_BG};
@define-color card_fg_color {WHITE};
@define-color popover_bg_color {PANEL_BG};
@define-color dialog_bg_color {PANEL_BG};
@define-color destructive_bg_color #c62828;

.ssm-title {{ color: {TEAL}; font-family: monospace; font-weight: bold; letter-spacing: 1px; }}
.mono {{ font-family: monospace; }}
.dim {{ opacity: 0.7; }}
.c-dim   {{ color: {DIM}; }}
.c-green {{ color: {GREEN}; }}
.c-red   {{ color: {RED}; }}
.c-amber {{ color: {AMBER}; }}
.c-teal  {{ color: {TEAL}; }}
.light {{ min-width: 13px; min-height: 13px; border-radius: 999px; background: {DIM}; }}
.light.green {{ background: {GREEN}; box-shadow: 0 0 7px {GREEN}; }}
.light.red   {{ background: {RED};   box-shadow: 0 0 7px {RED}; }}
.light.amber {{ background: {AMBER}; box-shadow: 0 0 7px {AMBER}; }}
.light.teal  {{ background: {TEAL};  box-shadow: 0 0 7px {TEAL}; }}
.radio-card {{ background: {PANEL_BG}; border: 1px solid {BORDER}; border-radius: 12px; padding: 12px 14px; }}
.owner {{ font-family: monospace; font-weight: bold; font-size: 1.15em; }}
.banner {{ font-family: monospace; font-weight: bold; font-size: 1.35em; }}
.mode-btn {{ font-weight: bold; font-family: monospace; }}
.mode-btn.current {{ background: {TEAL}; color: {BG}; }}
.svc-name {{ font-family: monospace; font-weight: bold; }}
textview.log {{ font-family: monospace; font-size: 0.9em; }}
"""


def load_services(path: Path = CONFIG):
    """Unit names from services.txt (one per line, # comments). Writes the
    default list the first time."""
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Services Shack Services watches -- one unit per line.\n"
                            "# Saved changes are picked up within a few seconds.\n"
                            + "\n".join(DEFAULT_SERVICES) + "\n")
        except OSError:
            return list(DEFAULT_SERVICES)
    out = []
    for line in path.read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if "." not in name:
            name += ".service"
        if name not in out:
            out.append(name)
    return out


class Light(Gtk.Box):
    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_css_class("light")
        self._c = "dim"

    def set_color(self, c):
        if c == self._c:
            return
        for x in COLORS:
            self.remove_css_class(x)
        if c != "dim":
            self.add_css_class(c)
        self._c = c


def tint(widget, color):
    for x in COLORS:
        widget.remove_css_class("c-" + x)
    if color:
        widget.add_css_class("c-" + color)


# --------------------------------------------------------------- notifications

class Notifier:
    """Decides what deserves a desktop notification. Pure logic, so it can be
    tested without GNOME: feed it each poll's states, it returns
    [("send"|"withdraw", id, title, body, urgent)]."""

    def __init__(self):
        self.alerted = {}       # service -> "failed"/"stopped" while an alert is up
        self.prev = {}
        self.first = True
        self.radio_alerted = False

    def update(self, statuses, paused, radio_owner=None):
        out = []
        for name, st in statuses.items():
            if st.error:
                continue
            cat = st.category
            was = self.prev.get(name)
            self.prev[name] = cat
            if name in paused and st.active_state == "inactive":
                if name in self.alerted:          # it was paused on purpose after all
                    out.append(("withdraw", name, "", "", False))
                    del self.alerted[name]
                continue
            if cat == sq.BAD and self.alerted.get(name) != "failed":
                out.append(("send", name, f"{name} FAILED",
                            f"{st.sub_state} -- open Shack Services for its log", True))
                self.alerted[name] = "failed"
            elif (cat == sq.STOPPED and was in (sq.GOOD, sq.TRANSITIONING)
                  and not self.first and name not in self.alerted):
                out.append(("send", name, f"{name} stopped",
                            "It isn't running any more and shack-mode didn't pause it.", False))
                self.alerted[name] = "stopped"
            elif cat == sq.GOOD and name in self.alerted:
                out.append(("withdraw", name, "", "", False))
                out.append(("send", name + ":ok", f"{name} is running again", "", False))
                del self.alerted[name]
        if radio_owner == "conflict" and not self.radio_alerted:
            out.append(("send", "radio", "FT-991A: more than one program can key it",
                        "Mercury, modem73 and WSJT-X/JS8Call -- only one should run.", True))
            self.radio_alerted = True
        elif radio_owner and radio_owner != "conflict" and self.radio_alerted:
            out.append(("withdraw", "radio", "", "", False))
            self.radio_alerted = False
        self.first = False
        return out


# --------------------------------------------------------- in-app password

class AskpassServer:
    """A private Unix socket that shack_askpass_client.py (run by sudo -A)
    connects to; each request shows the app's password dialog."""

    def __init__(self, on_request):
        self.on_request = on_request          # (prompt, reply_fn) on the GTK thread
        d = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self.dir = Path(tempfile.mkdtemp(prefix="shack-services-", dir=d))
        os.chmod(self.dir, 0o700)
        self.path = str(self.dir / f"askpass-{secrets.token_hex(6)}.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(4)
        threading.Thread(target=self._loop, daemon=True).start()

    def env(self):
        return {"SHACK_ASKPASS_SOCK": self.path,
                "SUDO_ASKPASS": str(HERE / "shack_askpass_client.py")}

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            prompt = json.loads(buf.decode()).get("prompt", "Password:")
        except (OSError, ValueError):
            conn.close()
            return
        done = threading.Event()
        answer = {"ok": False}

        def reply(ok, password=""):
            answer.update(ok=ok, password=password)
            done.set()
        GLib.idle_add(lambda: (self.on_request(prompt, reply), False)[1])
        done.wait()
        try:
            conn.sendall((json.dumps(answer) + "\n").encode())
        except OSError:
            pass
        finally:
            answer.clear()
            conn.close()

    def close(self):
        try:
            self.sock.close()
            os.unlink(self.path)
            self.dir.rmdir()
        except OSError:
            pass


# ------------------------------------------------------------------- window

class ShackWindow:
    def __init__(self, app):
        self.app = app
        self.services = load_services()
        self._config_mtime = self._mtime()
        self.rows = {}
        self.radio = None
        self.switching = False
        self.busy = set()               # services being restarted
        self.notifier = Notifier()
        self._polling = False
        self._auth_label = ""           # what the next password is for
        self._auth_tries = 0
        self.askpass = AskpassServer(self._password_dialog)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)
        except AttributeError:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self._build()
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_S, self._tick)

    def _mtime(self):
        try:
            return CONFIG.stat().st_mtime
        except OSError:
            return None

    # ---------------------------------------------------------------- layout

    def _build(self):
        win = Adw.ApplicationWindow(application=self.app, title="Shack Services")
        win.set_default_size(600, 760)
        win.set_size_request(380, 420)
        win.connect("close-request", self._on_close)
        self.win = win

        header = Adw.HeaderBar()
        title = Gtk.Label(label="SHACK SERVICES")
        title.add_css_class("ssm-title")
        header.set_title_widget(title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("Edit service list…", "app.editlist")
        for radio, rname in SETUP_RADIOS.items():
            setup_menu = Gio.Menu()
            for name in SETUPS:
                setup_menu.append(f"Save {rname} setup as {name.upper()}…", f"app.save-{radio}-{name}")
            menu.append_section(None, setup_menu)
        menu.append("Quit (stop watching)", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_start=14, margin_end=14, margin_top=10, margin_bottom=12)

        # radio owner (shack-mode)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("radio-card")
        l1 = Gtk.Box(spacing=8)
        lab = Gtk.Label(label="FT-991A")
        lab.add_css_class("dim")
        lab.add_css_class("heading")
        l1.append(lab)
        self.radio_light = Light()
        l1.append(self.radio_light)
        self.radio_lbl = Gtk.Label(label="checking…", xalign=0, hexpand=True, wrap=True)
        self.radio_lbl.add_css_class("owner")
        l1.append(self.radio_lbl)
        card.append(l1)
        self.radio_info = Gtk.Label(label="", xalign=0, wrap=True)
        self.radio_info.add_css_class("dim")
        card.append(self.radio_info)
        btns = Gtk.Box(homogeneous=True)
        btns.add_css_class("linked")
        self.mode_btns = {}
        for mode in shack_mode.MODES:
            b = Gtk.Button(label=shack_mode.MODE_LABEL[mode])
            b.add_css_class("mode-btn")
            b.connect("clicked", lambda _b, m=mode: self._switch_mode(m))
            btns.append(b)
            self.mode_btns[mode] = b
        card.append(btns)

        self.setup_btns, self.setup_info, self.setup_busy = {}, {}, set()
        self._setup_row(card, "ft991a")
        page.append(card)

        # KX3: just its data/voice setup
        kcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        kcard.add_css_class("radio-card")
        klab = Gtk.Label(label="KX3", xalign=0)
        klab.add_css_class("dim")
        klab.add_css_class("heading")
        kcard.append(klab)
        self._setup_row(kcard, "kx3")
        page.append(kcard)

        # amplifier, from DN-AUTO Monitor's status file
        amp_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        amp_list.add_css_class("boxed-list")
        self.amp_row = Adw.ActionRow(title="AMP  (DN-AUTO)", subtitle="checking…", activatable=True)
        self.amp_row.add_css_class("svc")
        self.amp_light = Light()
        self.amp_row.add_prefix(self.amp_light)
        self.amp_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        amp_list.append(self.amp_row)
        # HF Contact guests (echo tests and messages)
        self.hf_row = Adw.ActionRow(title="HF CONTACT", subtitle="checking…", activatable=True)
        self.hf_row.add_css_class("svc")
        self.hf_light = Light()
        self.hf_row.add_prefix(self.hf_light)
        self.hf_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        amp_list.append(self.hf_row)
        amp_list.connect("row-activated", lambda _l, row: self._open_hf_inbox() if row is self.hf_row
                         else self._open_amp())
        page.append(amp_list)

        # banner
        self.banner = Gtk.Label(label="checking…", xalign=0.5)
        self.banner.add_css_class("banner")
        page.append(self.banner)

        # services
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda _l, row: self._open_log(row.service))
        page.append(self.list)
        self._build_rows()

        hint = Gtk.Label(wrap=True, xalign=0.5,
                         label="Tap a service for its live log, ⟳ to restart it. MERCURY / MODEM73 / APPS "
                               "hand the FT-991A to one program. Both ask for your password, every time. "
                               "Closing the window keeps watching in the background.")
        hint.add_css_class("dim")
        hint.add_css_class("caption")
        page.append(hint)

        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(Adw.Clamp(maximum_size=720, child=page))
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=scroll)
        tv.set_content(self.toasts)
        win.set_content(tv)
        win.present()

    def _build_rows(self):
        child = self.list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt
        self.rows = {}
        for name in self.services:
            row = Adw.ActionRow(title=name, subtitle="checking…", activatable=True)
            row.service = name
            row.add_css_class("svc")
            light = Light()
            row.add_prefix(light)
            btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=f"Restart {name}")
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, n=name: self._confirm_restart(n))
            row.add_suffix(btn)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            self.list.append(row)
            self.rows[name] = {"row": row, "light": light, "btn": btn}

    # ----------------------------------------------------------------- polling

    def _tick(self):
        mt = self._mtime()
        if mt != self._config_mtime:
            self._config_mtime = mt
            new = load_services()
            if new != self.services:
                self.services = new
                self._build_rows()
                self.toast("Service list reloaded")
        self.refresh()
        return True

    def refresh(self):
        if self._polling:
            return
        self._polling = True
        services = list(self.services)

        def work():
            statuses = sq.query_states(services)
            merc = statuses.get("mercury.service")
            try:
                rs = shack_mode.radio_state(mercury_active=bool(merc and merc.category == sq.GOOD))
            except Exception as e:      # never let a /proc hiccup kill the poll
                print(f"[radio] {e}", flush=True)
                rs = None
            GLib.idle_add(self._apply, statuses, rs)
        threading.Thread(target=work, daemon=True).start()

    def _open_amp(self):
        info = Gio.DesktopAppInfo.new(AMP_DESKTOP_ID)
        if info is None:
            self.toast("DN-AUTO Monitor's launcher isn't installed")
            return
        try:
            info.launch([], None)
        except GLib.Error as e:
            self.alert("Couldn't start DN-AUTO Monitor", e.message)

    def _open_hf_inbox(self):
        try:
            url = HF_INBOX_URL_FILE.read_text().strip()
        except OSError:
            url = ""
        if not url:
            if not HF_INBOX_DIR.is_dir():
                self.toast("No inbox to open -- put your HF dashboard's address in " + str(HF_INBOX_URL_FILE))
                return
            url = HF_INBOX_DIR.as_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error as e:
            self.alert("Couldn't open the HF inbox", e.message)

    def _apply_hf(self):
        color, text = hf_contact_summary()
        self.hf_light.set_color(color)
        self.hf_row.set_subtitle(text)

    def _apply_amp(self):
        color, text = amp_summary()
        self.amp_light.set_color(color)
        self.amp_row.set_subtitle(text)
        if color == "red":
            self.amp_row.add_css_class("error")
        else:
            self.amp_row.remove_css_class("error")

    def _setup_row(self, card, radio):
        setup = Gtk.Box(spacing=8)
        sl = Gtk.Label(label="SETUP")
        sl.add_css_class("dim")
        sl.add_css_class("heading")
        setup.append(sl)
        sbtns = Gtk.Box(homogeneous=True, hexpand=True)
        sbtns.add_css_class("linked")
        self.setup_btns[radio] = {}
        for name in SETUPS:
            b = Gtk.Button(label=name.upper())
            b.add_css_class("mode-btn")
            b.connect("clicked", lambda _b, r=radio, n=name: self._setup_clicked(r, n))
            sbtns.append(b)
            self.setup_btns[radio][name] = b
        setup.append(sbtns)
        card.append(setup)
        info = Gtk.Label(label="", xalign=0, wrap=True)
        info.add_css_class("dim")
        info.add_css_class("caption")
        card.append(info)
        self.setup_info[radio] = info

    def _apply_setup(self):
        for radio in SETUP_RADIOS:
            self._apply_setup_one(radio)

    def _apply_setup_one(self, radio):
        if radio in self.setup_busy:
            return
        info = self.setup_info[radio]
        saved, cur, since = profile_state(radio)
        if not os.access(RADIO_PROFILE, os.X_OK):
            info.set_label("radio-profile isn't installed")
        elif not saved:
            info.set_label(f"no setups saved yet -- menu › Save {SETUP_RADIOS[radio]} setup…")
        else:
            when = since[11:16] if since and len(since) >= 16 else ""
            info.set_label(f"radio last set to {cur.upper()}" + (f" at {when}" if when else "")
                           if cur else "radio setup unknown")
        for name, b in self.setup_btns[radio].items():
            b.set_sensitive(name in saved and not (radio == "ft991a" and self.switching))
            if name == cur:
                b.add_css_class("current")
            else:
                b.remove_css_class("current")

    def _apply(self, statuses, rs):
        self._polling = False
        self._apply_amp()
        self._apply_hf()
        self._apply_setup()
        paused = set()
        if rs is not None:
            self._apply_radio(rs)
            paused = set(shack_mode.paused_units(rs))

        good = bad = other = 0
        for name, w in self.rows.items():
            st = statuses.get(name)
            if st is None:
                continue
            row, light = w["row"], w["light"]
            if name in self.busy:
                continue
            if name in paused and st.active_state == "inactive":
                light.set_color("dim")
                row.set_subtitle(f"PAUSED ({rs.mode} mode) · {sq.format_since(rs.since)}")
                row.remove_css_class("error")
                continue
            if st.error:
                light.set_color("dim")
                row.set_subtitle(f"UNKNOWN · {st.error}")
                continue
            light.set_color(CATEGORY_COLOR[st.category])
            row.set_subtitle(f"{CATEGORY_LABEL[st.category]} ({st.sub_state}) · {sq.format_since(st.since)}")
            if st.category == sq.BAD:
                row.add_css_class("error")
            else:
                row.remove_css_class("error")
            if st.category == sq.GOOD:
                good += 1
            elif st.category == sq.BAD:
                bad += 1
            else:
                other += 1

        total = len(self.rows) - len(paused & set(self.rows))
        if bad:
            self.banner.set_label(f"⚠ {bad} SERVICE{'S' if bad != 1 else ''} DOWN")
            tint(self.banner, "red")
        elif other:
            self.banner.set_label(f"◑ {other} NOT RUNNING")
            tint(self.banner, "amber")
        elif good == total and total:
            self.banner.set_label("✓ ALL SYSTEMS GO")
            tint(self.banner, "green")
        else:
            self.banner.set_label("checking…")
            tint(self.banner, "dim")

        for action, nid, title, body, urgent in self.notifier.update(
                {n: s for n, s in statuses.items() if n not in self.busy}, paused,
                rs.owner if rs else None):
            if action == "send":
                n = Gio.Notification.new(title)
                if body:
                    n.set_body(body)
                n.set_priority(Gio.NotificationPriority.URGENT if urgent else Gio.NotificationPriority.NORMAL)
                n.set_default_action("app.show")
                self.app.send_notification(nid, n)
            else:
                self.app.withdraw_notification(nid)
        return False

    def _apply_radio(self, rs):
        self.radio = rs
        if self.switching:
            return
        color = OWNER_COLOR.get(rs.owner, "dim")
        self.radio_light.set_color(color)
        text = shack_mode.MODE_TEXT.get(rs.owner, rs.owner)
        if rs.owner == "conflict":
            text = f"{text}: {', '.join(rs.owners)}"
        self.radio_lbl.set_label(text)
        tint(self.radio_lbl, color)
        info = []
        if rs.mode and rs.since:
            info.append(f"since {rs.since:%a %H:%M}")
        if rs.apps:
            info.append("open: " + ", ".join(rs.apps))
        if rs.outbox:
            info.append(f"outbox: {len(rs.outbox)} waiting")
        self.radio_info.set_label("  ·  ".join(info) or " ")
        tint(self.radio_info, "amber" if rs.outbox else None)
        for mode, b in self.mode_btns.items():
            b.set_sensitive(True)
            if mode == rs.owner:
                b.add_css_class("current")
            else:
                b.remove_css_class("current")

    # ------------------------------------------------------------- dialogs

    def ask(self, heading, body, responses, on_response, default=None, extra=None):
        """responses: [(id, label, style)]; the first is the cancel/close one."""
        styles = {"suggested": Adw.ResponseAppearance.SUGGESTED,
                  "destructive": Adw.ResponseAppearance.DESTRUCTIVE}
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog(heading=heading, body=body)
        else:
            d = Adw.MessageDialog(transient_for=self.win, heading=heading, body=body)
        for rid, label, style in responses:
            d.add_response(rid, label)
            if style:
                d.set_response_appearance(rid, styles[style])
        d.set_close_response(responses[0][0])
        if default:
            d.set_default_response(default)
        if extra is not None:
            d.set_extra_child(extra)
        d.connect("response", lambda _d, rid: on_response(rid))
        self.last_dialog = d                 # for the test harness
        if hasattr(Adw, "AlertDialog"):
            d.present(self.win)
        else:
            d.present()
        return d

    def alert(self, heading, body):
        self.ask(heading, body, [("ok", "OK", None)], lambda _r: None, default="ok")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _password_dialog(self, prompt, reply):
        """Shown for every sudo password request (restart and radio switch)."""
        self.show()
        self._auth_tries += 1
        pw = Adw.PasswordEntryRow(title="Password")
        pw.set_activates_default(True)          # Enter = Authenticate
        self.last_password_row = pw          # for the test harness
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(pw)
        what = self._auth_label or "This needs administrator rights."
        retry = "\n\nThat password didn't work -- try again." if self._auth_tries > 1 else ""
        replied = {"done": False}

        def respond(rid):
            if replied["done"]:
                return
            replied["done"] = True
            text = pw.get_text() if rid == "ok" else ""
            pw.set_text("")
            reply(rid == "ok", text)

        self.ask("Authentication required", f"{what}{retry}",
                 [("cancel", "Cancel", None), ("ok", "Authenticate", "suggested")],
                 respond, default="ok", extra=box)
        GLib.idle_add(lambda: (pw.grab_focus(), False)[1])

    # --------------------------------------------------------------- restart

    def _confirm_restart(self, service, on_done=None):
        if service in self.busy:
            return
        self.ask(f"Restart {service}?",
                 "This briefly interrupts it. You'll be asked for your password.",
                 [("no", "Cancel", None), ("yes", "Restart", "destructive")],
                 lambda rid: rid == "yes" and self._restart(service, on_done), default="yes")

    def _restart(self, service, on_done=None):
        self.busy.add(service)
        w = self.rows.get(service)
        if w:
            w["btn"].set_sensitive(False)
            w["row"].set_subtitle("restarting…")
            w["light"].set_color("amber")
        self._auth_label = f"Restart {service}"
        self._auth_tries = 0

        def work():
            r = sq.restart_service(service, askpass_env=self.askpass.env())
            GLib.idle_add(self._restart_done, service, r, on_done)
        threading.Thread(target=work, daemon=True).start()

    def _restart_done(self, service, r, on_done):
        self.busy.discard(service)
        self._auth_label = ""
        w = self.rows.get(service)
        if w:
            w["btn"].set_sensitive(True)
        if r.success:
            self.toast(f"{service} restarted")
        else:
            self.alert("Restart failed", f"{service}:\n\n{r.message}")
        self.refresh()
        if on_done:
            on_done()
        return False

    # ------------------------------------------------------------ radio switch

    def _switch_mode(self, mode):
        if self.switching:
            return
        rs = self.radio
        label = shack_mode.MODE_LABEL[mode]
        if rs and rs.owner == mode:
            return
        if rs and mode != "apps" and rs.apps:
            self.alert("Close the apps first",
                       f"{', '.join(rs.apps)} is still open and using the radio.\n\n"
                       f"Close it, then press {label} again.")
            return
        if mode == "mercury" and rs and rs.outbox:
            shown = "\n".join("   " + f for f in rs.outbox[:10])
            more = f"\n   …and {len(rs.outbox) - 10} more" if len(rs.outbox) > 10 else ""
            body = (f"{len(rs.outbox)} message(s) will TRANSMIT as soon as Mercury starts:\n\n"
                    f"{shown}{more}")
            yes = "Start Mercury and send"
        else:
            body = f"Hand the FT-991A to {shack_mode.MODE_TEXT[mode]}?\n\nYou'll be asked for your password."
            yes = f"Switch to {label}"
        self.ask(f"Switch radio to {label}?", body,
                 [("no", "Cancel", None), ("yes", yes, "suggested")],
                 lambda rid: rid == "yes" and self._do_switch(mode), default="yes")

    # ------------------------------------------------------- data/voice setup

    def _rp_env(self):
        # shack-mode (run by radio-profile for the handover) asks for the
        # password through this window, same as the MERCURY/APPS buttons
        return dict(os.environ, SUDO_ASKPASS=str(HERE / "shack_askpass_client.py"),
                    SHACK_MODE_ASKPASS="1", SHACK_ASKPASS_SOCK=self.askpass.path)

    def _setup_clicked(self, radio, name):
        if radio in self.setup_busy or (radio == "ft991a" and self.switching):
            return
        self._setup_start(radio, f"reading the {SETUP_RADIOS[radio]} to compare with {name.upper()}…")

        def work():
            try:
                r = subprocess.run([RADIO_PROFILE, radio, name, "--dry-run"], capture_output=True,
                                   text=True, timeout=90, stdin=subprocess.DEVNULL)
                out = (r.stdout or "") + (r.stderr or "")
                plan = parse_plan(out) if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired) as e:
                out, plan = str(e), None
            GLib.idle_add(self._setup_confirm, radio, name, plan, out)
        threading.Thread(target=work, daemon=True).start()

    def _setup_start(self, radio, text):
        self.setup_busy.add(radio)
        for b in self.setup_btns[radio].values():
            b.set_sensitive(False)
        self.setup_info[radio].set_label(text)

    def _setup_done(self, radio):
        self.setup_busy.discard(radio)
        self._apply_setup_one(radio)

    def _setup_confirm(self, radio, name, plan, out):
        label, rname = name.upper(), SETUP_RADIOS[radio]
        if plan is None:
            self._setup_done(radio)
            self.alert(f"Couldn't read the {rname}", out.strip()[-1200:] or "radio-profile failed")
            return False
        hand = ""
        if radio == "ft991a":
            hand = {"voice": "\n\nMercury is taken off the radio first, so queued mail can't go out with voice settings.",
                    "data": "\n\nThen the radio goes back to Mercury."}.get(name, "")
        notes = [l for l in out.splitlines() if l.startswith("⚠")]
        if not plan:
            if radio == "ft991a" and name == "data":
                body = f"The radio already matches {label}.\n\nHand it back to Mercury?"
            else:
                self._setup_done(radio)
                if notes:
                    self.alert(f"{rname} already set for {label}", "\n".join(n.lstrip("⚠ ") for n in notes))
                else:
                    self.toast(f"{rname} already set for {label}")
                return False
        else:
            shown = "\n".join(plan[:14]) + (f"\n…and {len(plan) - 14} more" if len(plan) > 14 else "")
            body = f"{len(plan)} change(s):\n\n{shown}{hand}"
        self.ask(f"Set the {rname} for {label}?", body,
                 [("no", "Cancel", None), ("yes", f"Set {label}", "suggested")],
                 lambda rid: self._setup_run(radio, name) if rid == "yes" else self._setup_done(radio),
                 default="yes")
        return False

    def _setup_run(self, radio, name):
        rname = SETUP_RADIOS[radio]
        self.setup_info[radio].set_label(f"setting the {rname} for {name.upper()}…")
        self._auth_label = f"Set the {rname} for {name.upper()} (radio handover)"
        self._auth_tries = 0

        def work():
            try:
                r = subprocess.run(["script", "-qec", f"{RADIO_PROFILE} {radio} {name} -y", "/dev/null"],
                                   capture_output=True, text=True, timeout=180, env=self._rp_env(),
                                   stdin=subprocess.DEVNULL)
                ok, out = r.returncode == 0, (r.stdout or "") + (r.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, out = False, str(e)
            GLib.idle_add(self._setup_finished, radio, name, ok, out)
        threading.Thread(target=work, daemon=True).start()

    def _setup_finished(self, radio, name, ok, out):
        self._auth_label = ""
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out).replace("\r", "").strip()
        rname = SETUP_RADIOS[radio]
        self._setup_done(radio)
        notes = [l.lstrip("⚠ ") for l in out.splitlines() if l.startswith("⚠")]
        if ok and notes:
            self.alert(f"{rname} set for {name.upper()} -- one thing to do by hand", "\n".join(notes))
        elif ok:
            self.toast(f"{rname} set for {name.upper()}")
        else:
            self.alert(f"{rname} not fully set for {name.upper()}", out[-1500:])
        self.refresh()
        return False

    def save_setup(self, radio, name):
        rname = SETUP_RADIOS[radio]
        what = ("every menu setting, power, mic gain, processor, VOX, monitor and mode" if radio == "ft991a" else
                "power, mic gain, compression, monitor, ESSB, mode and the KX3 menu settings CAT can reach "
                "(VOX is noted so you're reminded to set it by hand)")
        self.ask(f"Save the {rname}'s current setup as {name.upper()}?",
                 f"Reads {what} from the {rname} as it is right now and stores them as {name.upper()}, "
                 f"replacing the saved one.",
                 [("no", "Cancel", None), ("yes", "Save", "suggested")],
                 lambda rid: rid == "yes" and self._save_setup_run(radio, name), default="yes")

    def _save_setup_run(self, radio, name):
        self._setup_start(radio, f"reading the {SETUP_RADIOS[radio]} to save as {name.upper()}…")

        def work():
            try:
                r = subprocess.run([RADIO_PROFILE, radio, "save", name], capture_output=True, text=True,
                                   timeout=90, stdin=subprocess.DEVNULL)
                ok, out = r.returncode == 0, (r.stdout or "") + (r.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, out = False, str(e)
            GLib.idle_add(lambda: (self._setup_done(radio),
                                   self.toast(f"{SETUP_RADIOS[radio]} saved as {name.upper()}") if ok
                                   else self.alert("Not saved", out.strip()[-800:]), False)[-1])
        threading.Thread(target=work, daemon=True).start()

    def _do_switch(self, mode):
        label = shack_mode.MODE_LABEL[mode]
        self.switching = True
        self.radio_lbl.set_label(f"switching to {label}…")
        tint(self.radio_lbl, "amber")
        self.radio_light.set_color("amber")
        for b in self.mode_btns.values():
            b.set_sensitive(False)
        self._auth_label = f"Switch the FT-991A to {label} ({shack_mode.MODE_TEXT[mode]})"
        self._auth_tries = 0

        def work():
            r = shack_mode.switch(mode, extra_env={"SHACK_ASKPASS_SOCK": self.askpass.path})
            GLib.idle_add(self._switch_done, mode, r)
        threading.Thread(target=work, daemon=True).start()

    def _switch_done(self, mode, r):
        self.switching = False
        self._auth_label = ""
        label = shack_mode.MODE_LABEL[mode]
        if not r.success:
            self.alert("Radio not switched", r.message[-1500:])
        elif "!!" in r.message:
            self.alert(f"Radio: {label}", r.message[-1500:])   # worked, but shack-mode had something to say
        else:
            self.toast(f"FT-991A → {label}")
        self.refresh()
        return False

    # ------------------------------------------------------------ live log

    def _open_log(self, service):
        LogWindow(self, service)

    # ------------------------------------------------------------ lifecycle

    def show(self):
        self.app.release_bg()
        self.win.set_visible(True)
        self.win.present()

    def _on_close(self, _w):
        if self.app.quitting:
            return False
        self.app.hold_bg()
        self.win.set_visible(False)
        if not getattr(self, "_told_bg", False):
            self._told_bg = True
            n = Gio.Notification.new("Shack Services is still watching")
            n.set_body("You'll get a notification if a service fails. Open Shack Services to see the "
                       "window again, or Quit from its menu.")
            n.set_default_action("app.show")
            self.app.send_notification("bg", n)
        return True


class LogWindow:
    """Live journal for one service: the last 200 lines, then follows."""

    MAX_LINES = 4000

    def __init__(self, main, service):
        self.main = main
        self.service = service
        self.proc = None
        self.q = queue.Queue()
        self.alive = True

        w = Adw.Window(title=f"Log — {service}", default_width=820, default_height=520,
                       transient_for=main.win, application=main.app)
        w.connect("close-request", self._close)
        self.win = w
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title=service, subtitle="live journal"))
        self.follow = Gtk.ToggleButton(icon_name="go-bottom-symbolic", active=True,
                                       tooltip_text="Follow new lines")
        header.pack_start(self.follow)
        rb = Gtk.Button(label="Restart", tooltip_text=f"Restart {service}")
        rb.add_css_class("destructive-action")
        rb.connect("clicked", lambda _b: main._confirm_restart(service))
        header.pack_end(rb)
        cb = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy the log")
        cb.connect("clicked", lambda _b: self._copy())
        header.pack_end(cb)

        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.NONE)
        self.view.add_css_class("log")
        for m in ("left", "right", "top", "bottom"):
            getattr(self.view, f"set_{m}_margin")(8)
        self.buf = self.view.get_buffer()
        self.err_tag = self.buf.create_tag("err", foreground=RED)
        self.end = self.buf.create_mark("end", self.buf.get_end_iter(), False)
        sw = Gtk.ScrolledWindow(vexpand=True, child=self.view)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=sw)
        tv.set_content(self.toasts)
        w.set_content(tv)
        w.present()
        self._start()
        GLib.timeout_add(250, self._drain)

    def _start(self):
        try:
            self.proc = subprocess.Popen(sq.follow_log_cmd(self.service), stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self.q.put(f"(couldn't run journalctl: {e})\n")
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            self.q.put(line)
        self.q.put(None)

    def _drain(self):
        if not self.alive:
            return False
        lines = []
        ended = False
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    ended = True
                    break
                lines.append(item)
        except queue.Empty:
            pass
        for line in lines:
            low = line.lower()
            if any(k in low for k in ("error", "failed", "failure", "fatal")):
                self.buf.insert_with_tags(self.buf.get_end_iter(), line, self.err_tag)
            else:
                self.buf.insert(self.buf.get_end_iter(), line)
        if self.buf.get_line_count() > self.MAX_LINES:
            cut = self.buf.get_iter_at_line(self.buf.get_line_count() - self.MAX_LINES)
            cut = cut[1] if isinstance(cut, tuple) else cut
            self.buf.delete(self.buf.get_start_iter(), cut)
        if lines and self.follow.get_active():
            self.view.scroll_mark_onscreen(self.end)
        if ended:
            self.buf.insert(self.buf.get_end_iter(), "\n(journalctl stopped -- close and reopen to follow again)\n")
            return False
        return True

    def _copy(self):
        text = self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False)
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title="Log copied", timeout=2))

    def _close(self, _w):
        self.alive = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


class ShackApp(Adw.Application):
    def __init__(self):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        self.quitting = False
        self._held = False
        for name, cb in (("show", lambda: self.window and self.window.show()),
                         ("editlist", self._edit_list),
                         ("quit", self._quit)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
        for radio in SETUP_RADIOS:
            for name in SETUPS:
                a = Gio.SimpleAction.new(f"save-{radio}-{name}", None)
                a.connect("activate", lambda _a, _p, r=radio, n=name: self.window and self.window.save_setup(r, n))
                self.add_action(a)

    def do_activate(self):
        if self.window:
            self.window.show()
            return
        self.window = ShackWindow(self)

    def hold_bg(self):
        if not self._held:
            self.hold()
            self._held = True

    def release_bg(self):
        if self._held:
            self._held = False
            self.release()

    def _edit_list(self):
        load_services()      # make sure the file exists
        f = Gio.File.new_for_path(str(CONFIG))
        if hasattr(Gtk, "FileLauncher"):
            Gtk.FileLauncher.new(f).launch(self.window.win if self.window else None, None, None, None)
        else:
            subprocess.Popen(["xdg-open", str(CONFIG)])
        if self.window:
            self.window.toast("Edit and save -- the list reloads by itself")

    def _quit(self):
        self.quitting = True
        if self.window:
            self.window.askpass.close()
            self.window.win.destroy()
        self.release_bg()
        self.quit()


def main():
    return ShackApp().run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
