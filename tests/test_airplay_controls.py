"""Playback lifecycle and main-window cast controls without a TV connection."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from gnome_adb_tv_remote.core.airplay import AirPlayStatus
from gnome_adb_tv_remote.ui.airplay_dialog import AirPlayDialog, PlaybackSummary
from gnome_adb_tv_remote.ui.main_window import MainWindow


class PlaybackLifecycleTests(unittest.TestCase):
    def test_closing_dialog_keeps_local_stream_and_polling(self) -> None:
        session = Mock()
        dialog = SimpleNamespace(
            _dialog_open=True,
            _status=AirPlayStatus(),
            _preparing=True,
            _local_session=session,
            _stop_polling=Mock(),
        )
        AirPlayDialog._on_closed(dialog)
        self.assertFalse(dialog._dialog_open)
        self.assertIs(dialog._local_session, session)
        session.close.assert_not_called()
        dialog._stop_polling.assert_not_called()

    def test_closing_idle_dialog_stops_polling(self) -> None:
        dialog = SimpleNamespace(
            _dialog_open=True,
            _status=AirPlayStatus(),
            _preparing=False,
            _local_session=None,
            _stop_polling=Mock(),
        )
        AirPlayDialog._on_closed(dialog)
        dialog._stop_polling.assert_called_once()

    def test_late_poll_cannot_restore_stopped_playback(self) -> None:
        client = object()
        dialog = SimpleNamespace(
            _poll_busy=True,
            _shutdown=False,
            _stopping=False,
            _poll_generation=2,
            _client_obj=client,
            _apply_status=Mock(),
        )
        AirPlayDialog._on_polled(dialog, client, 1, AirPlayStatus(state="PLAYING"), None)
        self.assertFalse(dialog._poll_busy)
        dialog._apply_status.assert_not_called()

    def test_success_does_not_close_a_reopened_dialog(self) -> None:
        dialog = SimpleNamespace(_dialog_open=True, _presentation_generation=2, close=Mock())
        AirPlayDialog._return_to_remote(dialog, 1)
        dialog.close.assert_not_called()
        AirPlayDialog._return_to_remote(dialog, 2)
        dialog.close.assert_called_once()

    def test_finished_local_stream_is_released_after_idle_polls(self) -> None:
        client = object()
        dialog = SimpleNamespace(
            _poll_busy=True,
            _shutdown=False,
            _stopping=False,
            _poll_generation=1,
            _client_obj=client,
            _local_started=True,
            _preparing=False,
            _idle_polls=0,
            _dialog_open=False,
            _local_session=object(),
            _status=AirPlayStatus(),
            _poll_id=1,
            _apply_status=Mock(),
            _close_local=Mock(),
            _notify_status=Mock(),
            _start_polling=Mock(),
            _stop_polling=Mock(),
        )

        def close_local() -> None:
            dialog._local_session = None
            dialog._local_started = False

        dialog._close_local.side_effect = close_local
        for _ in range(3):
            AirPlayDialog._on_polled(dialog, client, 1, AirPlayStatus(state="ENDED"), None)
        dialog._close_local.assert_called_once()
        dialog._stop_polling.assert_called_once()


class MainWindowPlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = SimpleNamespace(
            _airplay_revealer=Mock(),
            _airplay_subtitle=Mock(),
            _airplay_details=Mock(),
            _airplay_play=Mock(),
            _airplay_back=Mock(),
            _airplay_forward=Mock(),
            _airplay_stop=Mock(),
        )

    def test_conversion_keeps_playback_visible_and_disables_seeking(self) -> None:
        summary = PlaybackSummary(
            status=AirPlayStatus(state="PLAYING", position=10, duration=100),
            converting=True,
            detail="Converting · 40%",
            media="clip.mp4",
        )
        MainWindow._update_airplay_bar(self.window, summary)
        self.window._airplay_revealer.set_reveal_child.assert_called_once_with(True)
        self.window._airplay_subtitle.set_label.assert_called_once_with("Playing · Converting · 40%")
        self.window._airplay_play.set_sensitive.assert_called_once_with(True)
        self.window._airplay_back.set_sensitive.assert_called_once_with(False)
        self.window._airplay_forward.set_sensitive.assert_called_once_with(False)

    def test_preparing_is_visible_and_idle_is_hidden(self) -> None:
        MainWindow._update_airplay_bar(
            self.window, PlaybackSummary(status=AirPlayStatus(), preparing=True)
        )
        self.window._airplay_revealer.set_reveal_child.assert_called_with(True)
        self.window._airplay_play.set_sensitive.assert_called_with(False)
        MainWindow._update_airplay_bar(self.window, PlaybackSummary(status=AirPlayStatus()))
        self.window._airplay_revealer.set_reveal_child.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()
