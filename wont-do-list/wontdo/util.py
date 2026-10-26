"""Small helpers: ids, safe file writes, URL checks and calendar line folding."""

from __future__ import annotations

import datetime as _dt
import ipaddress
import os
import re
import secrets
import tempfile
from typing import Optional
from urllib.parse import urlparse

SID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
TID_RE = re.compile(r"^t\d{1,4}$")


def today_local() -> _dt.date:
    return _dt.date.today()


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def new_sid(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    return "%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))


def clip(text: object, limit: int) -> str:
    """Collapse whitespace and cut to `limit` characters, adding an ellipsis."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def norm_key(text: str) -> str:
    """A comparison key: lowercase letters and digits only, single spaces."""
    return re.sub(r"[\W_]+", " ", text.lower(), flags=re.UNICODE).strip()


def atomic_write_text(path: str, text: str) -> None:
    """Write a file so readers never see a half-written one."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_host(host: str) -> bool:
    host = host.strip().lower().strip("[]")
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback or addr.is_unspecified


def is_unspecified_host(host: str) -> bool:
    """True for 0.0.0.0 / :: - 'every network interface' when you bind to it."""
    host = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def binds_to_this_machine_only(host: str) -> bool:
    """True when a server bound to `host` can only be reached from this computer."""
    return is_loopback_host(host) and not is_unspecified_host(host)


def is_local_url(url: str) -> bool:
    """True when the URL points at this machine (loopback only, not the LAN)."""
    return is_loopback_host(url_host(url))


def ics_fold(line: str) -> str:
    """Fold a calendar line to 75 octets (RFC 5545), splitting only between characters."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts = []
    current = b""
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current = encoded
            limit = 74  # continuation lines start with one space
        else:
            current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )
