#!/usr/bin/env bash
# Switch Shack Services to the GTK 4 window. Run from the unpacked folder:
#     ./install-gtk.sh            (installs into ~/shack-service-monitor)
# Safe to re-run. Rollback: SSM_UI=tk ~/shack-service-monitor/service_monitor_launcher.sh
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/shack-service-monitor}"
APPS="$HOME/.local/share/applications"

echo "== 1/4 packages"
sudo apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw; print(f"   GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}")'

echo "== 2/4 files -> $DEST"
mkdir -p "$DEST"
stamp=$(date +%Y%m%d%H%M%S)
for f in shack_services.py shack_askpass_client.py shack_askpass.py service_query.py shack_mode.py \
         service_monitor_gui.py shack-mode \
         service_monitor_launcher.sh test_notifier.py test_shack_mode.py test_setup_row.py install-gtk.sh \
         net.ag7jc.ShackServices.desktop .gitignore; do
    [ -f "$DEST/$f" ] && ! cmp -s "$SRC/$f" "$DEST/$f" && cp -p "$DEST/$f" "$DEST/$f.bak.$stamp"
    [ "$SRC" = "$DEST" ] || cp -p "$SRC/$f" "$DEST/$f"
done
chmod +x "$DEST"/shack_services.py "$DEST"/shack_askpass_client.py "$DEST"/service_monitor_launcher.sh
[ -f "$DEST/shack_askpass.py" ] && chmod +x "$DEST/shack_askpass.py"
[ -d "$SRC/assets" ] && [ "$SRC" != "$DEST" ] && cp -r "$SRC/assets" "$DEST/"
rm -rf "$DEST/__pycache__"
(cd "$DEST" && /usr/bin/python3 -m unittest -q test_notifier test_shack_mode 2>&1 | tail -1)

echo "== 3/4 launcher"
mkdir -p "$APPS"
sed "s|@APP_DIR@|$DEST|g" "$SRC/net.ag7jc.ShackServices.desktop" > "$APPS/net.ag7jc.ShackServices.desktop"
if [ -f "$APPS/service-monitor.desktop" ]; then
    mv "$APPS/service-monitor.desktop" "$DEST/service-monitor.desktop.old"
    echo "   old launcher moved to $DEST/service-monitor.desktop.old"
fi
update-desktop-database "$APPS" 2>/dev/null || true

echo "== 4/4 service list"
echo "   ~/.config/shack-services/services.txt is created on first start (your current 10 services)"
echo
echo "Done. Close the old window, then start Shack Services from the app grid"
echo "(re-pin it to the dock if it was pinned -- the launcher's name changed)."
echo "Old window, any time:  SSM_UI=tk $DEST/service_monitor_launcher.sh"
