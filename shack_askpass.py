#!/usr/bin/env python3
"""
Password window for `sudo -A`, used when Shack Services switches the radio
(shack-mode). sudo runs this with its prompt as the first argument; we print
the password on stdout and exit 0, or exit 1 if cancelled. Nothing is saved.
"""
import sys
import tkinter as tk

BG, PANEL_BG, FG_TEAL, FG_WHITE, FG_DIM, BORDER = "#05080d", "#0b1220", "#28e0e0", "#e8edf4", "#4a5568", "#1c2740"


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
    result = {"pw": None}

    root = tk.Tk(className="shack-askpass")
    root.title("Shack mode -- authenticate")
    root.configure(bg=BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = tk.Frame(root, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=2)
    frame.pack(padx=10, pady=10)
    tk.Label(frame, text="SWITCH RADIO", font=("DejaVu Sans Mono", 12, "bold"), fg=FG_TEAL, bg=PANEL_BG).pack(padx=16, pady=(12, 2))
    tk.Label(frame, text=prompt.replace("[sudo: authenticate] ", "").strip() or "Password:",
             font=("DejaVu Sans", 10), fg=FG_WHITE, bg=PANEL_BG).pack(padx=16)
    entry = tk.Entry(frame, show="•", width=28, bg=BG, fg=FG_WHITE, insertbackground=FG_WHITE, relief="flat")
    entry.pack(padx=16, pady=10, ipady=4)

    def ok(_e=None):
        result["pw"] = entry.get()
        root.destroy()

    def cancel(_e=None):
        root.destroy()

    btns = tk.Frame(frame, bg=PANEL_BG)
    btns.pack(pady=(0, 12))
    for text, cmd, color in (("OK", ok, FG_TEAL), ("CANCEL", cancel, FG_DIM)):
        b = tk.Label(btns, text=text, font=("DejaVu Sans", 9, "bold"), fg=color, bg=PANEL_BG, cursor="hand2", padx=12)
        b.pack(side="left")
        b.bind("<Button-1>", cmd)
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.after(100, lambda: (root.focus_force(), entry.focus_set()))
    root.mainloop()

    if result["pw"] is None:
        sys.exit(1)
    sys.stdout.write(result["pw"] + "\n")


if __name__ == "__main__":
    main()
