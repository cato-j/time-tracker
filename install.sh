#!/usr/bin/env bash
# Installs the Time Tracker launcher into the app menu and onto the desktop.
# Re-run after moving the project folder. Use "./install.sh --uninstall" to remove.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
NAME="time-tracker"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$APPS_DIR/$NAME.desktop" "$DESKTOP_DIR/$NAME.desktop" "$ICON_DIR/$NAME.svg"
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    echo "Removed launcher and icon."
    exit 0
fi

mkdir -p "$APPS_DIR" "$ICON_DIR" "$DESKTOP_DIR"
cp "$APP_DIR/icon.svg" "$ICON_DIR/$NAME.svg"
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/$NAME.desktop" > "$APPS_DIR/$NAME.desktop"
cp "$APPS_DIR/$NAME.desktop" "$DESKTOP_DIR/$NAME.desktop"
chmod +x "$DESKTOP_DIR/$NAME.desktop"
# GNOME shows a desktop launcher only after it has been marked trusted.
gio set "$DESKTOP_DIR/$NAME.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$APPS_DIR" 2>/dev/null || true
gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo "Installed:"
echo "  app menu : $APPS_DIR/$NAME.desktop"
echo "  desktop  : $DESKTOP_DIR/$NAME.desktop"
echo "  icon     : $ICON_DIR/$NAME.svg"
