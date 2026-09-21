"""Registry cutover declaration loading, schema and generation derivation.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow
from scripts.scheduler_refresh.config import _iso_utc
from scripts.scheduler_refresh.constants import (
    CUTOVER_CYCLE_HOURS,
    CUTOVER_FUTURE_TOLERANCE,
    CUTOVER_PAST_TOLERANCE,
    CUTOVER_TRANSITION_MODES,
    MAX_CUTOVER_DECLARATION_BYTES,
    REGISTRY_MANIFEST_SCHEMA_VERSION,
    RefreshError,
)
from services.orchestrator.scheduler_file_providers import MAX_REGISTRY_MANIFEST_BYTES


def _cutover_declaration_env_resolves_to_file(env_value: str | None) -> bool:
    """Return True when the cutover declaration env points at a readable file.

    R2-A1 audit helper: only records "did the operator stage a file the runner
    could open" — schema validity is proven separately by
    ``_load_cutover_declaration``.  Missing env, symlinks, non-regular files,
    and permission errors collapse to ``False`` so the audit fact never
    overclaims presence.
    """
    if not env_value:
        return False
    path = Path(env_value).expanduser()
    try:
        stat_result = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(stat_result.st_mode):
        return False
    return os.access(str(path), os.R_OK)

def _load_previous_canonical_registry(
    registry_uri: str, *, containment_root: Path
) -> tuple[str, list[dict[str, Any]], bytes] | None:
    """Return (sha256, models, raw_bytes) for the current canonical manifest.

    Missing file is legitimate first publication and returns ``None``.  Any
    other read/parse failure is a hard refusal condition and propagates.
    Returned ``raw_bytes`` lets the caller hand the exact bytes that were
    classified to downstream code without a second read (see finding C-F2).
    """
    path = Path(registry_uri)
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            containment_root=containment_root,
        )
    except FileNotFoundError:
        return None
    except (OSError, SafeFilesystemError) as error:
        raise RefreshError("provider_invalid") from error
    # Sentinel-plus-one: read_bytes_limited_no_follow returns max_bytes+1
    # bytes when the file is oversize; enforce the explicit cap here so the
    # loader is symmetric with _read_provider_header/provider_atomic.
    if len(content) > MAX_REGISTRY_MANIFEST_BYTES:
        raise RefreshError("provider_invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("provider_invalid") from error
    if not isinstance(payload, Mapping):
        raise RefreshError("provider_invalid")
    models = payload.get("models")
    if not isinstance(models, list):
        raise RefreshError("provider_invalid")
    normalized: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping):
            raise RefreshError("provider_invalid")
        normalized.append(dict(model))
    return hashlib.sha256(content).hexdigest(), normalized, content

def _prospective_registry_content(
    registry_models: Sequence[Mapping[str, Any]], *, generated_at: datetime
) -> tuple[bytes, str]:
    """Return the exact canonical bytes and SHA-256 that
    ``publish_scheduler_registry_manifest`` will commit.

    Mirrors the payload shape in
    ``services/orchestrator/scheduler_file_providers.publish_scheduler_registry_manifest``
    so the receipt's `new_registry_sha256` matches the actual on-disk hash.
    """
    payload: dict[str, Any] = {
        "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "models": [dict(model) for model in registry_models],
    }
    canonical_without_checksum = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    payload["checksum"] = f"sha256:{hashlib.sha256(canonical_without_checksum).hexdigest()}"
    pretty = json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    return pretty, hashlib.sha256(pretty).hexdigest()

def _prospective_registry_generation(
    registry_models: Sequence[Mapping[str, Any]], *, generated_at: datetime
) -> str:
    """Stable identifier for one prospective registry publication.

    Operators observe this value on a refused refresh receipt and file the
    matching cutover declaration.  The generation is a pure content hash of
    the sorted-by-model_id model list (``generated_at`` is intentionally
    excluded from the preimage): the value is byte-for-byte deterministic
    across any wall-clock interval, so the operator's refuse -> declare ->
    retry loop always sees the same generation string as long as the model
    set has not itself drifted.

    ``generated_at`` is still accepted so callers stay symmetric with
    ``_prospective_registry_content``, but the parameter is unused.  Keeping
    the signature stable avoids touching every call site.
    """
    del generated_at  # unused — kept for signature stability
    normalized = [
        {key: value for key, value in dict(model).items()}
        for model in registry_models
    ]
    normalized.sort(key=lambda model: str(model.get("model_id") or ""))
    preimage = json.dumps(
        {"models": normalized},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(preimage).hexdigest()
    return f"manifest-{digest[:12]}"

def _load_cutover_declaration(
    env_path: str | None, *, now: datetime
) -> dict[str, Any] | None:
    """Read/validate a cutover declaration file; return the parsed payload.

    Absent env or empty value returns ``None`` (no declaration).  Any file
    validation failure raises RefreshError(registry_cutover_declaration_invalid).
    """
    if not env_path:
        return None
    path = Path(env_path).expanduser()
    if not path.is_absolute():
        raise RefreshError("registry_cutover_declaration_invalid")
    parent = path.parent
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_CUTOVER_DECLARATION_BYTES,
            containment_root=parent,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    # Sentinel-plus-one: enforce the byte cap explicitly (finding C-F4).
    if len(content) > MAX_CUTOVER_DECLARATION_BYTES:
        raise RefreshError("registry_cutover_declaration_invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    try:
        # Module-level validator (finding C-F3) avoids the per-call
        # metaschema resolution jsonschema.validate performs.
        _CUTOVER_DECLARATION_VALIDATOR.validate(payload)
    except jsonschema.ValidationError as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    if not isinstance(payload, dict):
        raise RefreshError("registry_cutover_declaration_invalid")
    entries = payload.get("entries") or []
    seen: set[str] = set()
    for entry in entries:
        model_id = str(entry.get("model_id") or "")
        if model_id in seen:
            raise RefreshError("registry_cutover_declaration_invalid")
        seen.add(model_id)
        try:
            cycle = datetime.fromisoformat(str(entry["effective_cycle_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as error:
            raise RefreshError("registry_cutover_declaration_invalid") from error
        if cycle.tzinfo is None:
            raise RefreshError("registry_cutover_declaration_invalid")
        cycle = cycle.astimezone(UTC)
        if (
            cycle.minute != 0
            or cycle.second != 0
            or cycle.microsecond != 0
            or cycle.hour not in CUTOVER_CYCLE_HOURS
        ):
            raise RefreshError("registry_cutover_declaration_invalid")
        if cycle < now - CUTOVER_PAST_TOLERANCE or cycle > now + CUTOVER_FUTURE_TOLERANCE:
            raise RefreshError("registry_cutover_declaration_invalid")
        transition_mode = str(entry.get("transition_mode"))
        if transition_mode not in CUTOVER_TRANSITION_MODES:
            raise RefreshError("registry_cutover_declaration_invalid")
        # Mirror the schema's mode/checksum conditionals in code (#1433) so a
        # payload that reached here through a stale validator still fails
        # closed: a retirement declares no new package, a replacement must.
        new_checksum = entry.get("new_checksum")
        if transition_mode == "retire":
            if new_checksum is not None:
                raise RefreshError("registry_cutover_declaration_invalid")
        elif (
            not isinstance(new_checksum, str)
            or len(new_checksum) != 64
            or any(character not in "0123456789abcdef" for character in new_checksum)
        ):
            raise RefreshError("registry_cutover_declaration_invalid")
    return payload

# jsonschema is loaded from the vendored file at import time so the gate does
# not touch the filesystem per call.
_CUTOVER_DECLARATION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "scheduler_registry_package_cutover.schema.json"
)

try:
    _CUTOVER_DECLARATION_SCHEMA = json.loads(
        _CUTOVER_DECLARATION_SCHEMA_PATH.read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError) as _cutover_schema_load_error:  # pragma: no cover
    raise RuntimeError(
        f"cutover declaration schema unavailable: {_cutover_schema_load_error}"
    ) from _cutover_schema_load_error

# Module-level validator: jsonschema.validate() re-resolves the metaschema on
# every call and builds a fresh validator instance.  We hold the validator
# once (finding C-F3) so the gate pays the metaschema hop only at import.
# R2-B6 (round-2 review): attach a FormatChecker with a registered
# ``date-time`` check (the default Draft202012Validator FORMAT_CHECKER only
# ships date-time when ``rfc3339-validator`` is installed).  The check
# mirrors the consumer at ``services/orchestrator/scheduler_generation.py``
# so publisher and consumer accept and reject the same set of values.
_CUTOVER_DECLARATION_FORMAT_CHECKER = jsonschema.FormatChecker()

@_CUTOVER_DECLARATION_FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))
def _cutover_datetime_format_check(value: Any) -> bool:  # pragma: no cover - trivial
    """Return True when ``value`` parses as an aware RFC 3339 date-time."""
    if not isinstance(value, str):
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive datetime not permitted")
    return True

_CUTOVER_DECLARATION_VALIDATOR = jsonschema.Draft202012Validator(
    _CUTOVER_DECLARATION_SCHEMA,
    format_checker=_CUTOVER_DECLARATION_FORMAT_CHECKER,
)
