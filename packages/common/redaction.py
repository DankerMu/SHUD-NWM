from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

from psycopg2.extensions import parse_dsn

REDACTION_MARKER = "[redacted]"
BOOLEAN_CONFIGURED_FIELD_ALLOWLIST = frozenset({"database_url_configured"})

_AUTHORIZATION_KEY_PATTERN = (
    r"(?:(?:proxy[-_.]?)?authorization|"
    r"(?<![A-Za-z0-9])(?:proxy[-_.]?)?auth[-_.]?header)"
)

SENSITIVE_KEY_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[-_.]?key|access[-_.]?key|session[-_.]?key|signature|"
    rf"accountingstoragepass|storagepass|{_AUTHORIZATION_KEY_PATTERN}|^auth$)",
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
AUTHORIZATION_KEY_RE = re.compile(
    rf"(?:{_AUTHORIZATION_KEY_PATTERN}|auth)",
    re.IGNORECASE,
)
LIBPQ_PASSWORD_PREFIX_RE = re.compile(
    r"(?<!\S)(?:password|sslpassword)\s*=\s*", re.IGNORECASE
)
URL_RE = re.compile(r"(?:(?:[A-Za-z][A-Za-z0-9+.-]*)://|//)[^\s\"'<>]+", re.IGNORECASE)
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
MAX_FRAGMENT_DECODE_LAYERS = 3


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
    redacted = _redact_sensitive_assignments(redacted)
    return _redact_authorization_schemes(redacted)


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
        fragment_sensitive = False
        if quote is not None:
            key_start = cursor
            cursor, token, malformed, closed = _scan_quoted_assignment_key(value, cursor)
            fragment_sensitive = _fragment_contains_sensitive_assignment(token)
            if fragment_sensitive:
                pieces.append(value[emitted:key_start])
                pieces.append(REDACTION_MARKER)
                emitted = cursor
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
        if quote is None:
            separator_end = _skip_assignment_key_closer(value, separator_end)
        while separator_end < length and _is_assignment_whitespace(value[separator_end]):
            separator_end += 1
        if quote is not None and (
            separator_end >= length or value[separator_end] not in ":="
        ):
            continue
        key_is_sensitive = malformed or fragment_sensitive or is_sensitive_key(token)
        if (
            not key_is_sensitive
            or separator_end >= length
            or value[separator_end] not in ":="
        ):
            continue
        value_start = separator_end + 1
        while value_start < length and _is_assignment_whitespace(value[value_start]):
            value_start += 1
        if _is_authorization_assignment_key(token):
            value_end, replacement = _scan_authorization_assignment_value(value, value_start)
        else:
            value_end, _raw, _decoded = _scan_assignment_value(value, value_start)
            replacement = REDACTION_MARKER
        pieces.append(value[emitted:value_start])
        pieces.append(replacement)
        emitted = value_end
        cursor = value_end
    pieces.append(value[emitted:])
    return "".join(pieces)


def _fragment_contains_sensitive_assignment(value: str) -> bool:
    """Inspect at most three escape layers with a fixed linear work bound."""
    candidate = value
    if _fragment_contains_sensitive_assignment_once(candidate):
        return True
    for _layer in range(MAX_FRAGMENT_DECODE_LAYERS - 1):
        decoded = _decode_escaped_fragment_once(candidate)
        if decoded == candidate:
            return False
        if _fragment_contains_sensitive_assignment_decoded(decoded):
            return True
        candidate = decoded
    return False


def _fragment_contains_sensitive_assignment_once(value: str) -> bool:
    """Inspect one decoded quoted fragment with a bounded monotonic key scan."""
    cursor = 0
    length = len(value)
    while cursor < length:
        quote = value[cursor] if value[cursor] in {'"', "'"} else None
        malformed = False
        if quote is not None:
            cursor, token, malformed, closed = _scan_quoted_assignment_key(value, cursor)
            if _fragment_contains_bare_sensitive_assignment(token):
                return True
            if not closed:
                continue
        elif _is_assignment_key_char(value[cursor]):
            key_start = cursor
            while cursor < length and _is_assignment_key_char(value[cursor]):
                cursor += 1
            token = value[key_start:cursor]
        else:
            cursor += 1
            continue
        separator = cursor
        while separator < length and _is_assignment_whitespace(value[separator]):
            separator += 1
        if (
            separator < length
            and value[separator] in ":="
            and (malformed or is_sensitive_key(token))
        ):
            return True
    return False


