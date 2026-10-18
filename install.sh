#!/usr/bin/env bash
# Per-user install: ~/.local/share/121gw-app (code), ~/.local/bin/121gw (launcher), desktop entry + icon.
# Uninstall with: ./install.sh --uninstall
set -euo pipefail
APP_ID=io.github.bigtimer_dev.Meter121GW
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
LIBDIR="$PREFIX/121gw-app"
BIN="$HOME/.local/bin/121gw"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$LIBDIR" "$BIN" "$PREFIX/applications/$APP_ID.desktop" \
         "$PREFIX/icons/hicolor/scalable/apps/$APP_ID.svg"
  update-desktop-database -q "$PREFIX/applications" 2>/dev/null || true
  echo "Removed. Settings/sessions are kept in ~/.config/121gw and ~/.local/share/121gw."
  exit 0
fi

python3 - <<'PY'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12), "GTK >= 4.12 required"
assert Adw.get_minor_version() >= 6, "libadwaita >= 1.6 required"
PY

mkdir -p "$LIBDIR" "$(dirname "$BIN")" "$PREFIX/applications" "$PREFIX/icons/hicolor/scalable/apps"
rm -rf "$LIBDIR/gw121"
cp -r "$SRC/gw121" "$LIBDIR/"
find "$LIBDIR" -name __pycache__ -prune -exec rm -rf {} +

cat > "$BIN" <<SH
#!/bin/sh
exec env PYTHONPATH="$LIBDIR" python3 -m gw121 "\$@"
SH
chmod +x "$BIN"

sed "s|@BIN@|$BIN|g" "$SRC/data/$APP_ID.desktop" > "$PREFIX/applications/$APP_ID.desktop"
cp "$SRC/data/$APP_ID.svg" "$PREFIX/icons/hicolor/scalable/apps/"
update-desktop-database -q "$PREFIX/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$PREFIX/icons/hicolor" 2>/dev/null || true
echo "Installed. Run '121gw' (or '121gw --demo'), or launch '121GW Multimeter' from your app launcher."
