"""
Android TV Remote protocol v2 client.

Talks to Google TV / Android TV devices (and receivers such as TinyTV Receiver)
the same way the Google TV phone app does:

- Port 6467, pairing ("Polo"): the TV shows a 6-digit hex code that the user
  types into this app. Afterwards the TV trusts our TLS client certificate.
- Port 6466, remote control: key presses, text input and app links go to the
  TV; volume, power and current-app updates come back.

Both ports speak TLS with the self-signed client certificate from `keystore`.
Every message is a varint length-prefixed protobuf. The handful of messages we
need are encoded by hand, so the app needs no protobuf dependency.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import rsa

from .keystore import ensure_client_cert_exists, load_client_public_key

logger = logging.getLogger(__name__)

REMOTE_PORT = 6466
PAIRING_PORT = 6467

# Android keycodes (subset for TV remote + keyboard input)
AKEYCODE_MAP = {
    # Navigation
    "KEYCODE_DPAD_UP": 19,
    "KEYCODE_DPAD_DOWN": 20,
    "KEYCODE_DPAD_LEFT": 21,
    "KEYCODE_DPAD_RIGHT": 22,
    "KEYCODE_DPAD_CENTER": 23,
    # System
    "KEYCODE_BACK": 4,
    "KEYCODE_HOME": 3,
    "KEYCODE_MENU": 82,
    "KEYCODE_NOTIFICATION": 83,
    # Volume
    "KEYCODE_VOLUME_UP": 24,
    "KEYCODE_VOLUME_DOWN": 25,
    "KEYCODE_VOLUME_MUTE": 164,
    # Power
    "KEYCODE_POWER": 26,
    "KEYCODE_SLEEP": 223,
    "KEYCODE_WAKEUP": 224,
    # Media
    "KEYCODE_MEDIA_PLAY_PAUSE": 85,
    "KEYCODE_MEDIA_STOP": 86,
    "KEYCODE_MEDIA_NEXT": 87,
    "KEYCODE_MEDIA_PREVIOUS": 88,
    "KEYCODE_MEDIA_REWIND": 89,
    "KEYCODE_MEDIA_FAST_FORWARD": 90,
    "KEYCODE_MEDIA_PLAY": 126,
    "KEYCODE_MEDIA_PAUSE": 127,
    "KEYCODE_MEDIA_RECORD": 130,
    # Apps & TV
    "KEYCODE_ALL_APPS": 284,
    "KEYCODE_TV": 170,
    "KEYCODE_TV_INPUT": 178,
    "KEYCODE_SETTINGS": 176,
    "KEYCODE_INFO": 165,
    "KEYCODE_GUIDE": 172,
    # Channels
    "KEYCODE_CHANNEL_UP": 166,
    "KEYCODE_CHANNEL_DOWN": 167,
    # Assistant
    "KEYCODE_ASSIST": 219,
    # Accessibility
    "KEYCODE_CAPTIONS": 175,
    # Keyboard input
    "KEYCODE_ENTER": 66,
    "KEYCODE_DEL": 67,  # Backspace
    "KEYCODE_FORWARD_DEL": 112,  # Delete
    "KEYCODE_TAB": 61,
    "KEYCODE_SPACE": 62,
    "KEYCODE_ESCAPE": 111,
    # Numpad (0-9 for channel input)
    "KEYCODE_0": 7,
    "KEYCODE_1": 8,
    "KEYCODE_2": 9,
    "KEYCODE_3": 10,
    "KEYCODE_4": 11,
    "KEYCODE_5": 12,
    "KEYCODE_6": 13,
    "KEYCODE_7": 14,
    "KEYCODE_8": 15,
    "KEYCODE_9": 16,
    # Colored keys (Teletext/HbbTV)
    "KEYCODE_PROG_RED": 183,
    "KEYCODE_PROG_GREEN": 184,
    "KEYCODE_PROG_YELLOW": 185,
    "KEYCODE_PROG_BLUE": 186,
}

# RemoteDirection
_DIRECTION_START_LONG = 1
_DIRECTION_END_LONG = 2
_DIRECTION_SHORT = 3

# Feature bits we announce: PING | KEY | IME | POWER | VOLUME | APP_LINK
_FEATURES = 1 | 2 | 4 | 32 | 64 | 512

# Polo (pairing) constants
_POLO_PROTOCOL_VERSION = 2
_POLO_STATUS_OK = 200
_ENCODING_HEXADECIMAL = 3
_ROLE_INPUT = 1
_CODE_LENGTH = 6

# The TV pings every few seconds; this much silence means the link is dead.
_READ_TIMEOUT_S = 30.0


class RemoteError(Exception):
    """Base exception for Remote protocol errors."""


class RemoteConnectError(RemoteError):
    """The TV could not be reached on the remote/pairing port."""


class NotPairedError(RemoteError):
    """The TV refused our client certificate; pairing is required."""


class PairingError(RemoteError):
    """Pairing failed (wrong code, rejected by the TV, …)."""


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str
    model: str


@dataclass(frozen=True)
class VolumeInfo:
    level: int
    maximum: int
    muted: bool


# ─────────────────────────────────────────────────────────────────────────────
# Protobuf wire format helpers
# ─────────────────────────────────────────────────────────────────────────────

def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1  # negative int32 values are sent as 10-byte two's complement
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _msg(*fields: tuple[int, int | bool | str | bytes | None]) -> bytes:
    """Encode (field number, value) pairs; ints are varints, str/bytes are length-delimited."""
    out = bytearray()
    for num, value in fields:
        if value is None:
            continue
        if isinstance(value, (bool, int)):
            out += _varint(num << 3) + _varint(int(value))
        else:
            data = value.encode() if isinstance(value, str) else value
            out += _varint(num << 3 | 2) + _varint(len(data)) + data
    return bytes(out)


def _parse(data: bytes) -> dict[int, list[int | bytes]]:
    """Decode a message into {field number: [values]} (varints as int, rest as bytes)."""
    fields: dict[int, list[int | bytes]] = {}
    i = 0

    def read_varint() -> int:
        nonlocal i
        result = shift = 0
        while True:
            b = data[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while i < len(data):
        key = read_varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            value: int | bytes = read_varint()
        elif wire == 2:
            n = read_varint()
            value = data[i:i + n]
            i += n
        elif wire == 1:
            value = data[i:i + 8]
            i += 8
        elif wire == 5:
            value = data[i:i + 4]
            i += 4
        else:
            raise RemoteError(f"Unsupported protobuf wire type {wire}")
        fields.setdefault(num, []).append(value)
    return fields


def _first(fields: dict, num: int, default=None):
    values = fields.get(num)
    return values[0] if values else default


def _sub(fields: dict, num: int) -> dict:
    value = _first(fields, num)
    return _parse(value) if isinstance(value, bytes) else {}


def _text(fields: dict, num: int) -> str:
    value = _first(fields, num, b"")
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


# ─────────────────────────────────────────────────────────────────────────────
# TLS framing
# ─────────────────────────────────────────────────────────────────────────────

def _open_tls(host: str, port: int, timeout_s: float) -> ssl.SSLSocket:
    """Connect with our client certificate. The TV's certificate is self-signed, so it isn't verified."""
    paths = ensure_client_cert_exists()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(str(paths.certificate), str(paths.private_key))

    try:
        raw = socket.create_connection((host, port), timeout=timeout_s)
    except OSError as e:
        raise RemoteConnectError(f"Cannot reach {host}:{port} ({e})") from e
    try:
        return ctx.wrap_socket(raw)
    except (ssl.SSLError, OSError) as e:
        raw.close()
        # A TV that doesn't know our certificate aborts the handshake.
        raise NotPairedError("The TV rejected this computer; pair it first.") from e


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed by the TV")
        buf += chunk
    return bytes(buf)


def _read_frame(sock: ssl.SSLSocket) -> bytes:
    length = shift = 0
    while True:
        b = _recv_exact(sock, 1)[0]
        length |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 28:
            raise RemoteError("Bad message length")
    return _recv_exact(sock, length)


def _frame(payload: bytes) -> bytes:
    return _varint(len(payload)) + payload


def _rsa_key_from_cert(cert_der: bytes) -> rsa.PublicKey:
    """Extract the RSA public key from a DER certificate (Certificate → TBS → SubjectPublicKeyInfo)."""
    def items(data: bytes):
        i = 0
        while i < len(data):
            tag, n = data[i], data[i + 1]
            i += 2
            if n & 0x80:
                k = n & 0x7F
                n = int.from_bytes(data[i:i + k], "big")
                i += k
            yield tag, data[i:i + n]
            i += n

    cert = next(items(cert_der))[1]
    tbs = next(items(cert))[1]
    # serial, signature, issuer, validity, subject, subjectPublicKeyInfo (version [0] skipped)
    tbs_fields = [content for tag, content in items(tbs) if tag != 0xA0]
    bit_string = list(items(tbs_fields[5]))[1][1]
    return rsa.PublicKey.load_pkcs1(bit_string[1:], format="DER")


def _unsigned(n: int) -> bytes:
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


# ─────────────────────────────────────────────────────────────────────────────
# Pairing (port 6467)
# ─────────────────────────────────────────────────────────────────────────────

class PairingSession:
    """One pairing attempt: `start()` makes the TV show a code, `finish(code)` completes it.

    Both calls block and should be run in a worker thread.
    """

    def __init__(self, host: str, *, client_name: str = "TV Remote", timeout_s: float = 10.0) -> None:
        self._host = host
        self._client_name = client_name
        self._timeout_s = timeout_s
        self._sock: ssl.SSLSocket | None = None
        self._server_key: rsa.PublicKey | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def start(self) -> str:
        """Begin pairing; the TV displays a code afterwards. Returns the TV's name."""
        paths = ensure_client_cert_exists()
        try:
            raw = socket.create_connection((self._host, PAIRING_PORT), timeout=self._timeout_s)
        except OSError as e:
            raise RemoteConnectError(f"Cannot reach {self._host}:{PAIRING_PORT} ({e})") from e
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(str(paths.certificate), str(paths.private_key))
        try:
            self._sock = ctx.wrap_socket(raw)
            self._server_key = _rsa_key_from_cert(self._sock.getpeercert(binary_form=True))

            encoding = _msg((1, _ENCODING_HEXADECIMAL), (2, _CODE_LENGTH))
            self._send(10, _msg((1, "atvremote"), (2, self._client_name)))  # PairingRequest
            ack = self._expect(11)
            self._send(20, _msg((1, encoding), (3, _ROLE_INPUT)))  # Options
            self._expect(20)
            self._send(30, _msg((1, encoding), (2, _ROLE_INPUT)))  # Configuration
            self._expect(31)
        except PairingError:
            self.close()
            raise
        except (ssl.SSLError, OSError, ConnectionError, IndexError, ValueError, StopIteration) as e:
            self.close()
            raise PairingError(f"Pairing failed: {e}") from e
        return _text(ack, 1) or self._host

    def finish(self, code: str) -> None:
        """Send the code shown on the TV. Raises PairingError if it is wrong."""
        code = code.strip().upper()
        if len(code) != _CODE_LENGTH or any(c not in "0123456789ABCDEF" for c in code):
            raise PairingError("Enter the 6 characters shown on the TV.")
        if not self._sock or not self._server_key:
            raise PairingError("Pairing was not started.")

        client_key = load_client_public_key(ensure_client_cert_exists())
        digest = hashlib.sha256(
            _unsigned(client_key.n) + _unsigned(client_key.e)
            + _unsigned(self._server_key.n) + _unsigned(self._server_key.e)
            + bytes.fromhex(code[2:])
        ).digest()
        # The first byte of the code is a checksum, so typos are caught before the TV sees them.
        if digest[0] != int(code[:2], 16):
            raise PairingError("Wrong code, check the TV and try again.")

        try:
            self._send(40, _msg((1, digest)))  # Secret
            self._expect(41)
        except (ssl.SSLError, OSError, ConnectionError) as e:
            raise PairingError(f"Pairing failed: {e}") from e
        finally:
            self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, field: int, payload: bytes) -> None:
        assert self._sock
        self._sock.sendall(_frame(_msg(
            (1, _POLO_PROTOCOL_VERSION), (2, _POLO_STATUS_OK), (field, payload)
        )))

    def _expect(self, field: int) -> dict:
        assert self._sock
        msg = _parse(_read_frame(self._sock))
        status = _first(msg, 2, 0)
        if status != _POLO_STATUS_OK:
            raise PairingError(f"The TV rejected pairing (status {status}).")
        if field not in msg:
            raise PairingError("Unexpected reply from the TV.")
        return _sub(msg, field)


