from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

REDACTION_MARKER = "[redacted]"
BOOLEAN_CONFIGURED_FIELD_ALLOWLIST = frozenset({"database_url_configured"})

SENSITIVE_KEY_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|session[_-]?key|signature|"
    r"accountingstoragepass|storagepass|authorization|proxy[_-]?authorization|auth[_-]?header|^auth$)",
    re.IGNORECASE,
)
AUTH_NAMESPACE_KEY_RE = re.compile(r"^auth$", re.IGNORECASE)
AUTH_NAMESPACE_SAFE_SCALAR_KEYS = frozenset(
    {
        "accepted",
        "audience",
        "audiences",
        "client_id",
        "client_name",
        "dependency",
        "dependency_name",
        "denied_action",
        "execution_mode",
        "idp",
        "idp_id",
        "issuer",
        "issuer_url",
        "producer_checksum",
        "producer_receipt_id",
        "proof_type",
        "provider",
        "provider_id",
        "provider_name",
        "receipt_id",
        "run_id",
        "schema",
        "source",
        "source_ref",
        "status",
        "subject",
        "surface",
        "target_environment",
        "tenant_id",
    }
)
AUTH_NAMESPACE_SAFE_SEQUENCE_KEYS = frozenset(
    {
        "actions",
        "allowed_actions",
        "allowed_coverage",
        "artifact_refs",
        "audiences",
        "denied_actions",
        "denied_coverage",
        "missing_allowed_actions",
        "missing_denied_actions",
        "permissions",
        "roles",
        "scopes",
    }
)
AUTH_NAMESPACE_ARBITRARY_EVIDENCE_KEYS = frozenset({"role_mapping", "role_mappings"})
AUTH_NAMESPACE_METADATA_CONTAINER_KEYS = frozenset({"provider", "provider_metadata", "idp_metadata"})
AUTH_NAMESPACE_ROLE_MAPPING_SEQUENCE_KEYS = frozenset(
    {"actions", "allowed_actions", "denied_actions", "permissions", "roles", "scopes"}
)
AUTH_NAMESPACE_OPAQUE_SCALAR_KEYS = frozenset(
    {"data", "payload", "raw", "raw_payload", "raw_value", "response", "text", "value"}
)
AUTH_NAMESPACE_CONTEXT_ROOT = "root"
AUTH_NAMESPACE_CONTEXT_METADATA = "metadata"
AUTH_NAMESPACE_CONTEXT_OPAQUE = "opaque"
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
LIBPQ_PASSWORD_PREFIX_RE = re.compile(r"(?<!\S)password\s*=\s*", re.IGNORECASE)
URL_RE = re.compile(r"(?:(?:[A-Za-z][A-Za-z0-9+.-]*)://|//)[^\s\"'<>]+", re.IGNORECASE)
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def redact_payload(value: Any) -> Any:
    return _redact_payload(value, auth_context=None, auth_scalar_allowed=False)


def _redact_payload(value: Any, *, auth_context: str | None, auth_scalar_allowed: bool) -> Any:
    if isinstance(value, Mapping):
        current_auth_context = auth_context or (
            AUTH_NAMESPACE_CONTEXT_ROOT if _is_auth_live_proof_mapping(value) else None
        )
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            nested_auth_context = _auth_child_context(
                key_text,
                nested,
                parent_context=current_auth_context,
                parent_mapping=value,
            )
            nested_scalar_allowed = _auth_namespace_allows_direct_scalars(
                key_text,
                nested,
                auth_context=nested_auth_context,
            )
            redacted[key_text] = (
                REDACTION_MARKER
                if _should_redact_mapping_value(key_text, nested)
                or _should_redact_auth_namespace_value(
                    key_text,
                    nested,
                    auth_context=nested_auth_context,
                    scalar_allowed=nested_scalar_allowed,
                )
                else _redact_auth_role_mapping(nested)
                if nested_auth_context == AUTH_NAMESPACE_CONTEXT_ROOT
                and key_lower in AUTH_NAMESPACE_ARBITRARY_EVIDENCE_KEYS
                and isinstance(nested, Mapping)
                else _redact_auth_opaque_value(nested)
                if nested_auth_context == AUTH_NAMESPACE_CONTEXT_OPAQUE
                and isinstance(nested, Mapping | Sequence)
                and not isinstance(nested, str | bytes | bytearray)
                else _redact_payload(
                    nested,
                    auth_context=nested_auth_context,
                    auth_scalar_allowed=nested_scalar_allowed,
                )
            )
        return redacted
    if isinstance(value, tuple):
        return tuple(_redact_auth_sequence_item(item, auth_context, auth_scalar_allowed) for item in value)
    if isinstance(value, list):
        return [_redact_auth_sequence_item(item, auth_context, auth_scalar_allowed) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_auth_sequence_item(item, auth_context, auth_scalar_allowed) for item in value]
    if auth_context and not auth_scalar_allowed:
        return REDACTION_MARKER
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = SURROGATE_RE.sub("\ufffd", value)
    redacted = URL_RE.sub(lambda match: _redact_url(match.group(0)), redacted)
    redacted = AUTHORIZATION_ASSIGNMENT_RE.sub(_redact_authorization_assignment, redacted)
    redacted = AUTHORIZATION_SCHEME_RE.sub(_redact_authorization_scheme, redacted)
    return _redact_sensitive_assignments(redacted)


