"""
Pairing Flow for the Android TV Remote protocol v2.

Starts pairing with a TV (which then shows a 6-character code on screen) and
asks the user to type that code here. Used whenever the TV doesn't know this
computer's certificate yet.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.remote_v2 import PairingError, PairingSession, RemoteError  # noqa: E402


def run_pairing(parent: Gtk.Widget, ip: str, on_finished: Callable[[bool, str | None], None]) -> None:
    """Pair with the TV at `ip`.

    `on_finished(success, error_message)` is called on the main thread once pairing
    succeeds, fails or is cancelled (cancelled: success False, message None).
    """
    session = PairingSession(ip, client_name=f"TV Remote ({socket.gethostname() or 'Linux'})")

    def start() -> None:
        try:
            tv_name = session.start()
        except RemoteError as e:
            GLib.idle_add(on_finished, False, str(e) or "Pairing failed")
            return
        GLib.idle_add(_ask_for_code, parent, session, tv_name, None, on_finished)

    threading.Thread(target=start, name="pairing-start", daemon=True).start()


def _ask_for_code(
    parent: Gtk.Widget,
    session: PairingSession,
    tv_name: str,
    error: str | None,
    on_finished: Callable[[bool, str | None], None],
) -> bool:
    body = "Enter the code shown on the TV screen."
    if error:
        body = f"{error}\n\n{body}"
    dialog = Adw.AlertDialog(heading=f"Pair with {tv_name}", body=body)

    entry = Gtk.Entry(placeholder_text="e.g. 4F2A9C", max_length=6)
    entry.set_alignment(0.5)
    entry.set_activates_default(True)
    dialog.set_extra_child(entry)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("pair", "Pair")
    dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_response_enabled("pair", False)
    dialog.set_default_response("pair")
    dialog.set_close_response("cancel")

    def on_changed(*_args) -> None:
        dialog.set_response_enabled("pair", len(entry.get_text().strip()) == 6)

    entry.connect("changed", on_changed)

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response != "pair":
            session.close()
            on_finished(False, None)
            return
        code = entry.get_text()

        def finish() -> None:
            try:
                session.finish(code)
            except PairingError as e:
                if session.is_open:
                    # Wrong code caught locally: the TV still shows the same code, ask again.
                    GLib.idle_add(_ask_for_code, parent, session, tv_name, str(e), on_finished)
                else:
                    GLib.idle_add(on_finished, False, str(e))
                return
            GLib.idle_add(on_finished, True, None)

        threading.Thread(target=finish, name="pairing-finish", daemon=True).start()

    dialog.connect("response", on_response)
    dialog.present(parent)
    entry.grab_focus()
    return False
