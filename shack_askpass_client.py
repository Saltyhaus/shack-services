#!/usr/bin/env python3
"""
SUDO_ASKPASS helper for Shack Services.

sudo runs this when it needs the password, with its prompt as argv[1]. We
ask the running Shack Services window (over a private socket it named in
SHACK_ASKPASS_SOCK) to show its password dialog, then print the answer on
stdout for sudo and exit 0 -- or exit 1 if it was cancelled. Nothing is
stored; the password only passes from the dialog to sudo.

Doing the dialog inside the app (instead of a separate window) keeps it on
top of the app on Wayland, where a new window from a background process may
not get focus, and makes every password prompt look the same.

No SHACK_ASKPASS_SOCK (e.g. the old Tk window): falls back to the old
stand-alone shack_askpass.py window.
"""
import json
import os
import socket
import sys


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
    path = os.environ.get("SHACK_ASKPASS_SOCK")
    if not path:
        old = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shack_askpass.py")
        os.execv(sys.executable, [sys.executable, old, prompt])

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.sendall((json.dumps({"prompt": prompt}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        reply = json.loads(buf.decode() or "{}")
    except (OSError, ValueError):
        sys.exit(1)
    if not reply.get("ok"):
        sys.exit(1)
    sys.stdout.write(reply.get("password", "") + "\n")


if __name__ == "__main__":
    main()
