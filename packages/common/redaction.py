from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTION_MARKER = "[redacted]"

SENSITIVE_KEY_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|session[_-]?key|signature|"
    r"accountingstoragepass|storagepass|authorization|proxy[_-]?authorization|auth[_-]?header|^auth$)",
    re.IGNORECASE,
)
AUTH_NAMESPACE_KEY_RE = re.compile(r"^auth$", re.IGNORECASE)
AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?P<key_quote>[\"']?)\b"
    r"(?P<key>(?:proxy[-_.]?)?authorization|auth[-_.]?header|auth)\b"
    r"(?P=key_quote)(?P<separator>\s*[:=]\s*)(?P<value_quote>[\"']?))"
    r"(?:(?P<scheme>Bearer|Basic)\s+)?"
    r"(?P<secret>[^\"'\s,;&}]+)"
    r"(?P=value_quote)",
    re.IGNORECASE,
)
AUTHORIZATION_SCHEME_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<scheme>Bearer|Basic)\b(?P<separator>\s+)"
    r"(?P<secret>[^\"'\s,;&<>{}\[\]()]+)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"\b([A-Za-z0-9_.-]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|"
    r"access[_-]?key|session[_-]?key|signature|accountingstoragepass|storagepass)"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)([^\s,;&]+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:(?:[A-Za-z][A-Za-z0-9+.-]*)://|//)[^\s\"'<>]+", re.IGNORECASE)


def redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            redacted[key_text] = (
                REDACTION_MARKER if _should_redact_mapping_value(key_text, nested) else redact_payload(nested)
            )
        return redacted
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
    redacted = AUTHORIZATION_ASSIGNMENT_RE.sub(_redact_authorization_assignment, redacted)
    redacted = AUTHORIZATION_SCHEME_RE.sub(_redact_authorization_scheme, redacted)
    return SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTION_MARKER}", redacted)


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def _should_redact_mapping_value(key: str, value: Any) -> bool:
    if AUTH_NAMESPACE_KEY_RE.fullmatch(key) and isinstance(value, Mapping):
        return False
    return is_sensitive_key(key)


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return REDACTION_MARKER
    if not parsed.netloc:
        return SENSITIVE_ASSIGNMENT_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTION_MARKER}",
            value.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0],
        )
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _redact_authorization_assignment(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    value = f"{scheme} {REDACTION_MARKER}" if scheme else REDACTION_MARKER
    return f"{match.group('prefix')}{value}{match.group('value_quote')}"


def _redact_authorization_scheme(match: re.Match[str]) -> str:
    secret = match.group("secret")
    trimmed = secret.rstrip(".,:!?")
    suffix = secret[len(trimmed) :]
    if not trimmed:
        return match.group(0)
    return f"{match.group('scheme')}{match.group('separator')}{REDACTION_MARKER}{suffix}"
