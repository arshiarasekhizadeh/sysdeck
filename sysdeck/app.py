#!/usr/bin/env python3
"""SYSDECK desktop app.

A native Qt window (no browser) that runs the SYSDECK backend in-process and
renders the dashboard full-screen on a chosen monitor, with a tray icon to
show/hide it, pick the screen and quit. Single instance: launching it again
just shows the running window.

Config: ~/.config/sysdeck/config.json, e.g.
        {"screen": "HDMI-A-1", "fullscreen": true, "zoom": 1.25, "privacy": false}
        An empty "screen" picks a secondary monitor automatically.
Keys:   F11 full-screen · Ctrl +/-/0 zoom · Ctrl+R reload · Ctrl+W hide to tray · Ctrl+Q quit
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Chromium flags for the embedded web engine: keep it lean and GPU-accelerated.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-gpu-rasterization --ignore-gpu-blocklist --disable-features=Translate",
)

from PyQt6.QtCore import QUrl, Qt  # noqa: E402
from PyQt6.QtGui import QAction, QActionGroup, QColor, QGuiApplication, QIcon, QKeySequence, QShortcut  # noqa: E402
from PyQt6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEngineSettings  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon  # noqa: E402

APP_ID = "sysdeck"
ICON = HERE / "sysdeck.svg"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sysdeck" / "config.json"
DEFAULTS = {"screen": "", "fullscreen": True, "port": 8790, "zoom": 1.0, "privacy": False}
ZOOMS = [1.0, 1.15, 1.25, 1.4, 1.5, 1.65, 1.75, 2.0]


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_backend(port: int) -> int:
    """Start the in-process backend on `port`, or the next free port."""
    import server

    for p in range(port, port + 20):
        if port_free(p):
            server.start(p)
            return p
    raise RuntimeError(f"no free port in {port}..{port + 19}")


class Deck(QMainWindow):
    def __init__(self, app: QApplication, cfg: dict, url: str):
        super().__init__()
        self.app, self.cfg, self.base_url = app, cfg, url
        self.quitting = False
        self.setWindowTitle("SYSDECK")
        self.setWindowIcon(QIcon(str(ICON)))
        self.resize(1600, 900)

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.page().setBackgroundColor(QColor("#05070b"))
        st = self.view.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, False)
        self.view.setZoomFactor(float(cfg.get("zoom", DEFAULTS["zoom"])))
        self.view.setUrl(QUrl(self.page_url()))
        # zoom is re-applied after each load (QtWebEngine resets it per page load)
        self.view.loadFinished.connect(lambda _ok: self.view.setZoomFactor(self.zoom()))
        self.setCentralWidget(self.view)

        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.view.reload)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self.hide)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.quit)
        for seq in ("Ctrl++", "Ctrl+="):
            QShortcut(QKeySequence(seq), self, activated=lambda: self.step_zoom(+1))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.step_zoom(-1))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=lambda: self.set_zoom(1.0))

    def page_url(self) -> str:
        return self.base_url + ("?privacy=1" if self.cfg.get("privacy") else "")

    def set_privacy(self, on: bool):
        """Privacy mode hides host name, IP addresses and project folders (for screen sharing)."""
        self.cfg["privacy"] = bool(on)
        save_config(self.cfg)
        self.view.setUrl(QUrl(self.page_url()))

    # ── zoom ──
    def zoom(self) -> float:
        return float(self.cfg.get("zoom", DEFAULTS["zoom"]))

    def set_zoom(self, z: float):
        z = max(0.5, min(3.0, round(z, 2)))
        self.cfg["zoom"] = z
        save_config(self.cfg)
        self.view.setZoomFactor(z)

    def step_zoom(self, d: int):
        cur = self.zoom()
        if d > 0:
            nxt = next((z for z in ZOOMS if z > cur + 1e-3), ZOOMS[-1])
        else:
            nxt = next((z for z in reversed(ZOOMS) if z < cur - 1e-3), ZOOMS[0])
        self.set_zoom(nxt)

    # ── placement ──
    def target_screen(self):
        screens = QGuiApplication.screens()
        want = self.cfg.get("screen")
        match = next((s for s in screens if s.name() == want), None)
        if match:
            return match
        primary = QGuiApplication.primaryScreen()
        return next((s for s in screens if s is not primary), primary)

    def present(self):
        """(Re)map the window on the target screen. On Wayland a mapped window
        can't move itself between outputs, so hide → set screen → show."""
        scr = self.target_screen()
        if self.isVisible():
            self.hide()
        self.setScreen(scr)
        self.setGeometry(scr.availableGeometry())
        if self.cfg.get("fullscreen", True):
            self.showFullScreen()
        else:
            self.showMaximized()
        self.raise_()
        self.activateWindow()

    def move_to(self, name: str):
        self.cfg["screen"] = name
        save_config(self.cfg)
        self.present()

    def toggle_fullscreen(self):
        self.cfg["fullscreen"] = not self.isFullScreen()
        save_config(self.cfg)
        self.present()

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.present()

    def quit(self):
        self.quitting = True
        self.app.quit()

    def closeEvent(self, ev):  # noqa: N802 - close hides to tray, Ctrl+Q / tray quits
        if self.quitting:
            ev.accept()
        else:
            ev.ignore()
            self.hide()


