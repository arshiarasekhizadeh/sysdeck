#!/usr/bin/env bash
# Install SYSDECK for the current user (no root needed).
#
#   ./install.sh               install to ~/.local
#   ./install.sh --autostart   also start SYSDECK when you log in
#   ./install.sh --check       only check dependencies
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$DATA/sysdeck"
AUTOSTART=0; CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

bold "Checking dependencies"
missing=0
if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  ok "python $(python3 -c 'import platform; print(platform.python_version())')"
else
  fail "python 3.10+ is required"; missing=1
fi
if python3 -c 'import psutil' 2>/dev/null; then ok "psutil"; else fail "psutil"; missing=1; fi
if python3 -c 'import PyQt6.QtWebEngineWidgets' 2>/dev/null; then ok "PyQt6 + QtWebEngine"; else fail "PyQt6 + QtWebEngine"; missing=1; fi
command -v nvidia-smi >/dev/null && ok "nvidia-smi (GPU panel)" || warn "nvidia-smi not found: GPU panel will be empty (NVIDIA only for now)"
command -v ollama >/dev/null || curl -fs -m 1 http://127.0.0.1:11434/api/version >/dev/null 2>&1 \
  && ok "ollama" || warn "ollama not found: the AI Core panel will show 'offline'"
if journalctl -u ollama -n 1 --no-pager >/dev/null 2>&1 && [[ -n "$(journalctl -u ollama -n 1 --no-pager -q 2>/dev/null)" ]]; then
  ok "can read the ollama journal (live tok/s, layer offload)"
else
  warn "can't read the ollama systemd journal; live tok/s needs it:  sudo usermod -aG systemd-journal \$USER  (then log out/in)"
fi
command -v busctl >/dev/null && busctl --system status org.freedesktop.UDisks2 >/dev/null 2>&1 \
  && ok "udisks2 (drive health)" || warn "udisks2 not running: drive health (wear, TB written) won't show"
command -v docker >/dev/null && ok "docker (optional)" || true
command -v busctl >/dev/null && busctl --user status >/dev/null 2>&1 \
  && ok "session D-Bus (media controls)" || warn "busctl --user unavailable: the Media panel stays hidden"

if (( missing )); then
  echo
  bold "Install the missing packages, then run ./install.sh again:"
  echo "  Arch / EndeavourOS / Manjaro:  sudo pacman -S python-psutil python-pyqt6 python-pyqt6-webengine"
  echo "  Debian / Ubuntu (24.04+):      sudo apt install python3-psutil python3-pyqt6 python3-pyqt6.qtwebengine"
  echo "  Fedora:                        sudo dnf install python3-psutil python3-pyqt6 python3-pyqt6-webengine"
  echo "  any distro (venv/pip):         pip install -r requirements.txt"
  exit 1
fi
(( CHECK_ONLY )) && exit 0

echo
bold "Installing"
SYSDECK_HOME="$APP_DIR" "$SRC/bin/sysdeck" quit >/dev/null 2>&1 || true  # stop the installed copy, not the source tree
mkdir -p "$APP_DIR/static" "$BIN_DIR" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
install -m755 "$SRC/sysdeck/app.py" "$SRC/sysdeck/server.py" "$APP_DIR/"
install -m644 "$SRC/sysdeck/sysdeck.svg" "$APP_DIR/"
install -m644 "$SRC"/sysdeck/static/* "$APP_DIR/static/"
install -m755 "$SRC/bin/sysdeck" "$BIN_DIR/sysdeck"
install -m644 "$SRC/sysdeck/sysdeck.svg" "$DATA/icons/hicolor/scalable/apps/sysdeck.svg"
sed "s|@BIN@|$BIN_DIR/sysdeck|g" "$SRC/share/sysdeck.desktop.in" > "$DATA/applications/sysdeck.desktop"
ok "app      → $APP_DIR"
ok "launcher → $BIN_DIR/sysdeck"
ok "menu entry + icon"
if (( AUTOSTART )); then
  mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  cat > "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sysdeck.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=SYSDECK
Comment=Start the SYSDECK system monitor
Exec=sh -c "sleep 3; exec $BIN_DIR/sysdeck"
Icon=sysdeck
Terminal=false
X-KDE-autostart-phase=2
X-GNOME-Autostart-enabled=true
DESK
  ok "autostart on login"
fi
update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$DATA/icons/hicolor" >/dev/null 2>&1 || true
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not in your PATH; add it, or start SYSDECK from the app menu" ;; esac

echo
bold "Done. Start it with:  sysdeck   (or 'SYSDECK' in your app menu)"
echo "  Pick the monitor and zoom from the tray icon; settings: ~/.config/sysdeck/config.json"
