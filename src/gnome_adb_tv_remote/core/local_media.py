"""Local AirPlay file serving and cancellable sender-side HLS conversion."""

from __future__ import annotations

import atexit
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

DEFAULT_CAPS = {
    "avc": {"maxWidth": 1920, "maxHeight": 1088, "maxFps": 60},
    "hevc": {"maxWidth": 3840, "maxHeight": 2160, "maxFps": 60, "rotate": False},
}


@dataclass
class MediaInfo:
    """Coded video properties; rotation is clockwise for display."""

    codec: str
    width: int
    height: int
    fps: float
    rotation: int = 0


def needs_transcode(info: MediaInfo, caps: dict | None = None) -> bool:
    """Apply receiver limits to coded dimensions, independent of orientation."""
    if info.codec not in DEFAULT_CAPS:
        return True
    limits = dict(DEFAULT_CAPS[info.codec])
    limits.update((caps or {}).get(info.codec, {}))
    return (
        max(info.width, info.height) > limits["maxWidth"]
        or min(info.width, info.height) > limits["maxHeight"]
        or info.fps > limits["maxFps"]
        or (info.codec == "hevc" and info.rotation != 0 and not limits["rotate"])
    )


def media_binary(name: str) -> str:
    """Use bundled runtime tools or the host's working Jellyfin QSV build."""
    jellyfin = Path("/usr/lib/jellyfin-ffmpeg") / name
    if not Path("/.flatpak-info").exists() and jellyfin.is_file():
        return str(jellyfin)
    binary = shutil.which(name)
    if not binary:
        raise ValueError(f"{name} is required to play local files")
    return binary


def encoder_candidates() -> tuple[str, ...]:
    """QSV on the host; the Flatpak runtime supplies VAAPI and software H.264."""
    hardware = "h264_vaapi" if Path("/.flatpak-info").exists() else "h264_qsv"
    return hardware, "libx264", "libopenh264"