def redact_database_dsn(value: str, dsn: str | None) -> str:
    """Redact free-form text plus verbatim and driver-decoded DSN passwords."""
    redacted = SURROGATE_RE.sub("\ufffd", value)
    normalized_dsn = SURROGATE_RE.sub("\ufffd", dsn or "")
    if normalized_dsn:
        secrets = {normalized_dsn}
        for password_raw in _dsn_password_candidates(normalized_dsn):
            try:
                password_decoded = unquote(password_raw)
            except Exception:
                password_decoded = password_raw
            secrets.update(secret for secret in (password_raw, password_decoded) if secret)
        redacted = _replace_literals_once(redacted, secrets)
    return redact_text(redacted)


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def _should_redact_mapping_value(key: str, value: Any) -> bool:
    if AUTH_NAMESPACE_KEY_RE.fullmatch(key) and isinstance(value, Mapping):
        return False
    if key.lower() in BOOLEAN_CONFIGURED_FIELD_ALLOWLIST and isinstance(value, bool):
        return False
    if key.lower() == "password_present" and isinstance(value, bool):
        return False
    return is_sensitive_key(key)


def _should_redact_auth_namespace_value(
    key: str,
    value: Any,
    *,
    auth_context: str | None,
    scalar_allowed: bool,
) -> bool:
    if not auth_context or isinstance(value, Mapping):
        return False
    key_lower = key.lower()
    if key_lower in AUTH_NAMESPACE_OPAQUE_SCALAR_KEYS:
        return True
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return False
    return not scalar_allowed


def _auth_namespace_allows_direct_scalars(key: str, value: Any, *, auth_context: str | None) -> bool:
    if not auth_context or auth_context == AUTH_NAMESPACE_CONTEXT_OPAQUE:
        return False
    key_lower = key.lower()
    if isinstance(value, Mapping):
        return False
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return auth_context == AUTH_NAMESPACE_CONTEXT_ROOT and key_lower in AUTH_NAMESPACE_SAFE_SEQUENCE_KEYS
    return key_lower in AUTH_NAMESPACE_SAFE_SCALAR_KEYS


def _redact_auth_sequence_item(item: Any, auth_context: str | None, auth_scalar_allowed: bool) -> Any:
    if auth_context and auth_scalar_allowed and not _is_scalar_value(item):
        return _redact_auth_opaque_value(item)
    return _redact_payload(
        item,
        auth_context=AUTH_NAMESPACE_CONTEXT_OPAQUE if auth_context else None,
        auth_scalar_allowed=auth_scalar_allowed and _is_scalar_value(item),
    )


def _auth_child_context(
    key: str,
    value: Any,
    *,
    parent_context: str | None,
    parent_mapping: Mapping[Any, Any],
) -> str | None:
    if not isinstance(value, Mapping):
        return parent_context
    key_lower = key.lower()
    if key_lower == "payload" and _is_auth_receipt_details_mapping(parent_mapping):
        return AUTH_NAMESPACE_CONTEXT_ROOT
    if parent_context == AUTH_NAMESPACE_CONTEXT_ROOT:
        if key_lower in AUTH_NAMESPACE_METADATA_CONTAINER_KEYS:
            return AUTH_NAMESPACE_CONTEXT_METADATA
        if key_lower in AUTH_NAMESPACE_ARBITRARY_EVIDENCE_KEYS:
            return AUTH_NAMESPACE_CONTEXT_ROOT
        return AUTH_NAMESPACE_CONTEXT_OPAQUE
    if parent_context == AUTH_NAMESPACE_CONTEXT_METADATA:
        return AUTH_NAMESPACE_CONTEXT_OPAQUE
    if parent_context == AUTH_NAMESPACE_CONTEXT_OPAQUE:
        return AUTH_NAMESPACE_CONTEXT_OPAQUE
    if _is_auth_live_proof_mapping(value):
        return AUTH_NAMESPACE_CONTEXT_ROOT
    if AUTH_NAMESPACE_KEY_RE.fullmatch(key):
        return AUTH_NAMESPACE_CONTEXT_ROOT
    return None


