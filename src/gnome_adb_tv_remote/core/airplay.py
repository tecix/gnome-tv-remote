"""
AirPlay video sender.

Sends video links to an AirPlay v1 video receiver over plain HTTP, the way Web
Video Caster and ExoAirPlayer do: /play with a text/parameters body
(Content-Location, Start-Position, Caption-Location), /rate, /scrub, /stop and
the ExoAirPlayer subtitle extensions. TinyTV Receiver (port 7000 on the TV) also
answers GET /tinytv/status with JSON and /select-captions?index=n; other
receivers fall back to the standard /playback-info plist.

Every call blocks, so run them off the GTK main thread.
"""

from __future__ import annotations

import http.client
import json
import plistlib
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

AIRPLAY_PORT = 7000

_USER_AGENT = "TvRemote/1.1 (AirPlay sender)"


class AirPlayError(Exception):
    """The receiver can't be reached or refused a request."""


@dataclass
class AirPlayStatus:
    """What the receiver is playing; subtitle details come from TinyTV only."""

    state: str = "IDLE"  # IDLE, LOADING, PLAYING, PAUSED or ENDED
    position: float = 0.0  # seconds
    duration: float = 0.0  # seconds, 0 when unknown (live streams)
    url: str | None = None
    error: str | None = None
    captions: list[str] = field(default_factory=list)
    caption: int = -1  # index into captions, -1 when hidden
    caption_offset_ms: int = 0
    extended: bool = False  # True when it came from /tinytv/status

    @property
    def active(self) -> bool:
        return self.state in ("LOADING", "PLAYING", "PAUSED")


def parse_receiver(text: str) -> tuple[str, int]:
    """Split "host", "host:port" or "[v6]:port" into host and port."""
    text = text.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if not text:
        raise ValueError("No receiver address")
    if text.count(":") > 1 and not text.startswith("["):
        return text, AIRPLAY_PORT  # bare IPv6 address
    parts = urlsplit("//" + text)
    try:
        port = parts.port or AIRPLAY_PORT
    except ValueError as e:
        raise ValueError(f"Bad port in {text}") from e
    if not parts.hostname:
        raise ValueError(f"Bad receiver address {text}")
    return parts.hostname, port


def check_web_url(url: str) -> str:
    """Return the trimmed URL, or raise ValueError unless it is a plain http(s) link."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("AirPlay needs an http:// or https:// link")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("The link contains control characters")
    return url


class AirPlayClient:
    """One AirPlay video receiver; each method is a single HTTP request."""

    def __init__(self, host: str, port: int = AIRPLAY_PORT) -> None:
        self.host = host
        self.port = port
        self._session = str(uuid.uuid4()).upper()

    @property
    def label(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == AIRPLAY_PORT else f"{host}:{self.port}"

    def capabilities(self) -> dict:
        """Read TinyTV limits, falling back only for older receivers (404)."""
        from .local_media import DEFAULT_CAPS

        try:
            value = json.loads(self._request("GET", "/tinytv/caps", timeout=4.0))
            if not isinstance(value, dict):
                raise ValueError("Invalid receiver capabilities")
            return value
        except AirPlayError as error:
            if "doesn't support /tinytv/caps" in str(error):
                return DEFAULT_CAPS
            raise

    def server_info(self) -> dict:
        """GET /server-info; raises AirPlayError when nothing answers."""
        body = self._request("GET", "/server-info", timeout=4.0)
        try:
            info = plistlib.loads(body)
        except Exception as e:
            raise AirPlayError(f"{self.label} doesn't look like an AirPlay receiver") from e
        return info if isinstance(info, dict) else {}

    def play(self, url: str, subtitle: str | None = None, start_seconds: float = 0.0) -> None:
        lines = [
            f"Content-Location: {check_web_url(url)}",
            f"Start-Position: {max(0.0, start_seconds):.3f}",
        ]
        if subtitle:
            lines.append(f"Caption-Location: {check_web_url(subtitle)}")
        # The receiver checks the stream (and may set up remuxing) before it answers
        self._request("POST", "/play", "\n".join(lines) + "\n", timeout=25.0)

    def set_playing(self, playing: bool) -> None:
        self._request("POST", f"/rate?value={1.0 if playing else 0.0:.6f}")

    def seek(self, seconds: float) -> None:
        self._request("POST", f"/scrub?position={max(0.0, seconds):.3f}")

    def stop(self) -> None:
        self._request("POST", "/stop")

    def load_captions(self, url: str) -> None:
        """Adds a subtitle file to the current video."""
        self._request("POST", "/load-captions", f"Caption-Location: {check_web_url(url)}\n")

    def select_captions(self, index: int) -> None:
        """Shows subtitle track index; -1 hides them (TinyTV only)."""
        self._request("POST", f"/select-captions?index={index}")

    def set_captions_offset(self, offset_ms: int) -> None:
        """Subtitle delay; positive shows them later."""
        self._request("POST", f"/set-captions-offset?value={offset_ms * 1000}")

    def status(self) -> AirPlayStatus:
        try:
            return self._parse_tinytv_status(self._request("GET", "/tinytv/status", timeout=4.0))
        except AirPlayError:
            pass
        return self._parse_playback_info(self._request("GET", "/playback-info", timeout=4.0))

    def _request(
        self, method: str, path: str, body: str | None = None, timeout: float = 6.0
    ) -> bytes:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"User-Agent": _USER_AGENT, "X-Apple-Session-ID": self._session}
        data = None
        if body is not None or method == "POST":
            data = (body or "").encode("utf-8")
            headers["Content-Type"] = "text/parameters"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise AirPlayError(f"Can't reach AirPlay on {self.label}: {e}") from e
        finally:
            conn.close()
        name = path.split("?", 1)[0]
        if resp.status == 404:
            raise AirPlayError(f"The AirPlay receiver doesn't support {name}")
        if not 200 <= resp.status < 300:
            detail = payload.decode("utf-8", "replace").strip()
            raise AirPlayError(
                f"AirPlay {name} failed: HTTP {resp.status}"
                + (f" ({detail[:200]})" if detail else "")
            )
        return payload

    @staticmethod
    def _parse_tinytv_status(body: bytes) -> AirPlayStatus:
        try:
            o = json.loads(body)
        except ValueError as e:
            raise AirPlayError("Bad /tinytv/status reply") from e
        captions = [str(c) for c in o.get("captions") or []]
        caption = int(o.get("caption", -1))
        return AirPlayStatus(
            state=str(o.get("state") or "IDLE"),
            position=max(0.0, float(o.get("position") or 0)),
            duration=max(0.0, float(o.get("duration") or 0)),
            url=o.get("url"),
            error=o.get("error") or None,
            captions=captions,
            caption=caption if 0 <= caption < len(captions) else -1,
            caption_offset_ms=int(o.get("captionOffsetMs") or 0),
            extended=True,
        )

    @staticmethod
    def _parse_playback_info(body: bytes) -> AirPlayStatus:
        try:
            info = plistlib.loads(body) if body.strip() else {}
        except Exception:
            info = {}
        if not isinstance(info, dict) or "duration" not in info:
            return AirPlayStatus()
        return AirPlayStatus(
            state="PLAYING" if float(info.get("rate") or 0) > 0 else "PAUSED",
            position=max(0.0, float(info.get("position") or 0)),
            duration=max(0.0, float(info.get("duration") or 0)),
        )
