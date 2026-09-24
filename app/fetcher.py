import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin

import httpcore
import httpx
import certifi
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream, SOCKET_OPTION


USER_AGENT = "MerchantChecker/0.1 (public-web merchant technology scanner)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    "Accept-Language": "en-US,en;q=0.8",
}


class FetchError(RuntimeError):
    pass


class BlockedURL(FetchError):
    pass


class TargetSiteBlocked(FetchError):
    def __init__(self, status_code: int) -> None:
        descriptions = {
            403: "refused automated requests",
            429: "rate limited automated requests",
            503: "is temporarily unavailable for automated requests",
        }
        description = descriptions[status_code]
        super().__init__(
            f"This site {description} (HTTP {status_code}). This tool identifies itself "
            "honestly, reads public pages only, and does not attempt to bypass bot "
            "protection. Try a different site or check the page manually."
        )
        self.status_code = status_code


@dataclass(frozen=True)
class FetchedPage:
    url: str
    html: str
    headers: dict[str, str]
    cookies: dict[str, str]


@dataclass(frozen=True)
class ResolvedURL:
    url: httpx.URL
    address: ipaddress.IPv4Address | ipaddress.IPv6Address


class PinnedNetworkBackend(AsyncNetworkBackend):
    def __init__(self, backend: AsyncNetworkBackend | None = None) -> None:
        self._backend = backend or AutoBackend()
        self._pins: dict[str, str] = {}

    def pin(self, hostname: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        self._pins[hostname.rstrip(".").casefold()] = str(address)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        pinned_host = self._pins.get(host.rstrip(".").casefold())
        if pinned_host is None:
            raise BlockedURL(f"Connection attempted without a validated IP pin: {host}")
        return await self._backend.connect_tcp(
            pinned_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise BlockedURL("Unix socket connections are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        await self._stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, network_backend: PinnedNetworkBackend) -> None:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=network_backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


async def _resolve_hostname(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve hostname: {hostname}") from exc

    return {ipaddress.ip_address(record[4][0]) for record in records}


async def resolve_public_url(url: str) -> ResolvedURL:
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise BlockedURL("Enter a valid HTTP or HTTPS URL.") from exc

    if parsed.scheme not in {"http", "https"}:
        raise BlockedURL("Only HTTP and HTTPS URLs are allowed.")
    if not parsed.host:
        raise BlockedURL("The URL must include a hostname.")
    if parsed.username or parsed.password:
        raise BlockedURL("URLs containing credentials are not allowed.")

    hostname = parsed.host.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise BlockedURL("Local and internal addresses are not allowed.")

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await _resolve_hostname(hostname, port)

    if not addresses or any(not address.is_global for address in addresses):
        raise BlockedURL("Local, private, link-local, and internal addresses are not allowed.")

    address = sorted(addresses, key=lambda item: (item.version, item.packed))[0]
    return ResolvedURL(url=parsed, address=address)


async def validate_public_url(url: str) -> httpx.URL:
    return (await resolve_public_url(url)).url


async def fetch_page(url: str) -> FetchedPage:
    current_url = url

    pinning_backend = PinnedNetworkBackend()
    transport = PinnedTransport(pinning_backend)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            headers=REQUEST_HEADERS,
            trust_env=False,
            transport=transport,
        ) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                resolved = await resolve_public_url(current_url)
                safe_url = resolved.url
                pinning_backend.pin(safe_url.host, resolved.address)

                async with client.stream("GET", safe_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("The site returned a redirect without a destination.")
                        if redirect_count == MAX_REDIRECTS:
                            raise FetchError(f"The site exceeded the {MAX_REDIRECTS}-redirect limit.")
                        current_url = urljoin(str(safe_url), location)
                        continue

                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise FetchError(
                                f"The page exceeded the {MAX_RESPONSE_BYTES // (1024 * 1024)} MB response limit."
                            )

                    encoding = response.encoding or "utf-8"
                    html = bytes(content).decode(encoding, errors="replace")
                    return FetchedPage(
                        url=str(response.url),
                        html=html,
                        headers=dict(response.headers),
                        cookies={name: value for name, value in client.cookies.items()},
                    )
    except BlockedURL:
        raise
    except FetchError:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {403, 429, 503}:
            raise TargetSiteBlocked(exc.response.status_code) from exc
        raise FetchError(f"The site returned HTTP {exc.response.status_code}.") from exc
    except httpx.TimeoutException as exc:
        raise FetchError("The site did not respond before the timeout.") from exc
    except httpx.RequestError as exc:
        raise FetchError(f"The site could not be reached: {exc}") from exc
    except httpcore.TimeoutException as exc:
        raise FetchError("The site did not respond before the timeout.") from exc
    except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
        raise FetchError(f"The site could not be reached: {exc}") from exc

    raise FetchError("The page could not be fetched.")
