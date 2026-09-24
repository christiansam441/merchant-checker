import asyncio
import ipaddress
import ssl

import pytest

import httpx

from app.fetcher import (
    BlockedURL,
    PinnedNetworkBackend,
    PinnedTransport,
    REQUEST_HEADERS,
    ResolvedURL,
    TargetSiteBlocked,
    USER_AGENT,
    fetch_page,
    validate_public_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://192.168.1.10",
        "http://10.0.0.5",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_internal_addresses_are_blocked(url: str) -> None:
    with pytest.raises(BlockedURL, match=r"Local|internal|private|link-local"):
        asyncio.run(validate_public_url(url))


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)"])
def test_non_http_schemes_are_blocked(url: str) -> None:
    with pytest.raises(BlockedURL, match="Only HTTP and HTTPS"):
        asyncio.run(validate_public_url(url))


def test_connection_uses_the_ip_that_was_pinned_after_validation() -> None:
    class RecordingBackend:
        def __init__(self) -> None:
            self.connected_host = None

        async def connect_tcp(self, host, port, **kwargs):
            self.connected_host = host
            return object()

        async def sleep(self, seconds):
            return None

    recording_backend = RecordingBackend()
    backend = PinnedNetworkBackend(recording_backend)
    backend.pin("merchant.example", ipaddress.ip_address("93.184.216.34"))

    asyncio.run(backend.connect_tcp("merchant.example", 443))

    assert recording_backend.connected_host == "93.184.216.34"


def test_pinned_tls_verifies_certificate_against_original_hostname() -> None:
    class FakeTLSStream:
        def __init__(self) -> None:
            self.server_hostname = None
            self.ssl_context = None
            self.response_sent = False

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            self.ssl_context = ssl_context
            self.server_hostname = server_hostname
            return self

        async def write(self, buffer, timeout=None):
            return None

        async def read(self, max_bytes, timeout=None):
            if self.response_sent:
                return b""
            self.response_sent = True
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"

        async def aclose(self):
            return None

        def get_extra_info(self, info):
            return None

    class FakeBackend:
        def __init__(self, stream) -> None:
            self.stream = stream

        async def connect_tcp(self, host, port, **kwargs):
            return self.stream

        async def sleep(self, seconds):
            return None

    async def make_request():
        stream = FakeTLSStream()
        backend = PinnedNetworkBackend(FakeBackend(stream))
        backend.pin("merchant.example", ipaddress.ip_address("93.184.216.34"))
        async with httpx.AsyncClient(transport=PinnedTransport(backend)) as client:
            response = await client.get("https://merchant.example/")
        return response, stream

    response, stream = asyncio.run(make_request())

    assert response.status_code == 200
    assert stream.server_hostname == "merchant.example"
    assert stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert stream.ssl_context.check_hostname is True


@pytest.mark.parametrize(
    ("status_code", "description"),
    [
        (403, "refused automated requests"),
        (429, "rate limited automated requests"),
        (503, "temporarily unavailable"),
    ],
)
def test_target_site_block_errors_are_clear(status_code: int, description: str) -> None:
    error = TargetSiteBlocked(status_code)

    assert f"HTTP {status_code}" in str(error)
    assert description in str(error)
    assert "does not attempt to bypass bot protection" in str(error)


@pytest.mark.parametrize("status_code", [403, 429, 503])
def test_fetcher_classifies_target_site_block_responses(monkeypatch, status_code: int) -> None:
    async def fake_resolve(url: str) -> ResolvedURL:
        return ResolvedURL(httpx.URL(url), ipaddress.ip_address("93.184.216.34"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    monkeypatch.setattr("app.fetcher.resolve_public_url", fake_resolve)
    monkeypatch.setattr(
        "app.fetcher.PinnedTransport",
        lambda backend: httpx.MockTransport(handler),
    )

    with pytest.raises(TargetSiteBlocked, match=f"HTTP {status_code}"):
        asyncio.run(fetch_page("https://merchant.example/"))


def test_request_headers_are_polite_and_identify_the_scanner() -> None:
    assert REQUEST_HEADERS["User-Agent"] == USER_AGENT
    assert "MerchantChecker" in REQUEST_HEADERS["User-Agent"]
    assert REQUEST_HEADERS["Accept"].startswith("text/html")
    assert REQUEST_HEADERS["Accept-Language"] == "en-US,en;q=0.8"
