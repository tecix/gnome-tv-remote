"""
Home Assistant input backend.

Sends remote buttons through Home Assistant's Android Debug Bridge integration
(`remote.send_command` on the TV's `remote.*` entity) instead of the Android TV
Remote protocol. HA injects keys with `input keyevent` over ADB, which works on
screens that an accessibility-based receiver cannot steer.

Every string that is not one of HA's named keys is run as an ADB shell command,
so all buttons are sent as plain `input keyevent <code>` commands.

The token is kept in a private file in the config directory, not in GSettings.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from .remote_v2 import AKEYCODE_MAP

logger = logging.getLogger(__name__)

DEFAULT_ENTITY = "remote.living_room_philips_tv"

# Commands waiting for HA beyond this are dropped, so a held D-pad key doesn't
# keep moving the focus long after it was released.
_MAX_PENDING = 4
_TIMEOUT_S = 10.0


class HomeAssistantError(Exception):
    """Home Assistant could not be reached or refused the request."""


def _token_path(app_dir_name: str = "android-tv-remote") -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / app_dir_name / "home-assistant" / "token"


def load_token() -> str:
    try:
        return _token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_token(token: str) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions before writing the secret
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Best effort if filesystem doesn't support permissions


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


class HomeAssistantRemote:
    """Sends key presses to the TV through Home Assistant.

    Offers the same send methods as `AndroidTvRemote`, so the window can route
    buttons to either one. Requests run on one worker thread, in order; the
    send methods never block.
    """

    def __init__(
        self,
        url: str,
        token: str,
        entity_id: str = DEFAULT_ENTITY,
        *,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        parts = urlsplit(url.strip().rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HomeAssistantError("Home Assistant URL must start with http:// or https://")
        self._scheme = parts.scheme
        self._netloc = parts.netloc
        self._base_path = parts.path
        self._token = token.strip()
        self._entity_id = entity_id.strip() or DEFAULT_ENTITY
        self._on_error = on_error

        self._jobs: queue.Queue[Optional[Callable[[], None]]] = queue.Queue()
        self._conn: http.client.HTTPConnection | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="home-assistant", daemon=True)
        self._worker.start()

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def connected(self) -> bool:
        return not self._closed

    @property
    def device_info(self) -> None:
        return None

    # ── Commands (same names as AndroidTvRemote) ───────────────────────────

    def send_keycode(self, keycode_name: str) -> None:
        self._command(f"input keyevent {self._code(keycode_name)}", droppable=True)

    def send_long_press(self, keycode_name: str, duration_ms: int = 600) -> None:
        self._command(f"input keyevent --longpress {self._code(keycode_name)}")

    def send_text(self, text: str) -> None:
        # `input text` treats %s as a space; quote everything else for the shell.
        self._command(f"input text {_shell_quote(text.replace(' ', '%s'))}")

    def reset_text(self) -> None:
        pass  # Text goes in as typed; there is no field mirror to reset.

    def expand_notification_panel(self) -> None:
        self._command("service call statusbar 1")

    def check_async(self, on_result: Callable[[str | None, str | None], None]) -> None:
        """Verify URL, token and entity on the worker thread (this also warms up the connection).

        `on_result(friendly_name, error)` is called from the worker thread.
        """
        def job() -> None:
            try:
                state = self._request("GET", f"/api/states/{self._entity_id}")
            except HomeAssistantError as e:
                on_result(None, str(e))
                return
            name = (state or {}).get("attributes", {}).get("friendly_name") or self._entity_id
            on_result(name, None)

        self._jobs.put(job)

    def close(self) -> None:
        self._closed = True
        self._jobs.put(None)

    # ── Internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _code(keycode_name: str) -> int:
        code = AKEYCODE_MAP.get(keycode_name)
        if code is None:
            raise HomeAssistantError(f"Unknown keycode: {keycode_name}")
        return code

    def _command(self, command: str, *, droppable: bool = False) -> None:
        if self._closed:
            raise HomeAssistantError("Home Assistant backend is closed")
        if droppable and self._jobs.qsize() >= _MAX_PENDING:
            logger.debug("Home Assistant is lagging; dropped %s", command)
            return

        def job() -> None:
            t0 = time.monotonic()
            try:
                self._request("POST", "/api/services/remote/send_command",
                              {"entity_id": self._entity_id, "command": command})
            except HomeAssistantError as e:
                logger.warning("Home Assistant command %r failed: %s", command, e)
                if self._on_error:
                    self._on_error(str(e))
                return
            logger.debug("HA %r took %.0f ms", command, (time.monotonic() - t0) * 1000)

        self._jobs.put(job)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception:  # pragma: no cover - keep the worker alive
                logger.exception("Home Assistant job crashed")
        if self._conn:
            self._conn.close()

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            self._conn = cls(self._netloc, timeout=_TIMEOUT_S)
        return self._conn

    def _request(self, method: str, path: str, body: dict | None = None):
        """Send one request on the kept-alive connection, reconnecting once if it went stale."""
        if not self._token:
            raise HomeAssistantError("No Home Assistant access token set")
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None

        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request(method, self._base_path + path, body=data, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
                break
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                self._conn = None
                if attempt == 2:
                    raise HomeAssistantError(f"Cannot reach Home Assistant ({e})") from e

        if resp.status == 401:
            raise HomeAssistantError("Home Assistant rejected the access token")
        if resp.status == 404:
            raise HomeAssistantError(f"Home Assistant has no entity {self._entity_id}")
        if resp.status >= 400:
            raise HomeAssistantError(f"Home Assistant answered HTTP {resp.status}")
        try:
            return json.loads(payload) if payload else None
        except ValueError:
            return None