# ─────────────────────────────────────────────────────────────────────────────
# Remote control (port 6466)
# ─────────────────────────────────────────────────────────────────────────────

class AndroidTvRemote:
    """Connection to the TV's remote service.

    `connect()` blocks (run it in a worker thread). After that, a reader thread
    answers pings and reports state through the handlers, which are called from
    that thread.
    """

    def __init__(self, host: str, *, timeout_s: float = 8.0) -> None:
        self._host = host
        self._timeout_s = timeout_s
        self._sock: ssl.SSLSocket | None = None
        self._send_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closing = False
        self._active = False
        self._device_info: DeviceInfo | None = None

        # Text field mirror: the protocol replaces the whole field on every edit.
        self._ime_counter = 0
        self._ime_field_counter = 0
        self._ime_text = ""

        self._on_disconnect: Optional[Callable[[], None]] = None
        self._on_volume: Optional[Callable[[VolumeInfo], None]] = None
        self._on_power: Optional[Callable[[bool], None]] = None
        self._on_current_app: Optional[Callable[[str], None]] = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._active

    @property
    def device_info(self) -> DeviceInfo | None:
        return self._device_info

    def set_handlers(
        self,
        *,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_volume: Optional[Callable[[VolumeInfo], None]] = None,
        on_power: Optional[Callable[[bool], None]] = None,
        on_current_app: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_disconnect = on_disconnect
        self._on_volume = on_volume
        self._on_power = on_power
        self._on_current_app = on_current_app

    def connect(self) -> DeviceInfo:
        """Connect and finish the handshake. Raises NotPairedError if pairing is needed."""
        sock = _open_tls(self._host, REMOTE_PORT, self._timeout_s)
        self._sock = sock
        self._closing = False
        try:
            # The TV opens with RemoteConfigure, then RemoteSetActive; we're ready once both are answered.
            deadline = time.monotonic() + self._timeout_s
            while not self._active:
                if time.monotonic() > deadline:
                    raise RemoteConnectError("The TV did not finish the handshake.")
                self._handle(_parse(_read_frame(sock)))
        except RemoteError:
            self._close_socket()
            raise
        except (ssl.SSLError, OSError, ConnectionError) as e:
            self._close_socket()
            if self._device_info is None:
                # TLS 1.3 reports a rejected client certificate only on the first read.
                raise NotPairedError("The TV rejected this computer; pair it first.") from e
            raise RemoteConnectError(str(e) or "Connection failed") from e

        sock.settimeout(_READ_TIMEOUT_S)
        self._reader = threading.Thread(target=self._read_loop, name="remote-v2-reader", daemon=True)
        self._reader.start()
        assert self._device_info
        return self._device_info

    def disconnect(self) -> None:
        self._closing = True
        self._close_socket()

    # ── Commands ────────────────────────────────────────────────────────────

    def send_keycode(self, keycode_name: str) -> None:
        """Send a short key press."""
        if keycode_name == "KEYCODE_DEL" and self._ime_text:
            self._set_text(self._ime_text[:-1])
            return
        if keycode_name not in ("KEYCODE_FORWARD_DEL",):
            # Any navigation or confirmation probably leaves the text field.
            self._ime_text = ""
        self._send_key(keycode_name, _DIRECTION_SHORT)

    def send_key_down(self, keycode_name: str) -> None:
        self._send_key(keycode_name, _DIRECTION_START_LONG)

    def send_key_up(self, keycode_name: str) -> None:
        self._send_key(keycode_name, _DIRECTION_END_LONG)

    def send_long_press(self, keycode_name: str, duration_ms: int = 600) -> None:
        """Hold a key. Blocks for `duration_ms`."""
        self.send_key_down(keycode_name)
        time.sleep(duration_ms / 1000.0)
        self.send_key_up(keycode_name)

    def send_text(self, text: str) -> None:
        """Type text into the focused field on the TV."""
        self._set_text(self._ime_text + text)

    def reset_text(self) -> None:
        """Forget the mirrored field text, e.g. when the user starts typing anew."""
        self._ime_text = ""

    def expand_notification_panel(self) -> None:
        self._send_key("KEYCODE_NOTIFICATION", _DIRECTION_SHORT)

    def send_app_link(self, link: str) -> None:
        """Open a deep link or `market://launch?id=<package>` on the TV."""
        self._send(_msg((90, _msg((1, link)))))

    # ── Internals ───────────────────────────────────────────────────────────

    def _send_key(self, keycode_name: str, direction: int) -> None:
        code = AKEYCODE_MAP.get(keycode_name)
        if code is None:
            raise RemoteError(f"Unknown keycode: {keycode_name}")
        self._send(_msg((10, _msg((1, code), (2, direction)))))

    def _set_text(self, text: str) -> None:
        self._ime_text = text
        cursor = len(text) - 1
        field = _msg((1, cursor), (2, cursor), (3, text))
        edit = _msg((1, 1), (2, field))
        self._send(_msg((21, _msg((1, self._ime_counter), (2, self._ime_field_counter), (3, edit)))))

    def _send(self, payload: bytes) -> None:
        sock = self._sock
        if not sock:
            raise RemoteConnectError("Not connected")
        try:
            with self._send_lock:
                sock.sendall(_frame(payload))
        except (ssl.SSLError, OSError) as e:
            raise RemoteConnectError(str(e) or "Send failed") from e

    def _handle(self, msg: dict) -> None:
        if 1 in msg:  # RemoteConfigure
            info = _sub(_sub(msg, 1), 2)
            self._device_info = DeviceInfo(
                manufacturer=_text(info, 2) or "Unknown",
                model=_text(info, 1) or "Android TV",
            )
            device = _msg(
                (1, "TV Remote"), (2, "Linux"), (3, 1), (4, "1"),
                (5, "atvremote"), (6, "1.0.0"),
            )
            self._send(_msg((1, _msg((1, _FEATURES), (2, device)))))
        elif 2 in msg:  # RemoteSetActive
            self._send(_msg((2, _msg((1, _FEATURES)))))
            self._active = True
        elif 8 in msg:  # RemotePingRequest
            self._send(_msg((9, _msg((1, _first(_sub(msg, 8), 1, 0))))))
        elif 3 in msg:  # RemoteError
            logger.warning("TV reported an error: %s", _sub(msg, 3))
        elif 20 in msg:  # RemoteImeKeyInject: current app and text field
            inject = _sub(msg, 20)
            package = _text(_sub(inject, 1), 12)
            self._update_text_field(_sub(inject, 2))
            if package and self._on_current_app:
                self._on_current_app(package)
        elif 21 in msg:  # RemoteImeBatchEdit
            edit = _sub(msg, 21)
            self._ime_counter = _first(edit, 1, self._ime_counter)
            self._ime_field_counter = _first(edit, 2, self._ime_field_counter)
        elif 22 in msg:  # RemoteImeShowRequest
            self._update_text_field(_sub(_sub(msg, 22), 2))
        elif 40 in msg:  # RemoteStart
            if self._on_power:
                self._on_power(bool(_first(_sub(msg, 40), 1, 0)))
        elif 50 in msg:  # RemoteSetVolumeLevel
            vol = _sub(msg, 50)
            if self._on_volume:
                self._on_volume(VolumeInfo(
                    level=_first(vol, 7, 0),
                    maximum=_first(vol, 6, 0) or 100,
                    muted=bool(_first(vol, 8, 0)),
                ))

    def _update_text_field(self, status: dict) -> None:
        if not status:
            return
        counter = _first(status, 1)
        if counter is not None and counter != self._ime_field_counter:
            self._ime_field_counter = counter
            self._ime_text = _text(status, 2)

    def _read_loop(self) -> None:
        sock = self._sock
        try:
            while sock is not None and sock is self._sock:
                self._handle(_parse(_read_frame(sock)))
        except Exception as e:
            if not self._closing:
                logger.info("Remote connection to %s ended: %s", self._host, e)
        finally:
            was_closing = self._closing
            if sock is self._sock:
                self._close_socket()
            if not was_closing and self._on_disconnect:
                self._on_disconnect()

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        self._active = False
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def probe_paired_device(host: str, timeout_s: float = 3.0) -> DeviceInfo | None:
    """Return device info if `host` runs the remote service and already trusts us, else None."""
    remote = AndroidTvRemote(host, timeout_s=timeout_s)
    try:
        return remote.connect()
    except RemoteError:
        return None
    finally:
        remote.disconnect()
