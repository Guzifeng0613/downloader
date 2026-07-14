"""Privacy-safe diagnostics shared by the downloader subsystems."""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
SENSITIVE_QUERY_NAMES = {
    "access_token",
    "auth",
    "authorization",
    "credential",
    "expires",
    "key",
    "policy",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-security-token",
}

_HEADER_PATTERN = re.compile(
    r"(?im)^(authorization|cookie|proxy-authorization|set-cookie|x-api-key|x-auth-token)\s*:\s*.*$"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def redact_url(value: str) -> str:
    """Remove credentials and sensitive query values while preserving URL shape."""
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return "[REDACTED_URL]"
    if parts.scheme not in {"http", "https"}:
        return text
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        hidden = key.lower() in SENSITIVE_QUERY_NAMES or any(
            marker in key.lower() for marker in ("token", "sign", "auth", "credential")
        )
        query.append((key, "[REDACTED]" if hidden else item))
    return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Return headers suitable for UI/export. Raw credential values never survive."""
    result: dict[str, str] = {}
    for name, value in (headers or {}).items():
        key = str(name)
        if key.lower() in SENSITIVE_HEADER_NAMES:
            result[key] = "[REDACTED]"
        else:
            result[key] = redact_text(str(value))
    return result


def redact_text(value: str) -> str:
    text = _HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: [REDACTED]", str(value or ""))
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    return _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)


def redact_value(value: Any) -> Any:
    """Recursively sanitize values before diagnostic serialization."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in SENSITIVE_HEADER_NAMES or any(
                marker in name.lower() for marker in ("cookie", "authorization", "secret")
            ):
                result[name] = "[REDACTED]"
            elif name.lower() in {"url", "webpage_url", "source_url"}:
                result[name] = redact_url(str(item))
            else:
                result[name] = redact_value(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def export_diagnostic_bundle(
    target: str | Path,
    *,
    components: Mapping[str, Any],
    attempts: Iterable[Any],
    summary: str,
    log: str = "",
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Write a local-only, redacted ZIP diagnostic bundle."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "telemetry_uploaded": False,
        "components": components,
        "attempts": list(attempts),
        "extra": dict(extra or {}),
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "diagnostics.json",
            json.dumps(redact_value(payload), ensure_ascii=False, indent=2),
        )
        archive.writestr("summary.txt", redact_text(summary))
        archive.writestr("log.txt", redact_text(log))
    return destination
