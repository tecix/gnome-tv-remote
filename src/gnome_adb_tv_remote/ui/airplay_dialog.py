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
import re
import subprocess
from dataclasses import dataclass
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

from ..core.local_media import MediaInfo, MediaSession, needs_transcode, probe_file
from .local_video_group import LocalVideoGroup

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


@dataclass
class PlaybackSummary:
    """Playback state shared with the main window."""

    status: AirPlayStatus
    preparing: bool = False
    converting: bool = False
    detail: str = ""
    media: str | None = None
    error: str | None = None


class AirPlayDialog(Adw.Dialog):
    """Plays video links on the TV over AirPlay and controls the playback."""

    def __init__(
        self,
        settings: Gio.Settings,
        default_host: Callable[[], str | None],
        on_status: Callable[[PlaybackSummary], None] | None = None,
        on_message: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._selected_local: Path | None = None
        self._selection_generation = 0
        self._local_session: MediaSession | None = None
        self._local_client: AirPlayClient | None = None
        self._local_generation = 0
        self._play_lock = threading.Lock()
        self._settings = settings
        self._default_host = default_host
        self._on_status = on_status
        self._on_message = on_message
        self._dialog_open = False
        self._presentation_generation = 0
        self._shutdown = False
        self._preparing = False
        self._stopping = False
        self._preparation_detail = ""
        self._poll_error: str | None = None
        self._local_started = False
        self._idle_polls = 0
        self._client_obj: AirPlayClient | None = None
        self._poll_id: int | None = None
        self._poll_busy = False
        self._poll_generation = 0
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
        self.set_content_height(820)
        self._build_ui()
        self.connect("closed", self._on_closed)

    # ---- public ----

    def open(self, parent: Gtk.Widget) -> None:
        """Fill in the receiver address, start polling and show the dialog."""
        if not self._receiver_row.get_text().strip():
            self._receiver_row.set_text(
                self._settings.get_string("airplay-receiver") or self._default_host() or ""
            )
        self._dialog_open = True
        self._presentation_generation += 1
        self.present(parent)
        self._start_polling()

    def shutdown(self) -> None:
        """Release a local stream when the application window closes."""
        self._shutdown = True
        self._dialog_open = False
        self._stop_polling()
        self._close_local(stop_receiver=True)

    def _on_closed(self, *_args: Any) -> None:
        self._dialog_open = False
        if not (self._status.active or self._preparing or self._local_session):
            self._stop_polling()

    def _return_to_remote(self, presentation: int) -> None:
        if self._dialog_open and presentation == self._presentation_generation:
            self.close()

    def _notify_status(self) -> None:
        if self._on_status is None:
            return
        session = self._local_session
        converting = bool(session and session.process and session.process.poll() is None)
        self._on_status(
            PlaybackSummary(
                status=self._status,
                preparing=self._preparing,
                converting=converting,
                detail=self._preparation_detail,
                media=self._sent_video,
                error=self._poll_error,
            )
        )

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
        page.add(media_group)
        self._local_group = LocalVideoGroup(
            self._choose_local_file, self._clear_local_file, self._drop_local_file
        )
        page.add(self._local_group)
        self._play_button = Gtk.Button.new_with_mnemonic("_Play on TV")
        self._play_button.add_css_class("suggested-action")
        self._play_button.add_css_class("pill")
        self._play_button.set_halign(Gtk.Align.CENTER)
        self._play_button.connect("clicked", self._on_play_clicked)
        play_bar = Gtk.ActionBar()
        play_bar.set_center_widget(self._play_button)
        toolbar_view.add_bottom_bar(play_bar)

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
        if self._dialog_open or self._on_message is None:
            self._toast_overlay.add_toast(Adw.Toast(title=text))
        else:
            self._on_message(text)

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
        self._poll_generation += 1
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
            if not self._shutdown:
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
        self._preparing = False
        self._preparation_detail = ""
        self._local_started = False
        self._idle_polls = 0
        session, self._local_session = self._local_session, None
        generation = self._local_generation
        client, self._local_client = self._local_client, None
        if session:
            self._local_group.set_state("direct", message="Choose Play on TV to start again")
            self._local_group.status_row.set_title("Local playback stopped")
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
                    self._select_local_file(Path(path))
            except GLib.Error:
                pass

        chooser.open(self.get_root(), None, selected)

    @staticmethod
    def _local_metadata(info: MediaInfo) -> str:
        codec = {"avc": "H.264", "hevc": "HEVC"}.get(info.codec, info.codec.upper())
        parts = [codec, f"{info.width}×{info.height}"]
        if info.rotation in (90, 270):
            parts.append("portrait" if info.width > info.height else "landscape")
        if info.duration > 0:
            parts.append(_format_time(info.duration))
        if needs_transcode(info):
            parts.append("converts for TV")
        return " · ".join(parts)

    def _clear_local_file(self, *_args: Any) -> None:
        self._close_local(stop_receiver=True)
        self._selection_generation += 1
        self._selected_local = None
        self._local_group.set_state("idle", filename="", metadata="Or drop a video file here")

    def _drop_local_file(self, file: Gio.File) -> bool:
        path = file.get_path()
        if not path:
            return False
        self._select_local_file(Path(path))
        return True

    def _local_error(self, message: str, retry: Callable[[], None]) -> None:
        self._local_group.set_state("error", message=message)
        if not self._dialog_open and self._on_message:
            self._on_message(message)
            return
        toast = Adw.Toast(title=message, button_label="Retry")
        toast.connect("button-clicked", lambda *_: retry())
        self._toast_overlay.add_toast(toast)

    def _select_local_file(self, path: Path) -> None:
        self._close_local(stop_receiver=True)
        self._selection_generation += 1
        generation = self._selection_generation
        self._selected_local = path
        self._local_group.set_state(
            "probing", filename=path.name, metadata="Reading video details…"
        )

        def report(info: MediaInfo | None, error: str | None) -> bool:
            if generation != self._selection_generation:
                return False
            if error:
                self._local_error(error, lambda: self._select_local_file(path))
            elif info:
                converts = needs_transcode(info)
                self._local_group.set_state(
                    "direct",
                    metadata=self._local_metadata(info),
                    message="Will convert for this TV" if converts else "Ready to play unchanged",
                )
                if converts:
                    self._local_group.status_row.set_title("Converts for TV")
            return False

        def worker() -> None:
            try:
                info = probe_file(path)
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                subprocess.SubprocessError,
            ) as error:
                GLib.idle_add(report, None, str(error) or "No readable video track was found")
            else:
                GLib.idle_add(report, info, None)

        threading.Thread(target=worker, daemon=True).start()

    def _play_local(self, path: Path) -> None:
        """Probe, convert when needed, and send on a worker thread."""
        if self._client() is None:
            self._toast("Enter the receiver's address")
            return
        self._selection_generation += 1
        presentation = self._presentation_generation
        previous = self._close_local()
        generation = self._local_generation
        session = MediaSession()
        self._local_session = session
        self._local_client = self._client()
        self._sent_video = path.name
        self._poll_generation += 1
        self._stopping = False
        self._status = AirPlayStatus()
        self._poll_error = None
        self._preparing = True
        self._preparation_detail = "Preparing video…"
        self._notify_status()
        self._local_group.set_state(
            "probing", filename=path.name, metadata="Reading video details…"
        )

        completed = False
        encoder_name: str | None = None
        playing_on_tv = False
        conversion_fraction: float | None = None

        def progress(message: str) -> None:
            def update() -> bool:
                nonlocal completed, encoder_name, playing_on_tv, conversion_fraction
                if generation != self._local_generation:
                    return False
                info = session.info
                if info:
                    self._local_group.file_row.set_subtitle(self._local_metadata(info))
                if message.startswith("Conversion failed"):
                    self._local_error(message, lambda: self._play_local(path))
                elif message.startswith("Conversion complete"):
                    self._local_group.set_state(
                        "playing",
                        message=f"Playing on {self._local_client.label if self._local_client else 'TV'}",
                    )
                    if not completed:
                        self._toast("Video conversion complete")
                        completed = True
                elif (
                    message.startswith(("Converting", "Converted"))
                    or "converting in background" in message
                ):
                    encoder_match = re.search(r"Converting for TV \(([^)]+)\)", message)
                    if encoder_match:
                        encoder_name = encoder_match.group(1)
                        conversion_fraction = None
                    fraction = conversion_fraction
                    match = re.search(r"Converted (\d+):(\d+):(\d+)", message)
                    if match and info and info.duration > 0:
                        h, m, sec = map(int, match.groups())
                        fraction = min(1, (h * 3600 + m * 60 + sec) / info.duration)
                        conversion_fraction = fraction
                    detail = "Preparing video for playback"
                    if match and info and info.duration > 0:
                        elapsed = _format_time(h * 3600 + m * 60 + sec)
                        detail = f"Converted {elapsed} of {_format_time(info.duration)}"
                    self._local_group.set_state(
                        "converting",
                        message=detail,
                        fraction=fraction,
                        encoder=encoder_name,
                        receiver=(
                            self._local_client.label
                            if playing_on_tv and self._local_client
                            else None
                        ),
                    )
                    self._preparation_detail = (
                        f"Converting · {fraction:.0%}" if fraction is not None else "Converting for TV…"
                    )
                elif message.startswith("Playing"):
                    playing_on_tv = True
                    if session.process and session.process.poll() is None:
                        self._local_group.set_state(
                            "converting",
                            fraction=conversion_fraction,
                            encoder=encoder_name,
                            receiver=self._local_client.label if self._local_client else "TV",
                        )
                    else:
                        self._local_group.set_state(
                            "playing",
                            message=f"Playing on {self._local_client.label if self._local_client else 'TV'}",
                        )
                elif "original file" in message:
                    self._local_group.set_state("direct", message="Sending original video")
                self._notify_status()
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
                self._local_error(message, lambda: self._play_local(path))
                self._notify_status()

        def done(_result: Any) -> None:
            if generation != self._local_generation:
                return
            self._preparing = False
            self._local_started = True
            if not self._status.active:
                self._status = AirPlayStatus(state="LOADING")
            self._notify_status()
            self._return_to_remote(presentation)
            self._poll_now()

        self._run(play, done, failed)

    # ---- playing a link ----

    def _on_play_clicked(self, *_args) -> None:
        if self._selected_local is not None:
            self._play_local(self._selected_local)
            return
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
        presentation = self._presentation_generation

        def finished(_result: Any = None) -> None:
            self._play_button.set_sensitive(True)
            self._play_button.set_label("_Play on TV")

        def done(_result: Any) -> None:
            finished()
            if generation != self._local_generation:
                return
            self._preparing = False
            self._sent_video = video
            if not self._status.active:
                self._status = AirPlayStatus(state="LOADING")
            self._notify_status()
            self._return_to_remote(presentation)
            self._poll_now()

        def failed(message: str) -> None:
            finished()
            if generation != self._local_generation:
                return
            self._preparing = False
            self._toast(message)
            self._notify_status()

        previous = self._close_local()
        generation = self._local_generation
        self._poll_generation += 1
        self._stopping = False
        self._status = AirPlayStatus()
        self._poll_error = None
        self._preparing = True
        self._preparation_detail = "Sending video…"
        self._sent_video = video
        self._notify_status()

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
        self._poll_generation += 1
        self._stopping = True
        self._status = AirPlayStatus()
        self._poll_error = None
        self._notify_status()
        if self._client() is None:
            self._stopping = False
            self._toast("Enter the receiver's address")
            return
        generation = self._local_generation

        def stop(client: AirPlayClient) -> None:
            with self._play_lock:
                if generation == self._local_generation:
                    client.stop()

        def finished(message: str | None = None) -> None:
            if generation != self._local_generation:
                return
            self._stopping = False
            if message:
                self._toast(message)
            self._poll_now()

        self._run(stop, lambda _r: finished(), finished)

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
        if self._shutdown or self._stopping:
            return
        client = self._client()
        if client is None:
            self._apply_status(None, "Enter the receiver's address")
            return
        if self._poll_busy:
            return
        self._poll_busy = True
        generation = self._poll_generation

        def worker() -> None:
            try:
                status, error = client.status(), None
            except AirPlayError as e:
                status, error = None, str(e)
            GLib.idle_add(self._on_polled, client, generation, status, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_polled(
        self, client: AirPlayClient, generation: int,
        status: AirPlayStatus | None, error: str | None,
    ) -> None:
        self._poll_busy = False
        if self._shutdown or self._stopping or generation != self._poll_generation:
            return
        if client is not self._client_obj:
            return  # the address changed meanwhile
        self._apply_status(status, error)
        if status is not None and self._local_started and not self._preparing:
            self._idle_polls = 0 if status.active else self._idle_polls + 1
            if self._idle_polls >= 3:
                self._close_local()
                self._notify_status()
        if not self._dialog_open:
            if self._status.active or self._preparing or self._local_session:
                if self._poll_id is None:
                    self._start_polling()
            else:
                self._stop_polling()

    def _apply_status(self, status: AirPlayStatus | None, error: str | None) -> None:
        self._poll_error = error
        active = bool(status and status.active)
        extended = bool(status and status.extended)
        for widget in (self._position_box, self._controls_box):
            widget.set_visible(active)
        self._subtitles_group.set_visible(active and extended)

        if status is None:
            self._status_row.set_title("Receiver not reachable" if error else "Not checked yet")
            self._status_row.set_subtitle(GLib.markup_escape_text(error or ""))
            self._notify_status()
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
        self._notify_status()
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
                _format_time(status.duration)
                if seekable
                else ("Converting" if converting else "Live")
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