def _fragment_contains_sensitive_assignment_decoded(value: str) -> bool:
    cursor = 0
    length = len(value)
    while cursor < length:
        character = value[cursor]
        if not (character.isascii() and (character.isalnum() or character in "_.-")):
            cursor += 1
            continue
        key_start = cursor
        while cursor < length:
            character = value[cursor]
            if not (character.isascii() and (character.isalnum() or character in "_.-")):
                break
            cursor += 1
        separator = cursor
        separator = _skip_assignment_key_closer(value, separator)
        while separator < length and _is_assignment_whitespace(value[separator]):
            separator += 1
        if (
            separator < length
            and value[separator] in ":="
            and is_sensitive_key(value[key_start:cursor])
        ):
            return True
    return False


def _skip_assignment_key_closer(value: str, start: int) -> int:
    """Skip one bounded slash-run and its optional quoted-key closer."""
    cursor = start
    length = len(value)
    while cursor < length and value[cursor] == "\\":
        cursor += 1
    if cursor < length and value[cursor] in {'"', "'"}:
        cursor += 1
    return cursor


def _decode_escaped_fragment_once(value: str) -> str:
    """Decode one JSON-like escape layer without recursion or input revisits."""
    decoded: list[str] = []
    cursor = 0
    length = len(value)
    while cursor < length:
        current = value[cursor]
        if current != "\\" or cursor + 1 >= length:
            decoded.append(current)
            cursor += 1
            continue
        escape = value[cursor + 1]
        if escape == "u" and cursor + 6 <= length and all(
            _is_hex_digit(character) for character in value[cursor + 2 : cursor + 6]
        ):
            codepoint = int(value[cursor + 2 : cursor + 6], 16)
            decoded.append("\ufffd" if 0xD800 <= codepoint <= 0xDFFF else chr(codepoint))
            cursor += 6
            continue
        decoded_escape = _QUOTED_KEY_ESCAPES.get(escape)
        if decoded_escape is None:
            decoded.extend((current, escape))
        else:
            decoded.append(decoded_escape)
        cursor += 2
    return "".join(decoded)


def _fragment_contains_bare_sensitive_assignment(value: str) -> bool:
    """Inspect decoded quote contents without recursively parsing nested quotes."""
    cursor = 0
    length = len(value)
    while cursor < length:
        if not _is_assignment_key_char(value[cursor]):
            cursor += 1
            continue
        key_start = cursor
        while cursor < length and _is_assignment_key_char(value[cursor]):
            cursor += 1
        separator = cursor
        while separator < length and _is_assignment_whitespace(value[separator]):
            separator += 1
        if (
            separator < length
            and value[separator] in ":="
            and is_sensitive_key(value[key_start:cursor])
        ):
            return True
    return False


def _is_authorization_assignment_key(key: str) -> bool:
    return AUTHORIZATION_KEY_RE.fullmatch(key) is not None


def _scan_authorization_assignment_value(value: str, start: int) -> tuple[int, str]:
    quote = value[start] if start < len(value) and value[start] in {'"', "'"} else None
    if quote is not None:
        end, _raw, decoded = _scan_assignment_value(value, start)
        closed = end <= len(value) and end > start and value[end - 1] == quote
        replacement = _redact_authorization_schemes(decoded)
        if replacement == decoded:
            replacement = REDACTION_MARKER
        return end, f"{quote}{replacement}{quote if closed else ''}"

    scheme = _authorization_scheme_at(value, start)
    if scheme is None:
        end, _raw, _decoded = _scan_assignment_value(value, start)
        return end, REDACTION_MARKER
    scheme_end = start + len(scheme)
    separator_end = scheme_end
    while separator_end < len(value) and _is_assignment_whitespace(value[separator_end]):
        separator_end += 1
    if separator_end == scheme_end:
        end, _raw, _decoded = _scan_assignment_value(value, start)
        return end, REDACTION_MARKER
    credential_end, marker = _scan_authorization_credential(value, separator_end)
    return credential_end, f"{value[start:separator_end]}{marker}"


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


