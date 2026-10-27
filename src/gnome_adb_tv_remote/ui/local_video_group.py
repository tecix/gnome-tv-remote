"""Native Libadwaita local-video selection and preparation status."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango


class LocalVideoGroup(Adw.PreferencesGroup):
    """A boxed file row and native status controls, without custom styling."""

    def __init__(self, choose: Callable, clear: Callable, dropped: Callable) -> None:
        super().__init__(
            title="Local Video", description="Choose or drop a video from this device."
        )
        self.file_row = Adw.ActionRow(
            title="No video selected", subtitle="Or drop a video file here"
        )
        self.file_row.set_use_markup(False)
        self.file_row.set_title_lines(1)
        self.file_row.set_subtitle_lines(2)
        self.file_row.add_prefix(Gtk.Image(icon_name="video-x-generic-symbolic"))
        choose_button = Gtk.Button(icon_name="document-open-symbolic")
        choose_button.add_css_class("flat")
        choose_button.set_valign(Gtk.Align.CENTER)
        choose_button.set_tooltip_text("Choose Video…")
        choose_button.update_property([Gtk.AccessibleProperty.LABEL], ["Choose Video…"])
        choose_button.connect("clicked", choose)
        self.file_row.add_suffix(choose_button)
        self.file_row.set_activatable_widget(choose_button)
        self.clear_button = Gtk.Button(
            icon_name="edit-clear-symbolic",
            tooltip_text="Clear local video and use the video link",
        )
        self.clear_button.add_css_class("flat")
        self.clear_button.add_css_class("circular")
        self.clear_button.set_valign(Gtk.Align.CENTER)
        self.clear_button.connect("clicked", clear)
        self.file_row.add_suffix(self.clear_button)
        self.add(self.file_row)
        self.status_row = Adw.ActionRow(title="Ready to choose a video")
        self.status_row.set_use_markup(False)
        self.status_row.set_subtitle_lines(2)
        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.status_row.add_prefix(self.spinner)
        self.status_icon = Gtk.Image()
        self.status_row.add_prefix(self.status_icon)
        self.status_container = Adw.PreferencesRow()
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        status_box.append(self.status_row)
        self.status_container.set_child(status_box)
        self.add(self.status_container)
        self.progress = Gtk.ProgressBar(show_text=False)
        self.progress.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Video conversion progress"]
        )
        self.progress.set_margin_top(8)
        self.progress.set_margin_bottom(12)
        self.progress.set_margin_start(12)
        self.progress.set_margin_end(12)
        self.progress_revealer = Gtk.Revealer(child=self.progress)
        status_box.append(self.progress_revealer)
        self._pulse_id: int | None = None
        self.connect("unmap", lambda *_: self._stop_pulse())
        target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        target.connect("drop", lambda _target, file, _x, _y: dropped(file))
        self.add_controller(target)
        self._configure_file_labels()
        self.set_state("idle")

    def _configure_file_labels(self) -> None:
        """Configure ActionRow's native labels; it has no title-ellipsize property."""

        def visit(widget: Gtk.Widget) -> None:
            if isinstance(widget, Gtk.Label):
                if widget.has_css_class("title"):
                    widget.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                    widget.set_lines(1)
                elif widget.has_css_class("subtitle"):
                    widget.set_wrap(True)
                    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                    widget.set_ellipsize(Pango.EllipsizeMode.NONE)
                    widget.set_lines(2)
            child = widget.get_first_child()
            while child is not None:
                visit(child)
                child = child.get_next_sibling()

        visit(self.file_row)

    def _stop_pulse(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None

    def set_state(
        self,
        state: str,
        *,
        filename: str | None = None,
        metadata: str | None = None,
        message: str = "",
        fraction: float | None = None,
        encoder: str | None = None,
        receiver: str | None = None,
    ) -> None:
        """Update on the GTK thread; native controls respect the system theme."""
        self._stop_pulse()
        if filename is not None:
            self.file_row.set_title(filename or "No video selected")
            self.file_row.set_tooltip_text(filename or "Choose a local video file")
            self.clear_button.set_visible(bool(filename))
        if metadata is not None:
            self.file_row.set_subtitle(metadata)
        titles = {
            "idle": "Ready to choose a video",
            "probing": "Checking video…",
            "direct": "Plays directly",
            "converting": "Converting for the TV…",
            "playing": "Playing on TV",
            "error": "Couldn’t prepare video",
        }
        self.status_row.set_title(titles[state])
        subtitle = message
        if state in ("converting", "playing"):
            percent = f"{max(0, min(1, fraction)) * 100:.0f} %" if fraction is not None else ""
            encoder_label = {
                "h264_qsv": "Intel QSV",
                "h264_vaapi": "VA-API",
                "libx264": "Software",
                "libopenh264": "Software",
            }.get(encoder, "")
            if receiver:
                subtitle = " · ".join(part for part in (f"Playing on {receiver}", percent) if part)
            elif state == "converting":
                subtitle = " · ".join(part for part in (percent, encoder_label) if part) or message
        self.status_row.set_subtitle(subtitle)
        self.status_container.set_visible(state != "idle")
        busy = state in ("probing", "converting")
        self.spinner.set_spinning(busy)
        self.spinner.set_visible(busy)
        icons = {
            "direct": "emblem-ok-symbolic",
            "playing": "media-playback-start-symbolic",
            "error": "dialog-error-symbolic",
        }
        self.status_icon.set_from_icon_name(icons.get(state, "emblem-ok-symbolic"))
        self.status_icon.set_visible(not busy)
        animations = Gtk.Settings.get_default().get_property("gtk-enable-animations")
        self.progress_revealer.set_transition_duration(200 if animations else 0)
        self.progress_revealer.set_reveal_child(state == "converting")
        self.progress.set_fraction(max(0, min(1, fraction or 0)))
        self.progress.set_text(f"{fraction:.0%}" if fraction is not None else "Preparing…")
        if state == "converting" and fraction is None and animations:

            def pulse() -> bool:
                self.progress.pulse()
                return True

            self._pulse_id = GLib.timeout_add(150, pulse)
        if state == "idle":
            self.clear_button.set_visible(False)
