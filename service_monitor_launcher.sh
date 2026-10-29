#!/usr/bin/env bash
# Shack Services launcher (the app-grid icon runs this).
#   default:          the GTK 4 window (shack_services.py)
#   SSM_UI=tk ...     the old Tk window (service_monitor_gui.py)
# Falls back to Tk by itself if GTK 4 / libadwaita aren't installed.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
PY=/usr/bin/python3      # system Python: that's where python3-gi lives
if [ "${SSM_UI:-gtk}" != tk ] && "$PY" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Adw; assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 4)' 2>/dev/null; then
    exec "$PY" shack_services.py "$@"
fi
[ "${SSM_UI:-gtk}" != tk ] && echo "GTK 4 / libadwaita >= 1.4 not available -- starting the Tk window" >&2
exec "$PY" service_monitor_gui.py "$@"
