#!/usr/bin/env python3
"""
Shack Services -- a quick red/amber/green/gray glance at whether the shack
computer's key systemd services are actually running, styled to match
DN-AUTO Monitor's dark FT-991A-inspired look.

Usage:
    python3 service_monitor_gui.py

No arguments -- the service list is fixed in SERVICES below (edit that list
to add/remove services). Polls systemctl every REFRESH_MS milliseconds;
click any service row to see its last 30 journal lines without needing a
terminal. Each row also has a RESTART button, which shells out to `pkexec
systemctl restart <service>` -- pkexec pops the desktop's own native
PolicyKit authentication dialog, no custom setup needed on a stock
GNOME/Ubuntu install.

The RADIO panel at the top shows which program owns the FT-991A (Mercury,
modem73, or WSJT-X/JS8Call) and switches it with the MERCURY / MODEM73 /
APPS buttons, by running the `shack-mode` command (see shack_mode.py).
Services stopped on purpose by shack-mode show as PAUSED, not as down.

Requires: nothing beyond the Python standard library + Tkinter (same as
dnauto_gui.py in the sibling dnauto-linux-monitor project). No root/sudo
needed to read service status; journal log reading needs the running user
to be in the `adm` or `systemd-journal` group (already the case here).
Restarting a service or switching the radio needs the user to successfully
authenticate each time -- there's no standing "always allow" here by design.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

import shack_mode
from service_query import (
    query_states,
    recent_log,
    restart_service,
    format_since,
    GOOD,
    BAD,
    TRANSITIONING,
    STOPPED,
)

# Services to watch -- edit this list to add/remove services.
SERVICES = [
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

REFRESH_MS = 5000  # 5s -- systemctl show for 9 units is a few ms of work, this is plenty responsive

# Same palette as dnauto_gui.py, for a matching look.
BG = "#05080d"
PANEL_BG = "#0b1220"
FG_TEAL = "#28e0e0"
FG_AMBER = "#ffb734"
FG_GREEN = "#39ff6a"
FG_RED = "#ff4d4d"
FG_DIM = "#4a5568"
FG_WHITE = "#e8edf4"
BORDER = "#1c2740"

CATEGORY_COLOR = {
    GOOD: FG_GREEN,
    BAD: FG_RED,
    TRANSITIONING: FG_AMBER,
    STOPPED: FG_DIM,
}
CATEGORY_LABEL = {
    GOOD: "RUNNING",
    BAD: "FAILED",
    TRANSITIONING: "CHANGING",
    STOPPED: "STOPPED",
}

OWNER_COLOR = {
    "mercury": FG_GREEN,
    "modem73": FG_TEAL,
    "apps": FG_AMBER,
    "none": FG_DIM,
    "conflict": FG_RED,
}


class ToggleSwitch(tk.Canvas):
    """Small sliding on/off switch -- same widget as dnauto_gui.py's, kept
    as a local copy so this app has no dependency on that project's files."""

    WIDTH = 42
    HEIGHT = 22

    def __init__(self, parent, command=None, initial=False, bg=BG, on_color=FG_GREEN, off_color=FG_DIM):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.on_color = on_color
        self.off_color = off_color
        self.state = initial
        self._knob_x = self._on_x() if initial else self._off_x()
        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _off_x(self):
        return self.HEIGHT / 2

    def _on_x(self):
        return self.WIDTH - self.HEIGHT / 2

    def _draw(self):
        self.delete("all")
        track_color = self.on_color if self.state else self.off_color
        h, w, r = self.HEIGHT, self.WIDTH, self.HEIGHT / 2
        self.create_oval(0, 0, h, h, fill=track_color, outline="")
        self.create_oval(w - h, 0, w, h, fill=track_color, outline="")
        self.create_rectangle(r, 0, w - r, h, fill=track_color, outline="")
        knob_r = r - 3
        x = self._knob_x
        self.create_oval(x - knob_r, r - knob_r, x + knob_r, r + knob_r, fill="#f5f7fa", outline="")

    def _on_click(self, _event):
        self.state = not self.state
        target_x = self._on_x() if self.state else self._off_x()
        self._animate(target_x)
        if self.command:
            self.command(self.state)

    def _animate(self, target_x, steps=6):
        start_x = self._knob_x
        delta = (target_x - start_x) / steps

        def step(i):
            self._knob_x = start_x + delta * i
            self._draw()
            if i < steps:
                self.after(12, lambda: step(i + 1))

        step(1)


class ServiceMonitorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.rows = {}  # service name -> dict of widgets for that row
        self.radio = None  # last shack_mode.RadioState
        self.switching = False

        root.title("Shack Services")
        root.configure(bg=BG)
        root.geometry("560x640")
        root.minsize(480, 540)

        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
            self._icon_img = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        self._build_fonts()
        self._build_layout()
        self._refresh()

    def _build_fonts(self):
        self.f_title = tkfont.Font(family="DejaVu Sans Mono", size=16, weight="bold")
        self.f_banner = tkfont.Font(family="DejaVu Sans Mono", size=15, weight="bold")
        self.f_row_name = tkfont.Font(family="DejaVu Sans Mono", size=12, weight="bold")
        self.f_row_state = tkfont.Font(family="DejaVu Sans", size=10)
        self.f_label = tkfont.Font(family="DejaVu Sans", size=9, weight="bold")

    def _build_layout(self):
        outer = tk.Frame(self.root, bg=BORDER, bd=0)
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        screen = tk.Frame(outer, bg=BG, bd=0)
        screen.pack(fill="both", expand=True, padx=2, pady=2)

        top = tk.Frame(screen, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="SHACK SERVICES", font=self.f_title, fg=FG_TEAL, bg=BG).pack(side="left")

        ontop_frame = tk.Frame(top, bg=BG)
        ontop_frame.pack(side="right")
        tk.Label(ontop_frame, text="ON TOP", font=self.f_label, fg=FG_DIM, bg=BG).pack(side="left", padx=(0, 6))
        self.ontop_toggle = ToggleSwitch(ontop_frame, command=self._on_ontop_toggle, initial=False, bg=BG)
        self.ontop_toggle.pack(side="left")

        # ---- radio owner (shack-mode) ----
        radio = tk.Frame(screen, bg=PANEL_BG)
        radio.pack(fill="x", padx=14, pady=(12, 0))
        line1 = tk.Frame(radio, bg=PANEL_BG)
        line1.pack(fill="x")
        tk.Label(line1, text="FT-991A", font=self.f_label, fg=FG_DIM, bg=PANEL_BG).pack(side="left", padx=(12, 8), pady=(10, 0))
        self.radio_dot = tk.Canvas(line1, width=16, height=16, bg=PANEL_BG, highlightthickness=0)
        self.radio_dot.pack(side="left", pady=(10, 0))
        self.radio_dot_id = self.radio_dot.create_oval(2, 2, 14, 14, fill=FG_DIM, outline="")
        self.radio_lbl = tk.Label(line1, text="checking...", font=self.f_row_name, fg=FG_DIM, bg=PANEL_BG, anchor="w")
        self.radio_lbl.pack(side="left", padx=8, pady=(10, 0))

        line2 = tk.Frame(radio, bg=PANEL_BG)
        line2.pack(fill="x")
        self.radio_info = tk.Label(line2, text="", font=self.f_row_state, fg=FG_DIM, bg=PANEL_BG, anchor="w")
        self.radio_info.pack(side="left", padx=12, pady=(2, 10))
        self.mode_btns = {}
        for mode in reversed(shack_mode.MODES):   # packed right-to-left: MERCURY | MODEM73 | APPS
            b = tk.Label(line2, text=shack_mode.MODE_LABEL[mode], font=self.f_label, fg=FG_TEAL,
                         bg=PANEL_BG, cursor="hand2", padx=8, pady=3, highlightthickness=1,
                         highlightbackground=BORDER)
            b.pack(side="right", padx=(0, 8 if mode != "apps" else 12), pady=(2, 10))
            b.bind("<Button-1>", lambda e, m=mode: self._switch_mode(m))
            self.mode_btns[mode] = b

        # ---- aggregate banner ----
        banner_row = tk.Frame(screen, bg=PANEL_BG)
        banner_row.pack(fill="x", padx=14, pady=(8, 4))
        self.banner_lbl = tk.Label(
            banner_row, text="checking...", font=self.f_banner, fg=FG_DIM, bg=PANEL_BG, pady=10
        )
        self.banner_lbl.pack(side="left", padx=12)
        refresh_btn = tk.Label(
            banner_row, text="⟳ REFRESH", font=self.f_label, fg=FG_TEAL, bg=PANEL_BG, cursor="hand2", padx=10
        )
        refresh_btn.pack(side="right", padx=12)
        refresh_btn.bind("<Button-1>", lambda e: self._refresh_one_immediately())

        # ---- service rows ----
        rows_frame = tk.Frame(screen, bg=BG)
        rows_frame.pack(fill="both", expand=True, padx=14, pady=(8, 12))

        for name in SERVICES:
            row = tk.Frame(rows_frame, bg=PANEL_BG, cursor="hand2")
            row.pack(fill="x", pady=3)

            dot = tk.Canvas(row, width=16, height=16, bg=PANEL_BG, highlightthickness=0)
            dot.pack(side="left", padx=(10, 8), pady=8)
            dot_id = dot.create_oval(2, 2, 14, 14, fill=FG_DIM, outline="")

            name_lbl = tk.Label(row, text=name, font=self.f_row_name, fg=FG_WHITE, bg=PANEL_BG, anchor="w")
            name_lbl.pack(side="left", fill="x", expand=True, pady=8)

            restart_btn = tk.Label(
                row, text="⟳ RESTART", font=self.f_label, fg=FG_AMBER, bg=PANEL_BG, cursor="hand2", padx=8
            )
            restart_btn.pack(side="right", padx=(0, 12), pady=8)
            # Separate widget/binding from the row's log-open click below --
            # Tk fires the binding on whichever specific widget was clicked,
            # so this doesn't also open the log window.
            restart_btn.bind("<Button-1>", lambda e, n=name: self._confirm_and_restart(n))

            state_lbl = tk.Label(row, text="checking...", font=self.f_row_state, fg=FG_DIM, bg=PANEL_BG, anchor="e")
            state_lbl.pack(side="right", padx=12, pady=8)

            for w in (row, dot, name_lbl, state_lbl):
                w.bind("<Button-1>", lambda e, n=name: self._open_log_window(n))

            self.rows[name] = {
                "row": row,
                "dot": dot,
                "dot_id": dot_id,
                "name_lbl": name_lbl,
                "state_lbl": state_lbl,
                "restart_btn": restart_btn,
            }

        tk.Label(
            screen,
            text="Click a service for its recent log, or RESTART to bounce it (asks for authentication). "
                 "MERCURY / MODEM73 / APPS hand the FT-991A to one program. Auto-refreshes every 5s.",
            font=self.f_label,
            fg=FG_DIM,
            bg=BG,
            wraplength=520,
            justify="center",
        ).pack(pady=(0, 8))

    def _on_ontop_toggle(self, state: bool):
        self.root.attributes("-topmost", state)

    # ---------------- radio owner (shack-mode) ----------------

    def _apply_radio(self, rs):
        self.radio = rs
        if self.switching:
            return
        color = OWNER_COLOR.get(rs.owner, FG_DIM)
        self.radio_dot.itemconfig(self.radio_dot_id, fill=color)
        text = shack_mode.MODE_TEXT.get(rs.owner, rs.owner)
        if rs.owner == "conflict":
            text = f"{text}: {', '.join(rs.owners)}"
        self.radio_lbl.configure(text=text, fg=color)

        info = []
        if rs.mode and rs.since:
            info.append(f"since {rs.since:%a %H:%M}")
        if rs.apps:
            info.append("open: " + ", ".join(rs.apps))
        if rs.outbox:
            info.append(f"outbox: {len(rs.outbox)} waiting")
        self.radio_info.configure(text="  ·  ".join(info), fg=FG_AMBER if rs.outbox else FG_DIM)

        for mode, b in self.mode_btns.items():
            if mode == rs.owner:
                b.configure(fg=BG, bg=color, cursor="arrow")
            else:
                b.configure(fg=FG_TEAL, bg=PANEL_BG, cursor="hand2")

    def _switch_mode(self, mode: str):
        if self.switching:
            return
        rs = self.radio
        label = shack_mode.MODE_LABEL[mode]
        if rs and rs.owner == mode:
            return  # already there -- nothing to do
        if rs and mode != "apps" and rs.apps:
            messagebox.showerror(
                "Close the apps first",
                f"{', '.join(rs.apps)} is still open and using the radio.\n\nClose it, then press {label} again.",
                parent=self.root,
            )
            return

        if mode == "mercury" and rs and rs.outbox:
            shown = "\n".join("   " + f for f in rs.outbox[:10])
            more = f"\n   ...and {len(rs.outbox) - 10} more" if len(rs.outbox) > 10 else ""
            question = (f"{len(rs.outbox)} message(s) will TRANSMIT as soon as Mercury starts:\n\n"
                        f"{shown}{more}\n\nStart Mercury and send them?")
        else:
            question = f"Hand the FT-991A to {shack_mode.MODE_TEXT[mode]}?\n\nYou'll be asked for your password."
        if not messagebox.askyesno(f"Switch radio to {label}?", question, parent=self.root):
            return

        self.switching = True
        self.radio_lbl.configure(text=f"switching to {label}...", fg=FG_AMBER)
        self.radio_dot.itemconfig(self.radio_dot_id, fill=FG_AMBER)
        for b in self.mode_btns.values():
            b.configure(fg=FG_DIM, bg=PANEL_BG, cursor="watch")

        def worker():
            result = shack_mode.switch(mode)
            self.root.after(0, lambda: self._on_switch_done(mode, result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_switch_done(self, mode: str, result):
        self.switching = False
        if not result.success:
            messagebox.showerror("Radio not switched", result.message[-1500:], parent=self.root)
        elif "!!" in result.message:
            # it worked, but shack-mode had something to say (e.g. a retry)
            messagebox.showinfo(f"Radio: {shack_mode.MODE_LABEL[mode]}", result.message[-1500:], parent=self.root)
        self._refresh_one_immediately()

    # ---------------- service restart ----------------

    def _confirm_and_restart(self, service: str, on_done=None, parent=None):
        parent = parent or self.root
        if not messagebox.askyesno(
            "Restart service?",
            f"Restart {service}?\n\nThis will briefly interrupt it -- you'll be prompted to "
            f"authenticate first.",
            parent=parent,
        ):
            return

        widgets = self.rows.get(service)
        if widgets:
            widgets["restart_btn"].configure(text="...", fg=FG_DIM)
            widgets["state_lbl"].configure(text="restarting...", fg=FG_AMBER)

        def worker():
            result = restart_service(service)
            # pkexec/subprocess.run finished on this background thread --
            # hop back to the Tk main thread before touching any widgets.
            self.root.after(0, lambda: self._on_restart_done(service, result, on_done, parent))

        threading.Thread(target=worker, daemon=True).start()

    def _on_restart_done(self, service: str, result, on_done, parent):
        widgets = self.rows.get(service)
        if widgets:
            widgets["restart_btn"].configure(text="⟳ RESTART", fg=FG_AMBER)
        if not result.success:
            # parent (e.g. a per-service log Toplevel) may have been closed
            # while the restart was in flight -- fall back to the main
            # window rather than error trying to parent a dialog to it.
            if not parent or not parent.winfo_exists():
                parent = self.root
            messagebox.showerror("Restart failed", f"{service}:\n\n{result.message}", parent=parent)
        self._refresh_one_immediately()
        if on_done:
            on_done()

    def _refresh_one_immediately(self):
        # A restart just happened -- don't make the user wait up to
        # REFRESH_MS for the row to reflect it. This doesn't reschedule
        # the regular timer, the normal _refresh() loop is still running
        # independently every REFRESH_MS.
        statuses = query_states(SERVICES)
        self._apply_statuses(statuses)

    def _refresh(self):
        statuses = query_states(SERVICES)
        self._apply_statuses(statuses)
        self.root.after(REFRESH_MS, self._refresh)

    def _apply_statuses(self, statuses):
        merc = statuses.get("mercury.service")
        rs = shack_mode.radio_state(mercury_active=bool(merc and merc.category == GOOD))
        self._apply_radio(rs)
        paused = set(shack_mode.paused_units(rs))

        good = bad = other = 0
        for name, widgets in self.rows.items():
            status = statuses.get(name)
            if status is None:
                continue
            if name in paused and status.active_state == "inactive":
                # stopped on purpose by shack-mode -- not a problem
                widgets["dot"].itemconfig(widgets["dot_id"], fill=FG_DIM)
                widgets["name_lbl"].configure(fg=FG_DIM)
                widgets["state_lbl"].configure(
                    text=f"PAUSED ({rs.mode} mode) -- {format_since(rs.since)}", fg=FG_DIM)
                continue
            color = CATEGORY_COLOR[status.category]
            label = CATEGORY_LABEL[status.category]
            widgets["dot"].itemconfig(widgets["dot_id"], fill=color)
            widgets["name_lbl"].configure(fg=FG_WHITE if status.category != BAD else FG_RED)
            since_txt = format_since(status.since)
            detail = f"{label} ({status.sub_state}) -- {since_txt}"
            if status.error:
                detail = f"UNKNOWN -- {status.error}"
            widgets["state_lbl"].configure(text=detail, fg=color)

            if status.category == GOOD:
                good += 1
            elif status.category == BAD:
                bad += 1
            else:
                other += 1

        total = len(SERVICES) - len(paused & set(self.rows))
        if bad:
            self.banner_lbl.configure(text=f"⚠ {bad} SERVICE{'S' if bad != 1 else ''} DOWN", fg=FG_RED)
        elif other:
            self.banner_lbl.configure(text=f"◑ {other} NOT RUNNING", fg=FG_AMBER)
        elif good == total:
            self.banner_lbl.configure(text="✓ ALL SYSTEMS GO", fg=FG_GREEN)
        else:
            self.banner_lbl.configure(text="checking...", fg=FG_DIM)

    def _open_log_window(self, service: str):
        win = tk.Toplevel(self.root)
        win.title(f"Logs -- {service}")
        win.configure(bg=BG)
        win.geometry("700x420")

        header = tk.Label(win, text=service, font=self.f_row_name, fg=FG_TEAL, bg=BG)
        header.pack(anchor="w", padx=12, pady=(10, 4))

        text = ScrolledText(
            win, bg=PANEL_BG, fg=FG_WHITE, insertbackground=FG_WHITE, font=("DejaVu Sans Mono", 9), wrap="none"
        )
        text.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        def load():
            text.delete("1.0", "end")
            text.insert("1.0", "loading...")
            win.update_idletasks()
            log_text = recent_log(service)
            text.delete("1.0", "end")
            text.insert("1.0", log_text)
            text.see("end")

        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        refresh_btn = tk.Label(btn_row, text="⟳ REFRESH", font=self.f_label, fg=FG_TEAL, bg=BG, cursor="hand2")
        refresh_btn.pack(side="left")
        refresh_btn.bind("<Button-1>", lambda e: load())

        restart_btn = tk.Label(btn_row, text="⟳ RESTART", font=self.f_label, fg=FG_AMBER, bg=BG, cursor="hand2")
        restart_btn.pack(side="left", padx=(12, 0))
        # Passing win as the confirmation dialog's parent (not self.root)
        # keeps the "restart?" prompt on top of this log window rather
        # than popping behind it; reload the log once the restart finishes.
        def reload_if_still_open():
            # The restart can take a while (waiting on the user to
            # authenticate) -- if they closed this log window in the
            # meantime, win/text no longer exist and touching them would
            # raise, so just skip the reload rather than guard every line.
            if win.winfo_exists():
                win.after(500, load)

        restart_btn.bind(
            "<Button-1>",
            lambda e: self._confirm_and_restart(service, on_done=reload_if_still_open, parent=win),
        )

        load()


def main():
    # className sets WM_CLASS so GNOME (and other desktops) can correctly
    # match the running window to service-monitor.desktop's
    # StartupWMClass -- same reasoning as dnauto_gui.py.
    root = tk.Tk(className="shack-service-monitor")
    ServiceMonitorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
