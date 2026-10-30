"""Exact OAuth return addresses pinned by host-owned Workbench configuration.

Request headers and agent inputs cannot choose the public origin. The server
must use the same configured origin when constructing its callback URLs.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import SplitResult, urlsplit

from vwb.config import host_setting

from .store import IntegrationError

_ORIGIN_ERROR = (
    "VWB_UI_PUBLIC_URL must be an HTTPS origin with a public hostname and optional port, "
    "without credentials, path, query or fragment."
)
_CALLBACK_ERROR = (
    "Use the exact registered Workbench callback: HTTP on loopback, or the configured "
    "VWB_UI_PUBLIC_URL HTTPS origin with the exact callback path."
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LOOPBACK_AUTHORITY = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?\Z", re.I)
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".home.arpa")


def _parse(value: str, message: str) -> SplitResult:
    # urllib tolerates whitespace, empty delimiters and encoded authorities that
    # browsers can interpret differently. Reject these before parsing.
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            or any(char in value for char in "\\%@?#")):
        raise IntegrationError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise IntegrationError(message) from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.netloc.endswith(":") or (port is not None and port < 1)):
        raise IntegrationError(message)
    return parsed


def public_origin(value: str | None = None) -> str:
    """Validate and canonicalize an explicit origin, or the host environment.

    An unset/empty value disables public callbacks. DNS is not consulted: this
    validates hostname syntax and excludes local names and IP literals, while
    deployment and access controls remain the host server's responsibility.
    """
    value = host_setting("VWB_UI_PUBLIC_URL") if value is None else value
    if value == "":
        return ""
    parsed = _parse(value, _ORIGIN_ERROR)
    host = parsed.hostname
    if parsed.scheme != "https" or parsed.path or len(host) > 253:
        raise IntegrationError(_ORIGIN_ERROR)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise IntegrationError(_ORIGIN_ERROR)
    labels = host.split(".")
    if (len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels)
            or host.endswith(_LOCAL_SUFFIXES)
            # Browsers also accept abbreviated, octal and hex IPv4 spellings.
            or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", labels[-1])):
        raise IntegrationError(_ORIGIN_ERROR)
    port = parsed.port
    return f"https://{host}" + (f":{port}" if port is not None and port != 443 else "")


def validate_callback(value: str, path: str, *, require_loopback_port: bool = False) -> str:
    """Return the unchanged exact callback, accepting only pinned HTTPS or loopback.

    Public callbacks must already use the canonical origin. Do not normalize
    their strings: OAuth registration and pending state bind the exact URL.
    """
    origin = public_origin()
    parsed = _parse(value, _CALLBACK_ERROR)
    if parsed.path != path:
        raise IntegrationError(_CALLBACK_ERROR)
    if origin and value == origin + path:
        return value
    if (parsed.scheme == "http" and _LOOPBACK_AUTHORITY.fullmatch(parsed.netloc)
            and (not require_loopback_port or parsed.port is not None)):
        return value
    raise IntegrationError(_CALLBACK_ERROR)
