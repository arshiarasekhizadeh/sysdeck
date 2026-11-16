#!/usr/bin/env bash
# Remove SYSDECK for the current user.   ./uninstall.sh [--purge]   (--purge also deletes settings and logs)
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
"$HOME/.local/bin/sysdeck" quit >/dev/null 2>&1 || true
rm -rf "$DATA/sysdeck"
rm -f "$HOME/.local/bin/sysdeck" "$DATA/applications/sysdeck.desktop" \
      "$DATA/icons/hicolor/scalable/apps/sysdeck.svg" "$CONF/autostart/sysdeck.desktop"
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$CONF/sysdeck" "${XDG_STATE_HOME:-$HOME/.local/state}/sysdeck"
fi
update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
echo "SYSDECK removed."
