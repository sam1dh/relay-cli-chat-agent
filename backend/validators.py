"""Input validation for anything that will become a CLI argument."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from . import config

URL_RE = re.compile(r"(?<![@\w.-])((?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?::\d{2,5})?(?:/[^\s<>\"'`]*)?)", re.I)
LOCAL_URL_RE = re.compile(r"\b(https?://(?:localhost|127\.0\.0\.1)(?::\d{2,5})?(?:/[^\s<>\"'`]*)?)", re.I)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")   # RFC 6052 well-known NAT64 prefix


def _effective_ip(ip):
    """NAT64 / IPv4-mapped IPv6 addresses embed a real IPv4 address: judge that one."""
    if ip.version == 6:
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


class ValidationError(ValueError):
    pass


def extract_url(text: str) -> str | None:
    """Find the first URL-looking token in free text."""
    scheme = re.search(r"\b([a-z][a-z0-9+.-]{1,15})://[^\s<>\"'`]+", text, re.I)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        return scheme.group(0).rstrip(".,;:!?)]}")   # keep it so validation rejects it explicitly
    if config.ALLOW_PRIVATE_HOSTS:
        m = LOCAL_URL_RE.search(text)
        if m:
            return m.group(1).rstrip(".,;:!?)]}")
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(1).rstrip(".,;:!?)]}")
    if "@" in url.split("/")[0]:      # looks like an email address, not a URL
        return None
    return url


def validate_url(raw: str) -> str:
    """Return a normalised, safe http(s) URL or raise ValidationError."""
    if not raw or not isinstance(raw, str):
        raise ValidationError("No URL was provided.")
    url = raw.strip()
    if len(url) > 2048:
        raise ValidationError("URL is too long.")
    if any(c in url for c in "\x00\r\n\t "):
        raise ValidationError("URL contains whitespace or control characters.")
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            raise ValidationError(f"Only http and https URLs are allowed (got '{url.split('://')[0]}').")
        url = "https://" + url
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https"):
        raise ValidationError("Only http and https URLs are allowed.")
    if not p.hostname:
        raise ValidationError("The URL has no host name.")
    if p.username or p.password:
        raise ValidationError("URLs containing credentials are not allowed.")
    if url.startswith("-"):
        raise ValidationError("Invalid URL.")
    if not config.ALLOW_PRIVATE_HOSTS:
        _ensure_public(p.hostname)
    return url


def _ensure_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValidationError(f"The domain '{host}' could not be resolved (DNS lookup failed).") from None
    for info in infos:
        ip = _effective_ip(ipaddress.ip_address(info[4][0].split("%")[0]))
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise ValidationError(f"'{host}' points to a private/internal address ({ip}); crawling it is blocked.")


def clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))
