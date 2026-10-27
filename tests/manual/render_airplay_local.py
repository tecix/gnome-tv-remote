"""Render native local-video states and complete dialogs without any TV access."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gsk, Gtk

from gnome_adb_tv_remote.ui.airplay_dialog import AirPlayDialog
from gnome_adb_tv_remote.ui.local_video_group import LocalVideoGroup


def settle() -> None:
    until = time.monotonic() + 0.3
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


def capture(widget: Gtk.Widget, path: Path) -> None:
    settle()
    width, height = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    renderer = Gsk.Renderer.new_for_surface(widget.get_native().get_surface())
    bounds = Graphene.Rect()
    bounds.init(0, 0, width, height)
    texture = renderer.render_texture(node, bounds)
    texture.save_to_png(str(path))
    renderer.unrealize()
    print(path, flush=True)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    schemas = Path("/tmp/tvremote-r7-schemas")
    schemas.mkdir(exist_ok=True)
    subprocess.run(
        ["glib-compile-schemas", "--targetdir=" + str(schemas), str(root / "data")], check=True
    )
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    os.environ["GSETTINGS_BACKEND"] = "memory"
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    output = Path.home() / "tinytv-airplay-work/ui"
    output.mkdir(parents=True, exist_ok=True)
    states = ("idle", "probing", "direct", "converting", "playing", "error")
    for theme in ("light", "dark"):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if theme == "light" else Adw.ColorScheme.FORCE_DARK
        )
        for state in states:
            group = LocalVideoGroup(lambda *_: None, lambda *_: None, lambda *_: False)
            metadata = "HEVC · 3840×2160 · portrait · 0:51 · converts for TV"
            messages = {
                "probing": "Reading video details…",
                "direct": "Ready to play unchanged",
                "converting": "Playing on Living Room TV · conversion continues",
                "playing": "Playing on Living Room TV",
                "error": "The video could not be read. Choose another file or retry.",
            }
            group.set_state(
                state,
                filename="" if state == "idle" else "PXL_20261003_213139091.mp4",
                metadata=(
                    "Or drop a video file here"
                    if state == "idle"
                    else ("H.264 · 1920×1080 · 0:51" if state == "direct" else metadata)
                ),
                message=messages.get(state, ""),
                fraction=0.4 if state in ("converting", "playing") else None,
                encoder="h264_qsv" if state == "converting" else None,
                receiver="Living Room TV" if state == "playing" else None,
            )
            assert (
                group.file_row.get_activatable_widget().get_icon_name() == "document-open-symbolic"
            )
            expected_icon = {
                "direct": "emblem-ok-symbolic",
                "playing": "media-playback-start-symbolic",
                "error": "dialog-error-symbolic",
            }.get(state)
            if expected_icon:
                assert group.status_icon.get_icon_name() == expected_icon
            assert group.spinner.get_spinning() == (state in ("probing", "converting"))
            assert group.progress_revealer.get_reveal_child() == (state == "converting")
            if state == "converting":
                assert group.progress.get_fraction() == 0.4
                assert group.status_row.get_subtitle() == "40 % · Intel QSV"
                assert group.progress_revealer.get_parent() == group.status_row.get_parent()
            if state == "playing":
                assert group.status_row.get_subtitle() == "Playing on Living Room TV · 40 %"
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for setter in (
                box.set_margin_top,
                box.set_margin_bottom,
                box.set_margin_start,
                box.set_margin_end,
            ):
                setter(24)
            box.append(group)
            window = Adw.Window(default_width=460)
            window.set_content(box)
            window.present()
            capture(window, output / f"local-{theme}-{state}.png")
            window.destroy()
            settle()
        parent = Adw.Window(default_width=540, default_height=1000)
        parent.present()
        settings = Gio.Settings.new("io.github.tecix.TvRemote")
        dialog = AirPlayDialog(settings, lambda: None)
        dialog._receiver_row.set_text("192.168.2.192")
        dialog._local_group.set_state(
            "direct",
            filename="PXL_20261003_213139091.mp4",
            metadata="H.264 · 1920×1080 · 0:51",
            message="Ready to play unchanged",
        )
        dialog.present(parent)  # Intentionally bypass open(): no status polling/network.
        capture(parent, output / f"airplay-{theme}.png")
        dialog._local_error("The video could not be read", lambda: None)
        capture(parent, output / f"airplay-{theme}-error-toast.png")
        dialog.force_close()
        parent.destroy()
        settle()

    parent = Adw.Window(default_width=360, default_height=760)
    parent.present()
    dialog = AirPlayDialog(Gio.Settings.new("io.github.tecix.TvRemote"), lambda: None)
    dialog._local_group.set_state(
        "converting",
        filename="PXL_20261003_213139091.mp4",
        metadata="HEVC · 3840×2160 · portrait · 0:51 · converts for TV",
        message="Preparing video for the TV",
        fraction=0.4,
    )
    dialog.present(parent)
    capture(parent, output / "airplay-dark-narrow.png")
    assert dialog._play_button.get_mapped()
    dialog.force_close()
    parent.destroy()
    settle()


if __name__ == "__main__":
    main()