def _is_auth_live_proof_mapping(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    proof_type = value.get("proof_type", value.get("receipt_type"))
    return proof_type == "auth" and value.get("surface") == "live_backend_auth"


def _is_auth_receipt_details_mapping(value: Mapping[Any, Any]) -> bool:
    return value.get("surface") == "auth"


def _redact_auth_role_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(role): _redact_auth_role_mapping_value(mapped) for role, mapped in value.items()}


def _redact_auth_role_mapping_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if _should_redact_mapping_value(key_text, nested):
                redacted[key_text] = REDACTION_MARKER
            elif key_lower in AUTH_NAMESPACE_ROLE_MAPPING_SEQUENCE_KEYS and _is_sequence_value(nested):
                redacted[key_text] = _redact_auth_allowed_scalar_or_sequence(nested)
            else:
                redacted[key_text] = _redact_auth_opaque_value(nested)
        return redacted
    if _is_sequence_value(value):
        return _redact_auth_allowed_scalar_or_sequence(value)
    return _redact_auth_opaque_value(value)


def _redact_auth_allowed_scalar_or_sequence(value: Any) -> Any:
    if isinstance(value, tuple):
        return tuple(
            _redact_auth_allowed_scalar(item) if _is_scalar_value(item) else _redact_auth_opaque_value(item)
            for item in value
        )
    if isinstance(value, list):
        return [
            _redact_auth_allowed_scalar(item) if _is_scalar_value(item) else _redact_auth_opaque_value(item)
            for item in value
        ]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            _redact_auth_allowed_scalar(item) if _is_scalar_value(item) else _redact_auth_opaque_value(item)
            for item in value
        ]
    return _redact_auth_allowed_scalar(value)


def _redact_auth_allowed_scalar(value: Any) -> Any:
    return redact_text(value) if isinstance(value, str) else value


def _redact_auth_opaque_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTION_MARKER
            if _should_redact_mapping_value(str(key), nested)
            else _redact_auth_opaque_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_auth_opaque_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_auth_opaque_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_auth_opaque_value(item) for item in value]
    return REDACTION_MARKER


