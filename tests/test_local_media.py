"""Offline tests for sender policy, encoder argv, HTTP and cancellation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gnome_adb_tv_remote.core.airplay import AirPlayClient, AirPlayError
from gnome_adb_tv_remote.core.local_media import (
    MediaInfo,
    MediaSession,
    byte_range,
    encoder_candidates,
    ffmpeg_args,
    media_handler,
    needs_transcode,
    probe_file,
)


class PolicyTests(unittest.TestCase):
    def test_limits(self) -> None:
        for codec, width, height, fps, rotation, expected in [
            ("avc", 1920, 1088, 60, 0, False),
            ("avc", 1088, 1920, 60, 90, False),
            ("avc", 1922, 1080, 30, 0, True),
            ("avc", 1920, 1090, 30, 0, True),
            ("avc", 1920, 1080, 61, 0, True),
            ("avc", 2880, 2160, 60, 0, True),
            ("hevc", 3840, 2160, 60, 0, False),
            ("hevc", 2160, 3840, 60, 0, False),
            ("hevc", 3840, 2160, 60, 90, True),
            ("hevc", 3840, 2160, 60, 180, True),
            ("hevc", 3840, 2160, 60, 270, True),
            ("hevc", 4096, 2160, 30, 0, True),
            ("hevc", 3840, 2160, 61, 0, True),
            ("vp9", 640, 480, 24, 0, True),
        ]:
            with self.subTest(codec=codec, size=(width, height), fps=fps, rotation=rotation):
                self.assertEqual(
                    needs_transcode(MediaInfo(codec, width, height, fps, rotation)), expected
                )
        self.assertFalse(
            needs_transcode(MediaInfo("hevc", 3840, 2160, 30, 90), {"hevc": {"rotate": True}})
        )
        self.assertTrue(
            needs_transcode(MediaInfo("avc", 1920, 1080, 30), {"avc": {"maxWidth": 1280}})
        )

    def test_caps_404_only(self) -> None:
        client = AirPlayClient("unused")
        with patch.object(
            client,
            "_request",
            side_effect=AirPlayError("The AirPlay receiver doesn't support /tinytv/caps"),
        ):
            self.assertEqual(client.capabilities()["avc"]["maxWidth"], 1920)
        with patch.object(client, "_request", side_effect=AirPlayError("HTTP 500")):
            with self.assertRaises(AirPlayError):
                client.capabilities()
        with patch.object(client, "_request", return_value=b'{"hevc":{"rotate":true}}'):
            self.assertTrue(client.capabilities()["hevc"]["rotate"])

    def test_probe_rotation(self) -> None:
        stream = {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "30000/1001",
            "side_data_list": [{"rotation": -90}],
        }
        with patch("subprocess.run", return_value=Mock(stdout=json.dumps({"streams": [stream]}))):
            info = probe_file(Path("/input.mp4"))
        self.assertEqual(info.rotation, 90)
        self.assertAlmostEqual(info.fps, 29.97002997)


class ArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        binary = patch(
            "gnome_adb_tv_remote.core.local_media.media_binary", side_effect=lambda name: name
        )
        binary.start()
        self.addCleanup(binary.stop)

    def test_rotation_sizes_and_encoders(self) -> None:
        for encoder in ("h264_qsv", "h264_vaapi", "libx264", "libopenh264"):
            for size in ((3840, 2160), (2160, 3840)):
                for rotation in (0, 90, 180, 270):
                    with self.subTest(encoder=encoder, size=size, rotation=rotation):
                        args = ffmpeg_args(
                            Path("/input with spaces.mp4"),
                            Path("/out"),
                            MediaInfo("hevc", *size, 60, rotation),
                            encoder,
                        )
                        graph = args[args.index("-vf") + 1]
                        portrait = (size[0] < size[1]) != (rotation in (90, 270))
                        expected = "608" if portrait else "1920"
                        self.assertIn(expected, graph)
                        self.assertIn("1080", graph)
                        self.assertIn("-noautorotate", args)
                        self.assertIn("setsar=1", graph)
                        self.assertIn("fps=30", graph)
                        self.assertEqual(args[args.index("-hls_playlist_type") + 1], "event")
                        self.assertEqual(args[-1], "/out/index.m3u8")
                        self.assertEqual(args[args.index("-hls_segment_type") + 1], "mpegts")
                        self.assertIn("rotate=0", args)
                        if rotation == 90:
                            self.assertIn("clock", graph)
                            self.assertNotIn("cclock", graph)
                        elif rotation == 270:
                            self.assertIn("cclock", graph)
                        elif rotation == 180:
                            self.assertTrue(
                                "hflip,vflip" in graph
                                or "reversal" in graph
                                or graph.count("transpose=clock") == 2
                            )
                        else:
                            self.assertNotIn("transpose", graph)

    def test_qsv_forces_idr_at_hls_boundaries(self) -> None:
        for encoder in ("h264_qsv", "h264_vaapi", "libx264", "libopenh264"):
            with self.subTest(encoder=encoder):
                args = ffmpeg_args(
                    Path("/clip.mp4"), Path("/out"),
                    MediaInfo("hevc", 3840, 2160, 30, 90), encoder,
                )
                if encoder == "h264_qsv":
                    self.assertEqual(args[args.index("-idr_interval") + 1], "0")
                    self.assertEqual(args[args.index("-forced_idr") + 1], "1")
                    self.assertEqual(args[args.index("-g") + 1], "60")
                    self.assertEqual(
                        args[args.index("-force_key_frames") + 1], "expr:gte(t,n_forced*2)"
                    )
                else:
                    self.assertNotIn("-idr_interval", args)
                    self.assertNotIn("-forced_idr", args)

    def test_keep_low_fps(self) -> None:
        args = ffmpeg_args(Path("/x"), Path("/out"), MediaInfo("avc", 640, 480, 24), "libx264")
        self.assertIn("fps=24", args[args.index("-vf") + 1])
        self.assertIn("scale=640:480", args[args.index("-vf") + 1])

    def test_runtime_encoders(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(encoder_candidates()[0], "h264_vaapi")
        with patch.object(Path, "exists", return_value=False):
            self.assertEqual(encoder_candidates()[0], "h264_qsv")


class HttpTests(unittest.TestCase):
    def request(
        self,
        directory: Path,
        files: dict | None,
        path: str,
        range_value: str | None = None,
        head: bool = False,
    ) -> Mock:
        handler_type = media_handler(directory, files)
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.headers = {"Range": range_value} if range_value is not None else {}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.send_error = Mock()
        handler.serve(not head)
        return handler

    def test_range_and_head_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            file = directory / "clip.mp4"
            file.write_bytes(b"0123456789")
            for value, payload, code in [
                (None, b"0123456789", 200),
                ("bytes=2-5", b"2345", 206),
                ("bytes=7-", b"789", 206),
                ("bytes=-3", b"789", 206),
                ("bytes=8-99", b"89", 206),
            ]:
                with self.subTest(value=value):
                    handler = self.request(directory, {"/media.mp4": file}, "/media.mp4", value)
                    handler.send_response.assert_called_once_with(code)
                    self.assertEqual(handler.wfile.getvalue(), payload)
                    handler.send_header.assert_any_call("Content-Length", str(len(payload)))
            handler = self.request(
                directory, {"/media.mp4": file}, "/media.mp4", "bytes=1-3", True
            )
            self.assertEqual(handler.wfile.getvalue(), b"")
            handler.send_header.assert_any_call("Content-Range", "bytes 1-3/10")
            for value in ("bytes=99-", "bytes=-0", "bytes=5-2", "bytes=1-2,4-5", "nonsense"):
                handler = self.request(directory, {"/media.mp4": file}, "/media.mp4", value)
                handler.send_response.assert_called_once_with(416)
                handler.send_header.assert_any_call("Content-Range", "bytes */10")

    def test_hls_mimes_and_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name, mime in (
                ("index.m3u8", "application/vnd.apple.mpegurl"),
                ("seg000001.ts", "video/mp2t"),
            ):
                (directory / name).write_bytes(b"data")
                handler = self.request(directory, None, "/" + name)
                handler.send_header.assert_any_call("Content-Type", mime)
                handler.send_header.assert_any_call("Cache-Control", "no-store")
            for name in ("encoder.log", "progress.log", "seg.ts.tmp"):
                (directory / name).write_bytes(b"secret")
            for path in (
                "/",
                "/../outside",
                "/%2e%2e/outside",
                "/encoder.log",
                "/progress.log",
                "/seg.ts.tmp",
            ):
                self.request(directory, None, path).send_error.assert_called_once_with(404)
            self.request(
                directory, {"/clip": directory / "index.m3u8"}, "/index.m3u8"
            ).send_error.assert_called_once_with(404)

    def test_empty_file(self) -> None:
        self.assertEqual(byte_range(None, 0), (0, -1))
        with self.assertRaises(ValueError):
            byte_range("bytes=0-", 0)


class SessionTests(unittest.TestCase):
    def test_hardware_failure_retries_software_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = MediaSession()
            progress = []
            attempted = []

            def spawn(args: list[str], **kwargs: object) -> Mock:
                encoder = args[args.index("-c:v") + 1]
                attempted.append(encoder)
                process = Mock()
                process.poll.return_value = 1 if len(attempted) == 1 else 0
                if len(attempted) == 2:
                    Path(args[-1]).write_text("#EXTM3U\n#EXTINF:1,\nseg000.ts\n#EXT-X-ENDLIST\n")
                return process

            with (
                patch.dict("os.environ", {"XDG_CACHE_HOME": temp}),
                patch(
                    "gnome_adb_tv_remote.core.local_media.probe_file",
                    return_value=MediaInfo("hevc", 3840, 2160, 30, 90),
                ),
                patch(
                    "gnome_adb_tv_remote.core.local_media.media_binary",
                    side_effect=lambda name: name,
                ),
                patch("gnome_adb_tv_remote.core.local_media.subprocess.Popen", side_effect=spawn),
                patch("gnome_adb_tv_remote.core.local_media.socket.socket") as route,
                patch("gnome_adb_tv_remote.core.local_media.ThreadingHTTPServer") as server,
                patch("gnome_adb_tv_remote.core.local_media.threading.Thread"),
            ):
                route.return_value.__enter__.return_value.getsockname.return_value = (
                    "192.0.2.1",
                    1234,
                )
                server.return_value.server_port = 8888
                url = session.prepare(Path("/video.mp4"), "192.0.2.2", {}, progress.append)
                self.assertEqual(url, "http://192.0.2.1:8888/index.m3u8")
                self.assertEqual(attempted, ["h264_qsv", "libx264"])
                self.assertTrue(any("libx264" in message for message in progress))
                directory = session.directory
                session.close()
                self.assertFalse(directory.exists())

    def test_cleanup_and_cancel_before_start(self) -> None:
        session = MediaSession()
        process = Mock()
        process.poll.return_value = None
        session.process = process
        session.server = Mock()
        server = session.server
        directory = Path(tempfile.mkdtemp())
        (directory / "seg.ts").write_bytes(b"test")
        session.directory = directory
        session.close()
        process.terminate.assert_called_once()
        server.shutdown.assert_called_once()
        self.assertFalse(directory.exists())
        session.close()
        with patch(
            "gnome_adb_tv_remote.core.local_media.probe_file",
            return_value=MediaInfo("avc", 640, 480, 30),
        ):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                session.prepare(Path("/x"), "unused", {}, lambda _message: None)


if __name__ == "__main__":
    unittest.main()