def probe_file(path: Path) -> MediaInfo:
    """Read local metadata with ffprobe without decoding the file."""
    result = subprocess.run(
        [media_binary("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    stream = next(s for s in json.loads(result.stdout)["streams"] if s["codec_type"] == "video")
    rate = stream.get("avg_frame_rate", "0/1").split("/")
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 0
    rotation = float(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = -float(side["rotation"])
    if int(stream["width"]) <= 0 or int(stream["height"]) <= 0 or not math.isfinite(fps):
        raise ValueError("Invalid video dimensions or frame rate")
    if round(rotation) % 360 not in (0, 90, 180, 270):
        raise ValueError("Only quarter-turn video rotation is supported")
    return MediaInfo(
        {"h264": "avc"}.get(stream["codec_name"], stream["codec_name"]),
        int(stream["width"]),
        int(stream["height"]),
        fps,
        round(rotation) % 360,
    )


def ffmpeg_args(path: Path, output: Path, info: MediaInfo, encoder: str) -> list[str]:
    """Build explicit rotation and aspect preserving MPEG-TS EVENT HLS argv."""
    width, height = info.width, info.height
    if info.rotation in (90, 270):
        width, height = height, width
    ratio = min(1920 / width, 1080 / height, 1)
    width, height = max(2, round(width * ratio / 2) * 2), max(2, round(height * ratio / 2) * 2)
    fps = min(info.fps or 30, 30)
    args = [
        media_binary("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-y",
        "-progress",
        str(output / "progress.log"),
    ]
    hardware = encoder == "h264_qsv"
    if hardware:
        args += [
            "-init_hw_device",
            "qsv=hw:hw,child_device=/dev/dri/renderD128",
            "-filter_hw_device",
            "hw",
            "-hwaccel",
            "qsv",
            "-hwaccel_output_format",
            "qsv",
        ]
    if encoder == "h264_vaapi":
        args += [
            "-init_hw_device",
            "vaapi=hw:/dev/dri/renderD128",
            "-filter_hw_device",
            "hw",
            "-hwaccel",
            "vaapi",
            "-hwaccel_output_format",
            "vaapi",
        ]
    args += ["-noautorotate", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?"]
    rotation = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(
        info.rotation, ""
    )
    if hardware:
        rotation = {
            90: "vpp_qsv=transpose=clock",
            180: "vpp_qsv=transpose=clock,vpp_qsv=transpose=clock",
            270: "vpp_qsv=transpose=cclock",
        }.get(info.rotation, "")
        filters = [rotation, f"vpp_qsv=w={width}:h={height}:format=nv12", f"fps={fps}", "setsar=1"]
    elif encoder == "h264_vaapi":
        rotation = {
            90: "transpose_vaapi=dir=clock",
            180: "transpose_vaapi=dir=reversal",
            270: "transpose_vaapi=dir=cclock",
        }.get(info.rotation, "")
        filters = [
            rotation,
            f"scale_vaapi=w={width}:h={height}:format=nv12",
            f"fps={fps}",
            "setsar=1",
        ]
    else:
        filters = [rotation, f"scale={width}:{height}", f"fps={fps}", "setsar=1", "format=yuv420p"]
    args += ["-vf", ",".join(f for f in filters if f), "-c:v", encoder]
    if encoder == "h264_qsv":
        # HLS boundaries require IDRs, including beyond the first QSV GOPs.
        args += ["-idr_interval", "0", "-forced_idr", "1"]
    if encoder == "libx264":
        args += ["-preset", "veryfast", "-sc_threshold", "0"]
    args += [
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-b:v",
        "6M",
        "-maxrate",
        "8M",
        "-bufsize",
        "12M",
        "-g",
        str(math.ceil(fps * 2)),
        "-bf",
        "0",
        "-force_key_frames",
        "expr:gte(t,n_forced*2)",
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        "-map_metadata",
        "-1",
        "-metadata:s:v:0",
        "rotate=0",
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_segment_type",
        "mpegts",
        "-hls_flags",
        "independent_segments+temp_file",
        "-hls_segment_filename",
        str(output / "seg%06d.ts"),
        str(output / "index.m3u8"),
    ]
    return args


def byte_range(value: str | None, size: int) -> tuple[int, int]:
    """Resolve one HTTP byte range; reject unsatisfiable or multipart ranges."""
    if value is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise ValueError("Invalid range")
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(size - 1, int(last)) if first and last else size - 1
    if start > end or start >= size:
        raise ValueError("Unsatisfiable range")
    return start, end


def media_handler(directory: Path, files: dict[str, Path] | None) -> type[BaseHTTPRequestHandler]:
    """Build a handler exposing only the session's media (never directory listings)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_HEAD(self) -> None:
            self.serve(False)

        def do_GET(self) -> None:
            self.serve(True)

        def serve(self, body: bool) -> None:
            name = unquote(urlsplit(self.path).path)
            target = files.get(name) if files else directory / name.lstrip("/")
            if (
                target is None
                or (files is None and target.resolve().parent != directory.resolve())
                or not target.is_file()
                or target.name.endswith((".tmp", ".log"))
            ):
                self.send_error(404)
                return
            try:
                with target.open("rb") as source:
                    size = os.fstat(source.fileno()).st_size
                    try:
                        start, end = byte_range(self.headers.get("Range"), size)
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = self.headers.get("Range") is not None
                    self.send_response(206 if partial else 200)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    mime = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}.get(
                        target.suffix
                    )
                    self.send_header(
                        "Content-Type",
                        mime or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                    )
                    self.send_header("Content-Length", str(max(0, end - start + 1)))
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    if body:
                        source.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = source.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
            except (OSError, ConnectionError):
                return

    return Handler


class MediaSession:
    """One local server and encoder; close cancels startup and deletes artifacts."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.server: ThreadingHTTPServer | None = None
        self.directory: Path | None = None
        atexit.register(self.close)

    def close(self) -> None:
        """Stop owned resources, including an encoder still preparing playback."""
        self.cancelled.set()
        with self.lock:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                self.process = None
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
                self.server = None
            if self.directory is not None:
                shutil.rmtree(self.directory, ignore_errors=True)
                self.directory = None
        atexit.unregister(self.close)

    def prepare(self, path: Path, host: str, caps: dict, progress: Callable[[str], None]) -> str:
        """Prepare a reachable local URL; call only from a worker thread."""
        try:
            return self._prepare(path, host, caps, progress)
        except Exception:
            self.close()
            raise

    def _prepare(self, path: Path, host: str, caps: dict, progress: Callable[[str], None]) -> str:
        progress("Inspecting local video…")
        info = probe_file(path)
        with self.lock:
            if self.cancelled.is_set():
                raise ValueError("Playback cancelled")
            cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tv-remote"
            cache.mkdir(parents=True, exist_ok=True)
            self.directory = Path(tempfile.mkdtemp(prefix="airplay-", dir=cache))
            directory = self.directory
        transcode = needs_transcode(info, caps)
        if transcode:
            errors = []
            for encoder in encoder_candidates():
                progress(f"Converting for TV ({encoder})…")
                with self.lock:
                    if self.cancelled.is_set():
                        raise ValueError("Playback cancelled")
                    for item in directory.iterdir():
                        item.unlink()
                    with (directory / "encoder.log").open("wb") as log:
                        self.process = subprocess.Popen(
                            ffmpeg_args(path, directory, info, encoder),
                            stdout=subprocess.DEVNULL,
                            stderr=log,
                        )
                    process = self.process
                deadline = time.monotonic() + 90
                while not self.cancelled.wait(0.2):
                    playlist = directory / "index.m3u8"
                    text = playlist.read_text() if playlist.exists() else ""
                    if text.count("#EXTINF:") >= 3 or (
                        "#EXT-X-ENDLIST" in text and "#EXTINF:" in text
                    ):
                        break
                    if process.poll() is not None or time.monotonic() > deadline:
                        break
                if self.cancelled.is_set():
                    raise ValueError("Playback cancelled")
                if (
                    process.poll() is None
                    and time.monotonic() <= deadline
                    and text.count("#EXTINF:") >= 3
                ):
                    break
                if process.poll() == 0 and "#EXT-X-ENDLIST" in text:
                    break
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                errors.append((directory / "encoder.log").read_text(errors="replace")[-800:])
            else:
                raise ValueError("No usable H.264 encoder: " + errors[-1])
        files = {"/media" + path.suffix: path} if not transcode else None

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect((host, 7000))
            address = route.getsockname()[0]
        with self.lock:
            if self.cancelled.is_set():
                raise ValueError("Playback cancelled")
            self.server = ThreadingHTTPServer((address, 0), media_handler(directory, files))
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            port = self.server.server_port
        if transcode:
            threading.Thread(target=self._monitor, args=(progress,), daemon=True).start()
        progress("Ready · converting in background" if transcode else "Ready · original file")
        return f"http://{address}:{port}/" + (
            "index.m3u8" if transcode else "media" + quote(path.suffix, safe="")
        )

    def _monitor(self, progress: Callable[[str], None]) -> None:
        """Report encoding progress and failures after the initial buffer is ready."""
        process, directory = self.process, self.directory
        if process is None or directory is None:
            return
        while not self.cancelled.wait(1):
            try:
                with (directory / "progress.log").open("rb") as report:
                    report.seek(max(0, report.seek(0, 2) - 4096))
                    times = re.findall(
                        r"out_time=([^\n]+)", report.read().decode(errors="replace")
                    )
                if times:
                    progress("Converted " + times[-1].split(".")[0] + " · playing on TV")
                code = process.poll()
                if code is not None:
                    if code == 0:
                        progress("Conversion complete · playing on TV")
                    else:
                        progress("Conversion failed; stop and retry or select another file")
                    return
            except OSError:
                return