def _is_sequence_value(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _is_scalar_value(value: Any) -> bool:
    return not isinstance(value, Mapping) and not (
        isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
    )


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.netloc:
            return _redact_sensitive_assignments(
                value.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
            )
        hostname = parsed.hostname or ""
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        return REDACTION_MARKER


def _redact_sensitive_assignments(value: str) -> str:
    """Redact assignment values with one linear, mutually exclusive scan."""
    pieces: list[str] = []
    cursor = 0
    emitted = 0
    length = len(value)
    while cursor < length:
        quote = value[cursor] if value[cursor] in {'"', "'"} else None
        if quote is not None:
            key_start = cursor
            cursor, token, malformed, closed = _scan_quoted_assignment_key(value, cursor)
            if not closed:
                continue
        elif _is_assignment_key_char(value[cursor]):
            key_start = cursor
            while cursor < length and _is_assignment_key_char(value[cursor]):
                cursor += 1
            token = value[key_start:cursor]
            malformed = False
        else:
            cursor += 1
            continue
        separator_end = cursor
        while separator_end < length and value[separator_end] in " \t\r\n":
            separator_end += 1
        key_is_sensitive = malformed or _contains_sensitive_key_fragment(token)
        if (
            not key_is_sensitive
            or separator_end >= length
            or value[separator_end] not in ":="
        ):
            continue
        value_start = separator_end + 1
        while value_start < length and value[value_start] in " \t\r\n":
            value_start += 1
        value_end, _raw, _decoded = _scan_assignment_value(value, value_start)
        pieces.append(value[emitted:key_start])
        pieces.append(value[key_start:value_start])
        pieces.append(REDACTION_MARKER)
        emitted = value_end
        cursor = value_end
    pieces.append(value[emitted:])
    return "".join(pieces)


_QUOTED_KEY_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _scan_quoted_assignment_key(value: str, start: int) -> tuple[int, str, bool, bool]:
    """Scan and decode one quoted key without revisiting input characters."""
    quote = value[start]
    decoded: list[str] = []
    malformed = False
    cursor = start + 1
    length = len(value)
    while cursor < length:
        current = value[cursor]
        if current == quote:
            return cursor + 1, "".join(decoded), malformed, True
        if current != "\\":
            if 0xD800 <= ord(current) <= 0xDFFF:
                malformed = True
            else:
                decoded.append(current)
            cursor += 1
            continue
        cursor += 1
        if cursor >= length:
            return cursor, "".join(decoded), True, False
        escape = value[cursor]
        if escape != "u":
            decoded_escape = _QUOTED_KEY_ESCAPES.get(escape)
            if decoded_escape is None:
                malformed = True
            else:
                decoded.append(decoded_escape)
            cursor += 1
            continue
        cursor += 1
        code_start = cursor
        while cursor < length and cursor - code_start < 4 and _is_hex_digit(value[cursor]):
            cursor += 1
        if cursor - code_start != 4:
            malformed = True
            continue
        codepoint = int(value[code_start:cursor], 16)
        if 0xD800 <= codepoint <= 0xDBFF:
            low_escape_end = cursor + 6
            if (
                low_escape_end <= length
                and value[cursor : cursor + 2] == "\\u"
                and all(_is_hex_digit(character) for character in value[cursor + 2 : low_escape_end])
            ):
                low = int(value[cursor + 2 : low_escape_end], 16)
                cursor = low_escape_end
                if 0xDC00 <= low <= 0xDFFF:
                    decoded.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
                else:
                    malformed = True
            else:
                malformed = True
        elif 0xDC00 <= codepoint <= 0xDFFF:
            malformed = True
        else:
            decoded.append(chr(codepoint))
    return cursor, "".join(decoded), True, False


def _is_hex_digit(character: str) -> bool:
    return character.isascii() and (character.isdigit() or character.lower() in "abcdef")


def _is_assignment_key_char(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in "_.-")


_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "credential",
    "apikey",
    "api_key",
    "api-key",
    "accesskey",
    "access_key",
    "access-key",
    "sessionkey",
    "session_key",
    "session-key",
    "signature",
    "accountingstoragepass",
    "storagepass",
)


def _contains_sensitive_key_fragment(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _replace_literals_once(value: str, literals: set[str]) -> str:
    ordered = sorted((literal for literal in literals if literal), key=lambda item: (-len(item), item))
    if not ordered:
        return value
    matcher = re.compile("|".join(re.escape(literal) for literal in ordered))
    return matcher.sub(REDACTION_MARKER, value)


def _scan_assignment_value(value: str, start: int) -> tuple[int, str, str]:
    decoded: list[str] = []
    cursor = start
    length = len(value)
    quote = value[cursor] if cursor < length and value[cursor] in {'"', "'"} else None
    if quote is not None:
        cursor += 1
    body_start = cursor
    body_end = cursor
    while cursor < length:
        current = value[cursor]
        if current == "\\" and cursor + 1 < length:
            decoded.append(value[cursor + 1])
            cursor += 2
            body_end = cursor
        elif quote is not None and current == quote:
            body_end = cursor
            cursor += 1
            break
        elif quote is None and current in " \t\r\n,;&":
            body_end = cursor
            break
        else:
            decoded.append(current)
            cursor += 1
            body_end = cursor
    return cursor, value[body_start:body_end], "".join(decoded)


def _dsn_password_candidates(dsn: str) -> set[str]:
    candidates: set[str] = set()
    cursor = 0
    while match := LIBPQ_PASSWORD_PREFIX_RE.search(dsn, cursor):
        cursor, raw, decoded = _scan_assignment_value(dsn, match.end())
        candidates.update(candidate for candidate in (raw, decoded) if candidate)
        if cursor == match.end():
            cursor += 1
    try:
        parsed_password = urlsplit(dsn).password
    except Exception:
        parsed_password = None
    if parsed_password:
        candidates.add(parsed_password)
    scheme, separator, remainder = dsn.partition(":")
    if separator and scheme.lower() in {"postgres", "postgresql"} and not remainder.startswith("//"):
        userinfo, at, _target = remainder.rpartition("@")
        if at and ":" in userinfo:
            _username, _colon, password = userinfo.partition(":")
            if password:
                candidates.add(password)
    return candidates


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
