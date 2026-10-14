"""Downloading and security validation of public reference assets (currently only images are supported)."""
import asyncio
import ipaddress
import socket
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image

import config

_ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def validate_public_url(url: str) -> str:
    """Only accept public HTTP(S) URLs; reject localhost/intranet URLs and URLs carrying credentials."""
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Invalid reference image URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Reference images only support public http/https URLs")
    if parsed.username or parsed.password:
        raise ValueError("Reference image URL must not carry a username or password")
    host = parsed.hostname
    try:
        # Perform SSRF checks on both direct IPs and DNS resolution results; reject loopback, private, link-local, and similar addresses.
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = awaitable_getaddrinfo(host)
            addresses = {ipaddress.ip_address(x) for x in infos}
        except Exception as exc:
            raise ValueError(f"Cannot resolve reference image domain: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("Reference image URL points to an intranet or reserved address")
    return url


def awaitable_getaddrinfo(host: str) -> list[str]:
    """Synchronous DNS resolution wrapper; the caller invokes it via to_thread from the download coroutine."""
    return [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


async def _validate_url_async(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Invalid reference image URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Reference images only support public http/https URLs")
    if parsed.username or parsed.password:
        raise ValueError("Reference image URL must not carry a username or password")
    host = parsed.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = await asyncio.to_thread(awaitable_getaddrinfo, host)
            addresses = {ipaddress.ip_address(x) for x in resolved}
        except Exception as exc:
            raise ValueError(f"Cannot resolve reference image domain: {host}") from exc
    if not addresses or any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("Reference image URL points to an intranet or reserved address")
    return url


async def validate_reference_urls(urls: list[str]) -> list[str]:
    if len(urls) > config.REFERENCE_IMAGE_MAX_COUNT:
        raise ValueError(f"At most {config.REFERENCE_IMAGE_MAX_COUNT} reference images are allowed")
    normalized = []
    seen = set()
    for raw in urls:
        url = await _validate_url_async(raw)
        if url not in seen:
            normalized.append(url)
            seen.add(url)
    return normalized


async def _read_response_image(resp: aiohttp.ClientResponse) -> tuple[bytes, str]:
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > config.REFERENCE_IMAGE_MAX_BYTES:
        raise ValueError("Reference image exceeds the per-file size limit")
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > config.REFERENCE_IMAGE_MAX_BYTES:
            raise ValueError("Reference image exceeds the per-file size limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            fmt = image.format
    except Exception as exc:
        raise ValueError("Reference file is not a valid image") from exc
    if fmt not in _ALLOWED_IMAGE_FORMATS:
        raise ValueError("Reference images only support JPEG, PNG, WEBP")
    return data, _ALLOWED_IMAGE_FORMATS[fmt]


async def download_one_image(session: aiohttp.ClientSession, url: str, dest: Path) -> Path:
    current = await _validate_url_async(url)
    # Try the proxy first for public assets; some CDNs/image sites reject the proxy exit, so fall back to a direct connection.
    proxies = []
    if config.PROXY:
        proxies.append(config.PROXY)
    proxies.append(None)
    last_error = None
    for proxy in proxies:
        current = await _validate_url_async(url)
        for _ in range(5):
            try:
                async with session.get(
                    current,
                    allow_redirects=False,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=config.REFERENCE_DOWNLOAD_TIMEOUT),
                    headers={"User-Agent": "dola-pool-reference-fetch/1.0"},
                ) as resp:
                    if 300 <= resp.status < 400 and resp.headers.get("Location"):
                        current = await _validate_url_async(urljoin(current, resp.headers["Location"]))
                        continue
                    if resp.status != 200:
                        raise ValueError(f"Reference image download failed HTTP {resp.status}")
                    data, suffix = await _read_response_image(resp)
                    path = dest.with_suffix(suffix)
                    path.write_bytes(data)
                    return path
            except Exception as exc:
                last_error = exc
                break
    raise ValueError(str(last_error) if last_error else "Reference image download failed")


async def download_reference_images(urls: list[str], task_id: str) -> tuple[Path | None, list[str]]:
    """Download images to a temporary directory and return (directory, list of local paths). The caller is responsible for cleanup."""
    urls = await validate_reference_urls(urls)
    if not urls:
        return None, []
    root = Path(tempfile.mkdtemp(prefix=f"dola_ref_{task_id}_"))
    try:
        async with aiohttp.ClientSession() as session:
            paths = []
            for index, url in enumerate(urls):
                paths.append(str(await download_one_image(session, url, root / f"image_{index}")))
        return root, paths
    except Exception:
        for child in root.glob("*"):
            child.unlink(missing_ok=True)
        root.rmdir()
        raise


def write_skill_markdown(skill_markdown: str, task_id: str) -> Path:
    """Write the skill markdown to a temp skill.md and return its path.

    The caller is responsible for deleting the parent directory afterwards.
    """
    root = Path(tempfile.mkdtemp(prefix=f"dola_skill_{task_id}_"))
    path = root / "skill.md"
    path.write_text(skill_markdown, encoding="utf-8")
    return path


def resolve_skill_markdown(skill_markdown: str | None) -> str | None:
    """Return the skill markdown to use: the request value, or the configured default file."""
    if skill_markdown:
        return skill_markdown
    if config.SKILL_ENABLED:
        default = Path(config.SKILL_FILE)
        if default.exists():
            return default.read_text(encoding="utf-8")
    return None