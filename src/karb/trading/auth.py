"""Signed requests to Kalshi's demo exchange (docs/decisions/ADR-0010).

Kalshi authenticates every portfolio request with three headers:

    KALSHI-ACCESS-KEY        the key id
    KALSHI-ACCESS-TIMESTAMP  milliseconds since the epoch
    KALSHI-ACCESS-SIGNATURE  base64(sign(timestamp + METHOD + path))

The signed path is the full path as sent, ``/trade-api/v2`` prefix included, without the query
string. Ed25519 keys sign the message directly; RSA keys use PSS over SHA-256 with an MGF1-SHA-256
mask and a 32-byte salt.

karb trades on the demo exchange only, with mock funds. The keys come from two environment
variables -- never from the command line, never written anywhere -- and a signer refuses to sign
for any production host, whatever it is configured with.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

__all__ = [
    "DEMO_BASE_URL",
    "DEMO_HOSTS",
    "KEY_FILE_ENV",
    "KEY_ID_ENV",
    "PRODUCTION_HOSTS",
    "Credentials",
    "CredentialsError",
    "signing_message",
]

DEMO_BASE_URL: Final = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_HOSTS: Final = frozenset({"external-api.demo.kalshi.co", "demo-api.kalshi.co"})
PRODUCTION_HOSTS: Final = frozenset(
    {
        "external-api.kalshi.com",
        "api.elections.kalshi.com",
        "trading-api.kalshi.com",
        "api.kalshi.com",
    }
)
"""Hosts that move real money. Nothing in karb signs a request to them."""

KEY_ID_ENV: Final = "KALSHI_DEMO_KEY_ID"
KEY_FILE_ENV: Final = "KALSHI_DEMO_KEY_FILE"


class CredentialsError(RuntimeError):
    """Missing, unreadable or unsupported credentials, or a request karb refuses to sign."""


def signing_message(timestamp_ms: int, method: str, path: str) -> str:
    """What Kalshi expects signed: timestamp, upper-case method and path, query stripped."""
    return f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}"


@dataclass(frozen=True, slots=True)
class Credentials:
    key_id: str
    _key: Ed25519PrivateKey | RSAPrivateKey = field(repr=False)

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes) -> Credentials:
        if not key_id.strip():
            raise CredentialsError("the API key id is empty")
        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except (ValueError, TypeError) as exc:
            raise CredentialsError(
                "the private key is not an unencrypted PEM key (Ed25519 or RSA)"
            ) from exc
        if not isinstance(key, Ed25519PrivateKey | RSAPrivateKey):
            raise CredentialsError(f"unsupported key type {type(key).__name__}")
        return cls(key_id.strip(), key)

    @classmethod
    def from_file(cls, key_id: str, path: Path) -> Credentials:
        try:
            pem = path.read_bytes()
        except OSError as exc:
            raise CredentialsError(f"cannot read the private key file {path}: {exc}") from exc
        return cls.from_pem(key_id, pem)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Credentials:
        env = os.environ if environ is None else environ
        key_id = env.get(KEY_ID_ENV, "")
        key_file = env.get(KEY_FILE_ENV, "")
        if not key_id or not key_file:
            raise CredentialsError(
                f"set {KEY_ID_ENV} and {KEY_FILE_ENV} to your Kalshi demo API key id and the "
                "path of its private key file (docs/guide.md, section 5)"
            )
        return cls.from_file(key_id, Path(key_file))

    @property
    def algorithm(self) -> str:
        return "Ed25519" if isinstance(self._key, Ed25519PrivateKey) else "RSA-PSS"

    def public_key(self) -> Ed25519PublicKey | RSAPublicKey:
        """What the exchange verifies signatures with. Safe to share; the private key is not."""
        return self._key.public_key()

    def sign(self, message: str) -> str:
        data = message.encode("utf-8")
        if isinstance(self._key, Ed25519PrivateKey):
            signature = self._key.sign(data)
        else:
            signature = self._key.sign(
                data,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256(),
            )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": self.sign(signing_message(timestamp_ms, method, path)),
        }

    def __repr__(self) -> str:
        return f"Credentials(key_id={self.key_id[:8]!r}..., {self.algorithm})"
