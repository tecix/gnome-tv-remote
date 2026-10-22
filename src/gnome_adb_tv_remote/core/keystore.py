"""
Client Certificate Generation and Storage.

The Android TV Remote protocol v2 authenticates the remote by its TLS client
certificate: the TV remembers the certificate's public key once pairing succeeds.
This module creates a self-signed RSA certificate on first use and keeps it in the
XDG config directory, so pairing survives restarts.

The certificate is assembled by hand (a small DER encoder) and signed with `rsa`,
which keeps the app free of the `cryptography` library for Flatpak distribution.
"""

from __future__ import annotations

import base64
import os
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rsa


@dataclass(frozen=True)
class ClientCertPaths:
    certificate: Path
    private_key: Path


def _config_dir(app_dir_name: str = "android-tv-remote") -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / app_dir_name
    return Path.home() / ".config" / app_dir_name


def get_client_cert_paths(app_dir_name: str = "android-tv-remote") -> ClientCertPaths:
    base = _config_dir(app_dir_name) / "remote"
    return ClientCertPaths(certificate=base / "client.crt", private_key=base / "client.key")


# ─────────────────────────────────────────────────────────────────────────────
# Minimal DER encoding, just enough for a self-signed X.509 v3 certificate.
# ─────────────────────────────────────────────────────────────────────────────

_SEQUENCE = 0x30
_SET = 0x31
_INTEGER = 0x02
_BIT_STRING = 0x03
_UTF8_STRING = 0x0C
_UTC_TIME = 0x17
_GENERALIZED_TIME = 0x18

#: AlgorithmIdentifier { sha256WithRSAEncryption, NULL }
_SHA256_WITH_RSA = bytes.fromhex("300d06092a864886f70d01010b0500")
#: AlgorithmIdentifier { rsaEncryption, NULL }
_RSA_ENCRYPTION = bytes.fromhex("300d06092a864886f70d0101010500")
#: OID 2.5.4.3 (commonName)
_COMMON_NAME_OID = bytes.fromhex("0603550403")


def _der(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def _der_int(n: int) -> bytes:
    # One spare bit keeps positive numbers from reading as negative.
    return _der(_INTEGER, n.to_bytes(n.bit_length() // 8 + 1, "big"))


def _der_name(common_name: str) -> bytes:
    atv = _der(_SEQUENCE, _COMMON_NAME_OID + _der(_UTF8_STRING, common_name.encode()))
    return _der(_SEQUENCE, _der(_SET, atv))


def _build_certificate(pub_key: rsa.PublicKey, priv_key: rsa.PrivateKey, common_name: str) -> bytes:
    """Return a DER-encoded self-signed certificate for the given key pair."""
    not_before = datetime.now(timezone.utc) - timedelta(days=1)
    validity = _der(
        _SEQUENCE,
        _der(_UTC_TIME, not_before.strftime("%y%m%d%H%M%SZ").encode())
        + _der(_GENERALIZED_TIME, b"20991231235959Z"),
    )
    spki = _der(
        _SEQUENCE,
        _RSA_ENCRYPTION + _der(_BIT_STRING, b"\x00" + pub_key.save_pkcs1(format="DER")),
    )
    name = _der_name(common_name)
    tbs = _der(
        _SEQUENCE,
        _der(0xA0, _der_int(2))  # [0] EXPLICIT version v3
        + _der_int(secrets.randbits(63))
        + _SHA256_WITH_RSA
        + name
        + validity
        + name
        + spki,
    )
    signature = rsa.sign(tbs, priv_key, "SHA-256")
    return _der(_SEQUENCE, tbs + _SHA256_WITH_RSA + _der(_BIT_STRING, b"\x00" + signature))


def _pem(label: str, der: bytes) -> bytes:
    return (
        f"-----BEGIN {label}-----\n".encode()
        + base64.encodebytes(der)
        + f"-----END {label}-----\n".encode()
    )


def _generate_client_cert(paths: ClientCertPaths) -> None:
    pub_key, priv_key = rsa.newkeys(2048)
    common_name = f"atvremote/{socket.gethostname() or 'linux'}"

    paths.private_key.write_bytes(priv_key.save_pkcs1(format="PEM"))
    # Secure the private key file by restricting permissions to owner-only
    try:
        os.chmod(paths.private_key, 0o600)
    except OSError:
        pass  # Best effort if filesystem doesn't support permissions

    paths.certificate.write_bytes(_pem("CERTIFICATE", _build_certificate(pub_key, priv_key, common_name)))


def ensure_client_cert_exists(app_dir_name: str = "android-tv-remote") -> ClientCertPaths:
    """Ensure the TLS client certificate exists and return its paths.

    Keys are stored inside the Flatpak sandbox under XDG config. Replacing them
    means every TV has to be paired again.
    """
    paths = get_client_cert_paths(app_dir_name)
    paths.private_key.parent.mkdir(parents=True, exist_ok=True)

    if paths.private_key.exists() and paths.certificate.exists():
        # Ensure private key is secure (0o600)
        try:
            current_mode = paths.private_key.stat().st_mode
            if (current_mode & 0o777) != 0o600:
                paths.private_key.chmod(0o600)
        except OSError:
            pass  # Best effort
        return paths

    _generate_client_cert(paths)
    return paths


def load_client_public_key(paths: ClientCertPaths) -> rsa.PublicKey:
    """Return the public half of the stored client key (needed for the pairing hash)."""
    priv = rsa.PrivateKey.load_pkcs1(paths.private_key.read_bytes(), format="PEM")
    return rsa.PublicKey(priv.n, priv.e)
