"""Public rendering for operator-visible evidence payloads.

One renderer, two consumers.  The manual-retry route
(``POST /runs/{run_id}/retry``) answers a ``submission_failed`` attempt with a
structured 503 whose ``details.runtime_root_resolution`` is produced by
whichever lane served the retry: ``RetryService`` (database, reads the persisted
event and renders on the way out) or ``FileJournalRetryService`` (file journal,
renders once at write time and returns the persisted mapping unchanged).  Both
must put the SAME public shape on the wire -- absolute local roots as
``[local-path]``, URIs as ``[uri]``/``[object-uri]``, secrets as ``[redacted]``
-- so they share this code instead of each owning a copy (openspec change
``retry-runtime-root-evidence-public-shape``, #1961 + #1965).

Why a leaf module rather than a home in either lane: this code used to live in
``file_orchestration_journal.py``, which ``retry.py`` cannot import (the journal
imports ``retry``), and its scalar classifier used to come from
``scheduler_file_providers.py``, which ``retry.py`` also cannot import
(providers -> ``scheduler_state`` -> ``retry``).  Keeping the imports here down
to ``packages.common.redaction``, ``scheduler_state_common`` and the standard
library makes the module importable from retry, from the journal and from
providers alike, with no cycle in any direction.  Two dependencies are therefore
carried as local equivalents at the bottom of this file, pinned by a parity
test: ``_safe_error_message`` (``retry.py``) and ``_public_path_or_uri_placeholder``
(``scheduler_file_providers._sanitize_file_provider_scalar``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from packages.common.redaction import is_sensitive_key, redact_payload
from services.orchestrator.scheduler_state_common import _format_utc


def _public_evidence(value: Any) -> Any:
    return _sanitize_public_evidence(value)


def _sanitize_public_evidence(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_public_field(str(key), nested)
            for key, nested in value.items()
            if not str(key).startswith("_file_journal_")
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_public_evidence(item) for item in value]
    return _sanitize_public_scalar(value)


def _sanitize_public_field(key: str, value: Any) -> Any:
    lowered = key.lower()
    if is_sensitive_key(key):
        return "[redacted]" if value not in (None, "") else value
    if lowered == "message" or lowered.endswith("_message"):
        return _public_message(value)
    if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
        # #1965: a path-shaped KEY says nothing about a mapping VALUE.  Replacing
        # the whole mapping with ``[local-path]`` dropped ``present``/``source``/
        # ``same_as_workspace`` from ``runtime_root_resolution.resolved.object_store_root``
        # while its no-key-match sibling ``workspace_dir`` kept them -- one
        # mapping, two JSON types for sibling keys.  Recursing renders the inner
        # ``value`` as ``[local-path]`` all the same and keeps the provenance.
        # Scalars (and sequences) under these keys are unchanged.
        if isinstance(value, Mapping):
            return _sanitize_public_evidence(value)
        return "[local-path]" if value not in (None, "") else value
    if lowered.endswith("_uri") or lowered in {"uri", "object_uri", "manifest_uri"}:
        return _public_path_or_uri_placeholder(value)
    return _sanitize_public_evidence(value)


def _sanitize_public_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(value)
    if sanitized != value:
        return sanitized
    return _sanitize_public_text(value)


def _sanitize_public_path_or_uri_scalar(value: str) -> str:
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return value
    if (
        text.startswith("/")
        or text.startswith("~")
        or "://" in text
        or text.startswith("s3:")
        or text.startswith("published:")
    ):
        return _public_path_or_uri_placeholder(value)
    return value


def _public_message(value: Any) -> Any:
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        return _sanitize_public_evidence(value)
    return _sanitize_public_text(value)


def _sanitize_public_text(value: str) -> str:
    redacted = _safe_error_message(value)
    return _sanitize_public_text_tokens(redacted)


def _sanitize_public_text_tokens(value: str) -> str:
    rendered: list[str] = []
    token = ""
    for char in value:
        if char.isspace():
            if token:
                rendered.append(_sanitize_public_text_token(token))
                token = ""
            rendered.append(char)
        else:
            token += char
    if token:
        rendered.append(_sanitize_public_text_token(token))
    return "".join(rendered)


def _sanitize_public_text_token(value: str) -> str:
    prefix_length = 0
    suffix_length = 0
    while prefix_length < len(value) and value[prefix_length] in "'\"([{<":
        prefix_length += 1
    while suffix_length < len(value) - prefix_length and value[len(value) - suffix_length - 1] in "'\".,;:!?)]}>":
        suffix_length += 1
    prefix = value[:prefix_length]
    suffix = value[len(value) - suffix_length :] if suffix_length else ""
    core = value[prefix_length : len(value) - suffix_length if suffix_length else len(value)]
    if not core:
        return value
    sanitized = _sanitize_public_path_or_uri_scalar(core)
    if sanitized == core:
        for separator in ("=", ":"):
            key, found, nested = core.partition(separator)
            if not found or not key or not nested:
                continue
            sanitized_nested = _sanitize_public_path_or_uri_scalar(nested)
            if sanitized_nested != nested:
                sanitized = f"{key}{found}{sanitized_nested}"
                break
    return f"{prefix}{sanitized}{suffix}" if sanitized != core else value


# --- local equivalents of dependencies this leaf cannot import ---------------
#
# Both are byte-for-byte copies of a single upstream body.  Importing the
# originals would re-introduce the cycles this module exists to avoid:
# ``retry.py`` for ``_safe_error_message`` (the journal imports retry, so retry
# cannot import the journal) and ``scheduler_file_providers.py`` for the scalar
# classifier (providers -> scheduler_state -> retry).  The providers copy stays
# where it is and keeps its own whole-value ``_root`` rule; a parity test pins
# the two classifiers to the same output over a shared corpus.


def _safe_error_message(message: str) -> str:
    """Local equivalent of ``services.orchestrator.retry._safe_error_message``."""

    redacted = redact_payload(message)
    return redacted if isinstance(redacted, str) else str(redacted)


def _public_path_or_uri_placeholder(value: Any) -> Any:
    """Local equivalent of ``scheduler_file_providers._sanitize_file_provider_scalar``.

    Called where the journal's original code reached the providers module
    through ``_sanitize_file_provider_evidence_scalar(key, value)`` with a
    ``_uri``-shaped key and through ``_sanitize_file_provider_evidence_scalar("uri", value)``:
    both of those dispatch straight to this classifier, so the substitution is
    behaviour-preserving.
    """

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    parsed = urlparse(text)
    if parsed.scheme in {"s3", "published"}:
        return "[object-uri]"
    if parsed.scheme:
        return "[uri]"
    if text.startswith("/") or text.startswith("~"):
        return "[local-path]"
    return value
