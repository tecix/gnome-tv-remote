"""
Application entry point and GTK Application class.

This module defines the main Adw.Application subclass and the entry point
function for the TV Remote application.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .ui.main_window import MainWindow  # noqa: E402


APP_ID = "io.github.tecix.TvRemote"


class App(Adw.Application):
    """Main application class for TV Remote.
    
    Handles application lifecycle, global actions, and window management.
    Uses Adw.Application for Libadwaita integration.
    """

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

        self._create_actions()

    def _create_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_activate(self) -> None:  # type: ignore[override]
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        win.present()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv

    GLib.set_prgname("tv-remote")
    Adw.init()
    app = App()
    return int(app.run(argv))


