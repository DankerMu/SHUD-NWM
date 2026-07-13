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
SENSITIVE_ASSIGNMENT_PREFIX_RE = re.compile(
    r"\b([A-Za-z0-9_.-]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|"
    r"access[_-]?key|session[_-]?key|signature|accountingstoragepass|storagepass)"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)",
    re.IGNORECASE,
)
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
        redacted = redacted.replace(normalized_dsn, redact_text(normalized_dsn))
        try:
            password_raw = urlsplit(normalized_dsn).password
        except ValueError:
            password_raw = None
        if password_raw:
            try:
                password_decoded = unquote(password_raw)
            except Exception:
                password_decoded = password_raw
            for secret in (password_raw, password_decoded):
                if secret:
                    redacted = redacted.replace(secret, REDACTION_MARKER)
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
    length = len(value)
    while match := SENSITIVE_ASSIGNMENT_PREFIX_RE.search(value, cursor):
        pieces.append(value[cursor : match.start()])
        pieces.append(f"{match.group(1)}{match.group(2)}{REDACTION_MARKER}")
        value_start = match.end()
        value_end = value_start
        if value_start < length and value[value_start] in {'"', "'"}:
            quote = value[value_start]
            value_end += 1
            while value_end < length:
                current = value[value_end]
                if current == "\\" and value_end + 1 < length:
                    value_end += 2
                elif current == quote:
                    value_end += 1
                    break
                else:
                    value_end += 1
        else:
            while value_end < length and value[value_end] not in " \t\r\n,;&":
                if value[value_end] == "\\" and value_end + 1 < length:
                    value_end += 2
                else:
                    value_end += 1
        cursor = value_end
    pieces.append(value[cursor:])
    return "".join(pieces)


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
