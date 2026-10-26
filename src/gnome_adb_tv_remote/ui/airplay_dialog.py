"""
AirPlay Dialog.

This module provides the AirPlayDialog class, which sends a video link and an
optional subtitle link to an AirPlay video receiver (TinyTV Receiver on the TV)
and controls playback there: play/pause, seeking, stop, subtitle track and
subtitle delay. Requests run in worker threads; results come back through
GLib.idle_add.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core.airplay import (  # noqa: E402
    AirPlayClient,
    AirPlayError,
    AirPlayStatus,
    check_web_url,
    parse_receiver,
)

from ..core.local_media import MediaSession

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 1000
SEEK_BACK_S = 10
SEEK_FORWARD_S = 30

STATE_LABELS = {
    "IDLE": "Nothing playing",
    "LOADING": "Loading…",
    "PLAYING": "Playing",
    "PAUSED": "Paused",
    "ENDED": "Ended",
}


def _format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class AirPlayDialog(Adw.Dialog):
    """Plays video links on the TV over AirPlay and controls the playback."""

    def __init__(self, settings: Gio.Settings, default_host: Callable[[], str | None]) -> None:
        super().__init__()
        self._local_session: MediaSession | None = None
        self._local_client: AirPlayClient | None = None
        self._local_generation = 0
        self._play_lock = threading.Lock()
        self._settings = settings
        self._default_host = default_host
        self._client_obj: AirPlayClient | None = None
        self._poll_id: int | None = None
        self._poll_busy = False
        self._status = AirPlayStatus()
        self._updating = (
            False  # set while status updates widgets, so their signals don't send requests
        )
        self._seek_hold_until = 0.0  # ignore polled positions right after the user seeks
        self._seek_timer_id: int | None = None
        self._delay_timer_id: int | None = None
        self._caption_names: list[str] = []
        self._sent_video: str | None = None  # shown instead of the receiver's local proxy URL

        self.set_title("AirPlay")
        self.set_content_width(460)
        self.set_content_height(720)
        self._build_ui()
        self.connect(
            "closed", lambda *_: (self._stop_polling(), self._close_local(stop_receiver=True))
        )

    # ---- public ----

    def open(self, parent: Gtk.Widget) -> None:
        """Fill in the receiver address, start polling and show the dialog."""
        if not self._receiver_row.get_text().strip():
            self._receiver_row.set_text(
                self._settings.get_string("airplay-receiver") or self._default_host() or ""
            )
        self.present(parent)
        self._start_polling()

    # ---- UI ----

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        self._toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self._toast_overlay)
        self.set_child(toolbar_view)

        page = Adw.PreferencesPage()
        self._toast_overlay.set_child(page)

        receiver_group = Adw.PreferencesGroup(
            title="Receiver",
            description="TinyTV Receiver on the TV, or another AirPlay video receiver (port 7000 unless given).",
        )
        self._receiver_row = Adw.EntryRow(title="Receiver address")
        self._receiver_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._receiver_row.connect("changed", self._on_receiver_changed)
        receiver_group.add(self._receiver_row)
        page.add(receiver_group)

        media_group = Adw.PreferencesGroup(
            title="Media",
            description="http(s) links: HLS (.m3u8), MP4 and other streams the TV can play. Subtitles: SRT or WebVTT.",
        )
        self._video_row = self._link_row("Video link", "airplay-video-url")
        self._subtitle_row = self._link_row("Subtitle link (optional)", "airplay-subtitle-url")
        media_group.add(self._video_row)
        media_group.add(self._subtitle_row)
        local_button = Gtk.Button(label="Play a local file…")
        local_button.connect("clicked", self._choose_local_file)
        media_group.add(local_button)
        self._local_state = Adw.ActionRow(
            title="Local video", subtitle="Original or converted automatically for this TV"
        )
        media_group.add(self._local_state)

        self._play_button = Gtk.Button(label="Play on TV")
        self._play_button.add_css_class("suggested-action")
        self._play_button.add_css_class("pill")
        self._play_button.set_halign(Gtk.Align.CENTER)
        self._play_button.set_margin_top(18)
        self._play_button.connect("clicked", self._on_play_clicked)
        media_group.add(self._play_button)
        page.add(media_group)

        tv_group = Adw.PreferencesGroup(title="On the TV")
        self._status_row = Adw.ActionRow(title="Not checked yet")
        self._status_row.set_subtitle_lines(2)
        tv_group.add(self._status_row)

        # Position slider with elapsed/total time
        self._position_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._position_box.set_margin_top(12)
        self._elapsed_label = Gtk.Label(label="0:00")
        self._elapsed_label.add_css_class("numeric")
        self._duration_label = Gtk.Label(label="0:00")
        self._duration_label.add_css_class("numeric")
        self._position_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self._position_scale.set_draw_value(False)
        self._position_scale.set_hexpand(True)
        self._position_scale.connect("change-value", self._on_scale_change_value)
        self._position_box.append(self._elapsed_label)
        self._position_box.append(self._position_scale)
        self._position_box.append(self._duration_label)
        tv_group.add(self._position_box)

        self._controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._controls_box.set_halign(Gtk.Align.CENTER)
        self._controls_box.set_margin_top(6)
        self._controls_box.set_margin_bottom(12)
        self._controls_box.append(
            self._control_button(
                "media-seek-backward-symbolic",
                f"Back {SEEK_BACK_S} s",
                lambda: self._seek_relative(-SEEK_BACK_S),
            )
        )
        self._play_pause_button = self._control_button(
            "media-playback-pause-symbolic", "Pause", self._on_play_pause_clicked
        )
        self._play_pause_button.add_css_class("suggested-action")
        self._controls_box.append(self._play_pause_button)
        self._controls_box.append(
            self._control_button(
                "media-seek-forward-symbolic",
                f"Forward {SEEK_FORWARD_S} s",
                lambda: self._seek_relative(SEEK_FORWARD_S),
            )
        )
        self._controls_box.append(
            self._control_button("media-playback-stop-symbolic", "Stop", self._on_stop_clicked)
        )
        tv_group.add(self._controls_box)
        page.add(tv_group)

        # Subtitle controls (TinyTV's extensions)
        self._subtitles_group = Adw.PreferencesGroup(title="Subtitles")
        self._captions_row = Adw.ComboRow(title="Track", model=Gtk.StringList.new(["Off"]))
        self._captions_row.connect("notify::selected", self._on_caption_selected)
        self._subtitles_group.add(self._captions_row)

        self._delay_row = Adw.SpinRow.new_with_range(-60, 60, 0.1)
        self._delay_row.set_title("Delay")
        self._delay_row.set_subtitle("Seconds; positive shows them later")
        self._delay_row.set_digits(1)
        self._delay_row.connect("notify::value", self._on_delay_changed)
        self._subtitles_group.add(self._delay_row)

        self._add_subtitle_row = Adw.ActionRow(
            title="Add subtitle", subtitle="Load the subtitle link above into the current video"
        )
        add_btn = Gtk.Button(label="Add")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", self._on_add_subtitle_clicked)
        self._add_subtitle_row.add_suffix(add_btn)
        self._subtitles_group.add(self._add_subtitle_row)
        page.add(self._subtitles_group)

        self._apply_status(None, None)

    def _link_row(self, title: str, key: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_input_purpose(Gtk.InputPurpose.URL)
        row.set_text(self._settings.get_string(key))
        paste = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Paste")
        paste.set_valign(Gtk.Align.CENTER)
        paste.add_css_class("flat")
        paste.connect("clicked", lambda *_: self._paste_into(row))
        row.add_suffix(paste)
        clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear")
        clear.set_valign(Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: row.set_text(""))
        row.add_suffix(clear)
        return row

    def _control_button(self, icon: str, tooltip: str, callback: Callable[[], None]) -> Gtk.Button:
        btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
        btn.add_css_class("circular")
        btn.set_size_request(48, 48)
        btn.connect("clicked", lambda *_: callback())
        return btn

    def _toast(self, text: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=text))

    def _paste_into(self, row: Adw.EntryRow) -> None:
        clipboard = Gdk.Display.get_default().get_clipboard()

        def done(cb: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            try:
                text = cb.read_text_finish(result)
            except GLib.Error as e:
                logger.warning(f"Clipboard read failed: {e}")
                return
            if text:
                row.set_text(text.strip())

        clipboard.read_text_async(None, done)

    # ---- receiver and requests ----

    def _on_receiver_changed(self, *_args) -> None:
        self._client_obj = None

    def _client(self) -> AirPlayClient | None:
        if self._client_obj is None:
            try:
                host, port = parse_receiver(self._receiver_row.get_text())
            except ValueError:
                return None
            self._client_obj = AirPlayClient(host, port)
        return self._client_obj

    def _run(
        self,
        call: Callable[[AirPlayClient], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Run call(client) in a worker thread; report back on the main thread."""
        client = self._client()
        if client is None:
            self._toast("Enter the receiver's address")
            return

        def report(callback: Callable[[Any], None], value: Any) -> bool:
            callback(value)
            return False  # run once

        def worker() -> None:
            try:
                result = call(client)
            except (
                AirPlayError,
                ValueError,
                OSError,
                subprocess.SubprocessError,
                StopIteration,
                KeyError,
                TypeError,
            ) as e:
                message = str(e)
                logger.warning(f"AirPlay request failed: {message}")
                GLib.idle_add(report, on_error or self._toast, message)
                return
            if on_done:
                GLib.idle_add(report, on_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _close_local(self, stop_receiver: bool = False) -> MediaSession | None:
        """Cancel synchronously; release server/process off the GTK thread."""
        self._local_generation += 1
        session, self._local_session = self._local_session, None
        generation = self._local_generation
        client, self._local_client = self._local_client, None
        if session:
            self._local_state.set_subtitle("Local playback stopped")
            session.cancelled.set()

            def cleanup() -> None:
                with self._play_lock:
                    session.close()
                    if stop_receiver and generation == self._local_generation and client:
                        try:
                            client.stop()
                        except AirPlayError:
                            pass

            threading.Thread(target=cleanup, daemon=True).start()
        return session

    def _choose_local_file(self, *_args: Any) -> None:
        """Use the file portal, retaining the selected local path for serving."""
        chooser = Gtk.FileDialog(title="Play a local video")

        def selected(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dialog.open_finish(result)
                path = file.get_path()
                if path:
                    self._play_local(Path(path))
            except GLib.Error:
                pass

        chooser.open(self.get_root(), None, selected)

    def _play_local(self, path: Path) -> None:
        """Probe, convert when needed, and send on a worker thread."""
        if self._client() is None:
            self._toast("Enter the receiver's address")
            return
        previous = self._close_local()
        generation = self._local_generation
        session = MediaSession()
        self._local_session = session
        self._local_client = self._client()
        self._sent_video = path.name
        self._local_state.set_subtitle("Preparing local video…")

        def progress(message: str) -> None:
            def update() -> bool:
                if generation == self._local_generation:
                    self._local_state.set_subtitle(message)
                return False

            GLib.idle_add(update)

        def play(client: AirPlayClient) -> None:
            with self._play_lock:
                if previous:
                    previous.close()
                if session.cancelled.is_set():
                    return
                client.stop()
                url = session.prepare(path, client.host, client.capabilities(), progress)
                if session.cancelled.is_set():
                    return
                client.play(url)
                progress("Playing · " + path.name)

        def failed(message: str) -> None:
            if generation == self._local_generation:
                self._close_local()
                self._local_state.set_subtitle(message)
                self._toast(message)

        self._run(play, lambda _r: self._poll_now(), failed)

    # ---- playing a link ----

    def _on_play_clicked(self, *_args) -> None:
        try:
            video = check_web_url(self._video_row.get_text())
            subtitle_text = self._subtitle_row.get_text().strip()
            subtitle = check_web_url(subtitle_text) if subtitle_text else None
        except ValueError as e:
            self._toast(str(e))
            return
        if self._client() is None:
            self._toast("Enter the receiver's address")
            return

        receiver = self._receiver_row.get_text().strip()
        self._settings.set_string(
            "airplay-receiver", "" if receiver == (self._default_host() or "") else receiver
        )
        self._settings.set_string("airplay-video-url", video)
        self._settings.set_string("airplay-subtitle-url", subtitle or "")

        self._play_button.set_sensitive(False)
        self._play_button.set_label("Sending…")

        def finished(_result: Any = None) -> None:
            self._play_button.set_sensitive(True)
            self._play_button.set_label("Play on TV")

        def done(_result: Any) -> None:
            finished()
            if generation != self._local_generation:
                return
            self._sent_video = video
            self._toast("Sent to the TV")
            self._poll_now()

        def failed(message: str) -> None:
            finished()
            self._toast(message)

        previous = self._close_local()
        generation = self._local_generation

        def play(client: AirPlayClient) -> None:
            with self._play_lock:
                if previous:
                    previous.close()
                if generation == self._local_generation:
                    client.play(video, subtitle)

        self._run(play, done, failed)

    def _on_add_subtitle_clicked(self, *_args) -> None:
        text = self._subtitle_row.get_text().strip()
        if not text:
            self._toast("Enter a subtitle link first")
            return
        self._run(
            lambda c: c.load_captions(text),
            lambda _r: (self._toast("Subtitle added"), self._poll_now()),
        )

    # ---- playback controls ----

    def _on_play_pause_clicked(self) -> None:
        playing = self._status.state != "PLAYING"
        self._run(lambda c: c.set_playing(playing), lambda _r: self._poll_now())

    def _on_stop_clicked(self) -> None:
        self._close_local()
        generation = self._local_generation

        def stop(client: AirPlayClient) -> None:
            with self._play_lock:
                if generation == self._local_generation:
                    client.stop()

        self._run(stop, lambda _r: self._poll_now())

    def _seek_relative(self, delta: float) -> None:
        target = self._status.position + delta
        if self._status.duration > 0:
            target = min(target, self._status.duration - 1)
        self._seek_to(max(0.0, target))

    def _seek_to(self, seconds: float) -> None:
        local = self._local_session
        if local and local.process and local.process.poll() is None:
            self._toast("Seeking is available after conversion finishes")
            return
        self._seek_hold_until = time.monotonic() + 2.0
        self._status.position = seconds
        self._updating = True
        self._position_scale.set_value(seconds)
        self._updating = False
        self._elapsed_label.set_label(_format_time(seconds))
        self._run(lambda c: c.seek(seconds))

    def _on_scale_change_value(
        self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float
    ) -> bool:
        # Dragging fires this continuously; seek once the slider settles
        self._seek_hold_until = time.monotonic() + 2.0
        self._elapsed_label.set_label(_format_time(value))
        if self._seek_timer_id is not None:
            GLib.source_remove(self._seek_timer_id)

        def fire() -> bool:
            self._seek_timer_id = None
            self._seek_to(value)
            return False

        self._seek_timer_id = GLib.timeout_add(300, fire)
        return False

    def _on_caption_selected(self, *_args) -> None:
        if self._updating:
            return
        index = self._captions_row.get_selected() - 1  # row 0 is "Off"
        self._run(lambda c: c.select_captions(index))

    def _on_delay_changed(self, *_args) -> None:
        if self._updating:
            return
        if self._delay_timer_id is not None:
            GLib.source_remove(self._delay_timer_id)

        def fire() -> bool:
            self._delay_timer_id = None
            offset_ms = round(self._delay_row.get_value() * 1000)
            self._run(lambda c: c.set_captions_offset(offset_ms))
            return False

        self._delay_timer_id = GLib.timeout_add(300, fire)

    # ---- status polling ----

    def _start_polling(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll_tick)
        self._poll_now()

    def _stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll_tick(self) -> bool:
        self._poll_now()
        return True

    def _poll_now(self) -> None:
        client = self._client()
        if client is None:
            self._apply_status(None, "Enter the receiver's address")
            return
        if self._poll_busy:
            return
        self._poll_busy = True

        def worker() -> None:
            try:
                status, error = client.status(), None
            except AirPlayError as e:
                status, error = None, str(e)
            GLib.idle_add(self._on_polled, client, status, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_polled(
        self, client: AirPlayClient, status: AirPlayStatus | None, error: str | None
    ) -> None:
        self._poll_busy = False
        if client is not self._client_obj:
            return  # the address changed meanwhile
        self._apply_status(status, error)

    def _apply_status(self, status: AirPlayStatus | None, error: str | None) -> None:
        active = bool(status and status.active)
        extended = bool(status and status.extended)
        for widget in (self._position_box, self._controls_box):
            widget.set_visible(active)
        self._subtitles_group.set_visible(active and extended)

        if status is None:
            self._status_row.set_title("Receiver not reachable" if error else "Not checked yet")
            self._status_row.set_subtitle(GLib.markup_escape_text(error or ""))
            return

        self._status = status
        title = STATE_LABELS.get(status.state, status.state.title())
        if active and status.duration > 0:
            title += f" · {_format_time(status.position)} / {_format_time(status.duration)}"
        self._status_row.set_title(title)
        url = status.url
        if url and url.startswith(("http://127.0.0.1", "http://localhost")):
            url = self._sent_video  # TinyTV plays remuxed HLS through its own proxy
        detail = status.error or url or ""
        self._status_row.set_subtitle(GLib.markup_escape_text(detail))
        if not active:
            return

        self._updating = True
        try:
            self._play_pause_button.set_icon_name(
                "media-playback-pause-symbolic"
                if status.state == "PLAYING"
                else "media-playback-start-symbolic"
            )
            self._play_pause_button.set_tooltip_text(
                "Pause" if status.state == "PLAYING" else "Play"
            )

            local = self._local_session
            converting = bool(local and local.process and local.process.poll() is None)
            seekable = status.duration > 0 and not converting
            self._position_scale.set_sensitive(seekable)
            self._duration_label.set_label(
                _format_time(status.duration) if seekable else ("Converting" if converting else "Live")
            )
            if time.monotonic() >= self._seek_hold_until:
                self._position_scale.set_range(0, max(1.0, status.duration))
                self._position_scale.set_value(status.position)
                self._elapsed_label.set_label(_format_time(status.position))

            if not extended:
                return
            if status.captions != self._caption_names:
                self._caption_names = list(status.captions)
                names = [f"{i + 1}. {name}" for i, name in enumerate(status.captions)]
                self._captions_row.set_model(Gtk.StringList.new(["Off", *names]))
            self._captions_row.set_selected(status.caption + 1)
            if self._delay_timer_id is None:
                self._delay_row.set_value(status.caption_offset_ms / 1000)
        finally:
            self._updating = False