def _is_assignment_whitespace(character: str) -> bool:
    """Own the Unicode whitespace semantics shared by assignment scans."""
    return character.isspace()


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
    escaped_quote: str | None = None
    if quote is None:
        slash_end = cursor
        while slash_end < length and value[slash_end] == "\\":
            slash_end += 1
        if slash_end > cursor and slash_end < length and value[slash_end] in {'"', "'"}:
            escaped_quote = value[cursor : slash_end + 1]
            quote = value[slash_end]
            cursor = slash_end + 1
    else:
        cursor += 1
    body_start = cursor
    body_end = cursor
    while cursor < length:
        current = value[cursor]
        if escaped_quote is not None and current == "\\":
            slash_end = cursor
            while slash_end < length and value[slash_end] == "\\":
                slash_end += 1
            if slash_end < length and value[slash_end] == quote and (
                slash_end + 1 == length
                or _is_assignment_whitespace(value[slash_end + 1])
                or value[slash_end + 1] in ",;&}]"
            ):
                body_end = cursor
                cursor = slash_end + 1
                break
            while cursor < slash_end:
                if cursor + 1 < slash_end:
                    decoded.append("\\")
                    cursor += 2
                else:
                    decoded.append("\\")
                    cursor += 1
            body_end = cursor
            continue
        if current == "\\" and cursor + 1 < length:
            decoded.append(value[cursor + 1])
            cursor += 2
            body_end = cursor
        elif escaped_quote is not None and current == quote:
            decoded.append(current)
            cursor += 1
            body_end = cursor
        elif quote is not None and current == quote:
            body_end = cursor
            cursor += 1
            break
        elif quote is None and (_is_assignment_whitespace(current) or current in ",;&"):
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
    try:
        parsed_dsn = parse_dsn(dsn)
    except Exception:
        parsed_dsn = {}
    candidates.update(
        parsed_dsn[key]
        for key in ("password", "sslpassword")
        if isinstance(parsed_dsn.get(key), str) and parsed_dsn[key]
    )
    try:
        query = urlsplit(dsn).query
    except Exception:
        query = ""
    for field in query.split("&"):
        raw_key, separator, raw_value = field.partition("=")
        if separator and unquote_plus(raw_key).lower() in {"password", "sslpassword"}:
            candidates.add(raw_value)
            candidates.add(unquote_plus(raw_value))
    scheme, separator, remainder = dsn.partition(":")
    if separator and scheme.lower() in {"postgres", "postgresql"} and not remainder.startswith("//"):
        userinfo, at, _target = remainder.rpartition("@")
        if at and ":" in userinfo:
            _username, _colon, password = userinfo.partition(":")
            if password:
                candidates.add(password)
    return candidates


def _authorization_scheme_at(value: str, start: int) -> str | None:
    if start > 0 and (value[start - 1].isalnum() or value[start - 1] in "_.-"):
        return None
    for scheme in ("Bearer", "Basic"):
        end = start + len(scheme)
        if value[start:end].lower() != scheme.lower():
            continue
        if end < len(value) and (value[end].isalnum() or value[end] in "_.-"):
            continue
        return value[start:end]
    return None


def _scan_authorization_credential(value: str, start: int) -> tuple[int, str]:
    length = len(value)
    if start >= length:
        return start, REDACTION_MARKER
    if value.startswith(REDACTION_MARKER, start):
        return start + len(REDACTION_MARKER), REDACTION_MARKER
    slash_end = start
    while slash_end < length and value[slash_end] == "\\":
        slash_end += 1
    escaped_quote = (
        value[start : slash_end + 1]
        if slash_end > start and slash_end < length and value[slash_end] in {'"', "'"}
        else None
    )
    quote = value[start] if value[start] in {'"', "'"} else None
    if quote is not None or escaped_quote is not None:
        delimiter = escaped_quote or quote or ""
        cursor = start + len(delimiter)
        while cursor < length:
            if escaped_quote is not None and value[cursor] == "\\":
                closing_end = cursor
                while closing_end < length and value[closing_end] == "\\":
                    closing_end += 1
                if closing_end < length and value[closing_end] == escaped_quote[-1]:
                    closing = value[cursor : closing_end + 1]
                    return closing_end + 1, f"{delimiter}{REDACTION_MARKER}{closing}"
                cursor = closing_end
                continue
            if quote is not None and value[cursor] == quote:
                return cursor + 1, f"{quote}{REDACTION_MARKER}{quote}"
            if value[cursor] == "\\" and cursor + 1 < length:
                cursor += 2
            else:
                cursor += 1
        return cursor, f"{delimiter}{REDACTION_MARKER}"

    cursor = start
    while cursor < length and not (
        _is_assignment_whitespace(value[cursor])
        or value[cursor] in "\"',;&<>{}[]()"
    ):
        cursor += 1
    secret = value[start:cursor]
    trimmed = secret.rstrip(".,:!?")
    suffix = secret[len(trimmed) :]
    return cursor, f"{REDACTION_MARKER}{suffix}"


def _redact_authorization_schemes(value: str) -> str:
    """Redact free-form Bearer/Basic credentials with one monotonic scan."""
    pieces: list[str] = []
    emitted = 0
    cursor = 0
    length = len(value)
    while cursor < length:
        scheme = _authorization_scheme_at(value, cursor)
        if scheme is None:
            cursor += 1
            continue
        scheme_end = cursor + len(scheme)
        separator_end = scheme_end
        while separator_end < length and _is_assignment_whitespace(value[separator_end]):
            separator_end += 1
        if separator_end == scheme_end or separator_end >= length:
            cursor = scheme_end
            continue
        credential_end, marker = _scan_authorization_credential(value, separator_end)
        if credential_end == separator_end:
            cursor = scheme_end
            continue
        pieces.append(value[emitted:separator_end])
        pieces.append(marker)
        emitted = credential_end
        cursor = credential_end
    pieces.append(value[emitted:])
    return "".join(pieces)
