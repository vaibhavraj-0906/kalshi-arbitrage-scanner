"""Request signing, and the guard that keeps karb off every production host."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from karb.core.clock import FakeClock
from karb.exchange.client import KalshiClient
from karb.trading.auth import (
    DEMO_BASE_URL,
    KEY_FILE_ENV,
    KEY_ID_ENV,
    PRODUCTION_HOSTS,
    Credentials,
    CredentialsError,
    signing_message,
)
from tests.support import NOW


def pem(key: Ed25519PrivateKey | rsa.RSAPrivateKey, fmt: serialization.PrivateFormat) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())


def test_the_signed_message_drops_the_query_string() -> None:
    assert (
        signing_message(1703123456789, "get", "/trade-api/v2/portfolio/orders?limit=5")
        == "1703123456789GET/trade-api/v2/portfolio/orders"
    )


def test_ed25519_signatures_verify() -> None:
    key = Ed25519PrivateKey.generate()
    credentials = Credentials.from_pem("kid", pem(key, serialization.PrivateFormat.PKCS8))
    headers = credentials.headers(1700000000000, "POST", "/trade-api/v2/portfolio/events/orders")
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    message = b"1700000000000POST/trade-api/v2/portfolio/events/orders"
    key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), message)
    assert credentials.algorithm == "Ed25519"


@pytest.mark.parametrize(
    "fmt", [serialization.PrivateFormat.TraditionalOpenSSL, serialization.PrivateFormat.PKCS8]
)
def test_rsa_keys_sign_with_pss(fmt: serialization.PrivateFormat) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credentials = Credentials.from_pem("kid", pem(key, fmt))
    signature = base64.b64decode(credentials.sign("1GET/trade-api/v2/portfolio/balance"))
    key.public_key().verify(
        signature,
        b"1GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    assert credentials.algorithm == "RSA-PSS"


def test_credentials_come_from_the_environment(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match=KEY_ID_ENV):
        Credentials.from_env({})
    with pytest.raises(CredentialsError, match="cannot read"):
        Credentials.from_env({KEY_ID_ENV: "kid", KEY_FILE_ENV: str(tmp_path / "missing.pem")})
    key_file = tmp_path / "demo.pem"
    key_file.write_bytes(pem(Ed25519PrivateKey.generate(), serialization.PrivateFormat.PKCS8))
    loaded = Credentials.from_env({KEY_ID_ENV: " kid ", KEY_FILE_ENV: str(key_file)})
    assert loaded.key_id == "kid"


def test_bad_keys_are_refused() -> None:
    with pytest.raises(CredentialsError):
        Credentials.from_pem("kid", b"not a key")
    encrypted = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    )
    with pytest.raises(CredentialsError, match="unencrypted"):
        Credentials.from_pem("kid", encrypted)
    with pytest.raises(CredentialsError, match="empty"):
        Credentials.from_pem(
            " ", pem(Ed25519PrivateKey.generate(), serialization.PrivateFormat.PKCS8)
        )


def test_repr_never_shows_the_key() -> None:
    credentials = Credentials("0123456789abcdef", Ed25519PrivateKey.generate())
    text = repr(credentials)
    assert "01234567" in text and "89abcdef" not in text and "PRIVATE" not in text


def signed_client(base_url: str, seen: list[httpx.Request], **options: object) -> KalshiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"balance_dollars": "1.00"})

    return KalshiClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(NOW),
        credentials=Credentials("kid", Ed25519PrivateKey.generate()),
        **options,  # type: ignore[arg-type]
    )


async def test_portfolio_requests_to_the_demo_exchange_are_signed() -> None:
    seen: list[httpx.Request] = []
    async with signed_client(DEMO_BASE_URL, seen) as client:
        await client.get("/portfolio/balance", [("limit", 5)], auth=True)
        await client.get("/markets")  # public data travels unsigned
    signed, public = seen
    assert signed.headers["KALSHI-ACCESS-TIMESTAMP"] == str(int(NOW.timestamp() * 1000))
    assert "KALSHI-ACCESS-SIGNATURE" in signed.headers
    assert "KALSHI-ACCESS-KEY" not in public.headers


@pytest.mark.parametrize("host", sorted(PRODUCTION_HOSTS))
async def test_no_production_host_is_ever_signed_for(host: str) -> None:
    seen: list[httpx.Request] = []
    # Even a client told to sign for the host refuses: real money is out of scope.
    async with signed_client(
        f"https://{host}/trade-api/v2", seen, sign_hosts=frozenset({host})
    ) as client:
        with pytest.raises(CredentialsError, match="demo exchange only"):
            await client.get("/portfolio/balance", auth=True)
        with pytest.raises(CredentialsError):
            await client.post("/portfolio/events/orders", {"ticker": "X"})
    assert seen == []  # nothing left the machine


async def test_unknown_hosts_are_refused_and_unsigned_clients_cannot_sign() -> None:
    seen: list[httpx.Request] = []
    async with signed_client("https://example.org/trade-api/v2", seen) as client:
        with pytest.raises(CredentialsError):
            await client.get("/portfolio/balance", auth=True)
    async with KalshiClient(base_url=DEMO_BASE_URL) as anonymous:
        with pytest.raises(CredentialsError, match="no credentials"):
            await anonymous.get("/portfolio/balance", auth=True)
    assert seen == []