def build_tray(deck: Deck) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(QIcon(str(ICON)), deck)
    tray.setToolTip("SYSDECK — system monitor")
    menu = QMenu()

    menu.addAction(QAction("Show / hide", menu, triggered=deck.toggle_visible))

    screens_menu = menu.addMenu("Screen")
    group = QActionGroup(screens_menu)

    def refresh_screens():
        screens_menu.clear()
        current = deck.target_screen().name()
        for s in QGuiApplication.screens():
            g = s.geometry()
            act = QAction(f"{s.name()}  ({g.width()}×{g.height()})", screens_menu, checkable=True)
            act.setChecked(s.name() == current)
            act.triggered.connect(lambda _=False, n=s.name(): deck.move_to(n))
            group.addAction(act)
            screens_menu.addAction(act)

    menu.aboutToShow.connect(refresh_screens)

    fs = QAction("Full screen", menu, checkable=True)
    fs.triggered.connect(deck.toggle_fullscreen)
    menu.aboutToShow.connect(lambda: fs.setChecked(deck.isFullScreen()))
    menu.addAction(fs)
    zoom_menu = menu.addMenu("Zoom")
    zgroup = QActionGroup(zoom_menu)

    def refresh_zoom():
        zoom_menu.clear()
        for z in ZOOMS:
            act = QAction(f"{round(z * 100)}%", zoom_menu, checkable=True)
            act.setChecked(abs(z - deck.zoom()) < 1e-3)
            act.triggered.connect(lambda _=False, z=z: deck.set_zoom(z))
            zgroup.addAction(act)
            zoom_menu.addAction(act)

    menu.aboutToShow.connect(refresh_zoom)
    priv = QAction("Privacy mode (hide host, IPs, projects)", menu, checkable=True)
    priv.triggered.connect(deck.set_privacy)
    menu.aboutToShow.connect(lambda: priv.setChecked(bool(deck.cfg.get("privacy"))))
    menu.addAction(priv)
    menu.addAction(QAction("Reload", menu, triggered=deck.view.reload))
    menu.addSeparator()
    menu.addAction(QAction("Quit SYSDECK", menu, triggered=deck.quit))

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: deck.toggle_visible() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    tray.show()
    return tray


def main() -> int:
    QGuiApplication.setDesktopFileName(APP_ID)  # Wayland app_id → taskbar icon, KWin rules
    app = QApplication(sys.argv)
    app.setApplicationName("SYSDECK")
    app.setApplicationDisplayName("SYSDECK")
    app.setWindowIcon(QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)

    # Single instance: if one is running, tell it what to do and exit.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        sock.write(cmd.encode())
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return 0
    if cmd == "quit":
        return 0

    cfg = load_config()
    port = start_backend(int(os.environ.get("SYSDECK_PORT", cfg.get("port", 8790))))
    deck = Deck(app, cfg, f"http://127.0.0.1:{port}/")
    deck.tray = build_tray(deck)

    QLocalServer.removeServer(APP_ID)
    ipc = QLocalServer(app)
    ipc.listen(APP_ID)

    def on_ipc():
        conn = ipc.nextPendingConnection()
        if conn is None:
            return

        def read():
            msg = bytes(conn.readAll()).decode(errors="replace").strip()
            {"quit": deck.quit, "hide": deck.hide, "toggle": deck.toggle_visible}.get(msg, deck.present)()

        conn.readyRead.connect(read)

    ipc.newConnection.connect(on_ipc)

    if cmd != "--tray":
        deck.present()
    print(f"SYSDECK running · backend http://127.0.0.1:{port}/ · screen {deck.target_screen().name()}", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
