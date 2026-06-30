from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import SplitResult, unquote, urlsplit

from packages.common.source_identity import normalize_source_id
from services.artifacts.reader import DEFAULT_PUBLISHED_URI_PREFIX
from workers.data_adapters.base import parse_cycle_time

PRODUCTION_CONTRACT_SCHEMA_VERSION = "nhms.production.identity_status_uri_contract.v1"
PRODUCTION_CONTRACT_ID = "m23-qhh-22-production-identity-status-uri.v1"

PRODUCTION_IDENTITY_FIELDS: tuple[str, ...] = (
    "run_id",
    "model_id",
    "basin_id",
    "source",
    "cycle_time",
    "basin_version_id",
    "river_network_version_id",
    "canonical_product_id",
    "forcing_version_id",
    "hydro_run_id",
    "published_manifest_id",
)
OPTIONAL_PRODUCTION_IDENTITY_FIELDS: tuple[str, ...] = ("pipeline_job_id", "pipeline_event_id")
PRODUCTION_EVIDENCE_CORRELATION_FIELDS: tuple[str, ...] = OPTIONAL_PRODUCTION_IDENTITY_FIELDS

PRODUCTION_STAGE_TAXONOMY: tuple[str, ...] = (
    "download",
    "convert",
    "forcing",
    "forecast",
    "parse",
    "q_down_publish",
    "production_run",
)
PRODUCTION_STATUS_TAXONOMY: tuple[str, ...] = (
    "pending",
    "ready",
    "running",
    "succeeded",
    "blocked",
    "unavailable",
    "partial",
    "failed",
    "cancelled",
    "superseded",
)

_SAFE_PUBLIC_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENCODED_FORBIDDEN_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_CREDENTIAL_WORD_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|session[_-]?key|signature)",
    re.IGNORECASE,
)
_LOCAL_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_PUBLIC_S3_PREFIXES = ("logs", "manifests", "products", "runs")

_IDENTITY_ALIASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "run_id": (("run_id",), ("identity", "run_id"), ("hydro_run", "run_id")),
    "model_id": (("model_id",), ("identity", "model_id"), ("model", "model_id"), ("pipeline_job", "model_id")),
    "basin_id": (
        ("basin_id",),
        ("identity", "basin_id"),
        ("model", "basin_id"),
        ("pipeline_job", "basin_id"),
        ("hydro_run", "basin_id"),
    ),
    "source": (
        ("source",),
        ("source_id",),
        ("identity", "source"),
        ("identity", "source_id"),
        ("forecast_cycle", "source_id"),
    ),
    "cycle_time": (
        ("cycle_time",),
        ("cycle_time_utc",),
        ("identity", "cycle_time"),
        ("identity", "cycle_time_utc"),
        ("forecast_cycle", "cycle_time"),
    ),
    "basin_version_id": (
        ("basin_version_id",),
        ("identity", "basin_version_id"),
        ("model", "basin_version_id"),
        ("hydro_run", "basin_version_id"),
    ),
    "river_network_version_id": (
        ("river_network_version_id",),
        ("identity", "river_network_version_id"),
        ("model", "river_network_version_id"),
    ),
    "canonical_product_id": (
        ("canonical_product_id",),
        ("canonical_met_product_id",),
        ("identity", "canonical_product_id"),
        ("canonical_product", "canonical_product_id"),
        ("canonical_product", "product_id"),
    ),
    "forcing_version_id": (
        ("forcing_version_id",),
        ("identity", "forcing_version_id"),
        ("forcing", "forcing_version_id"),
        ("forcing_version", "forcing_version_id"),
    ),
    "hydro_run_id": (
        ("hydro_run_id",),
        ("identity", "hydro_run_id"),
        ("hydro_run", "hydro_run_id"),
        ("hydro_run", "run_id"),
    ),
    "published_manifest_id": (
        ("published_manifest_id",),
        ("identity", "published_manifest_id"),
        ("published_manifest", "manifest_id"),
        ("published_manifest", "id"),
        ("outputs", "published_manifest_id"),
    ),
    "pipeline_job_id": (
        ("pipeline_job_id",),
        ("identity", "pipeline_job_id"),
    ),
    "pipeline_event_id": (
        ("pipeline_event_id",),
        ("identity", "pipeline_event_id"),
    ),
}


class ProductionContractError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        expected: Any = None,
        actual: Any = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.expected = expected
        self.actual = actual
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.field is not None:
            payload["field"] = self.field
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class ProductionIdentity:
    run_id: str
    model_id: str
    basin_id: str
    source: str
    cycle_time: str
    basin_version_id: str
    river_network_version_id: str
    canonical_product_id: str
    forcing_version_id: str
    hydro_run_id: str
    published_manifest_id: str
    pipeline_job_id: str | None = None
    pipeline_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_id"] = self.source
        return {key: value for key, value in payload.items() if value is not None}


def production_contract_matrix() -> dict[str, Any]:
    return {
        "schema_version": PRODUCTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": PRODUCTION_CONTRACT_ID,
        "openspec_change": "m23-qhh-22-production-automation",
        "scope": "production_identity_status_uri_boundary",
        "identity_fields": list(PRODUCTION_IDENTITY_FIELDS),
        "optional_identity_fields": list(OPTIONAL_PRODUCTION_IDENTITY_FIELDS),
        "stages": list(PRODUCTION_STAGE_TAXONOMY),
        "statuses": list(PRODUCTION_STATUS_TAXONOMY),
        "uri_boundary": {
            "display_readable_schemes": ["published", "file", "s3"],
            "published_uri_prefix": DEFAULT_PUBLISHED_URI_PREFIX,
            "requires_explicit_identity_binding": True,
            "private_path_classes": ["workspace", "scratch", "slurm", "traversal", "non_allowlisted_local"],
        },
    }


def production_identity_contract_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    identity = _identity_values(payload)
    missing = [field for field in PRODUCTION_IDENTITY_FIELDS if identity.get(field) in (None, "")]
    return {
        "schema_version": PRODUCTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": PRODUCTION_CONTRACT_ID,
        "identity": {key: value for key, value in identity.items() if value not in (None, "")},
        "required_fields": list(PRODUCTION_IDENTITY_FIELDS),
        "optional_fields": list(OPTIONAL_PRODUCTION_IDENTITY_FIELDS),
        "complete": not missing,
        "missing_fields": missing,
    }


def production_identity_from_payload(
    payload: Mapping[str, Any] | ProductionIdentity,
    *,
    require_complete: bool = True,
) -> ProductionIdentity:
    if isinstance(payload, ProductionIdentity):
        return payload
    values = _identity_values(payload)
    missing = [field for field in PRODUCTION_IDENTITY_FIELDS if values.get(field) in (None, "")]
    if require_complete and missing:
        raise ProductionContractError(
            "PRODUCTION_IDENTITY_MISSING",
            "Production identity evidence is missing required fields.",
            details={"missing_fields": missing},
        )
    return ProductionIdentity(
        run_id=str(values.get("run_id") or ""),
        model_id=str(values.get("model_id") or ""),
        basin_id=str(values.get("basin_id") or ""),
        source=str(values.get("source") or ""),
        cycle_time=str(values.get("cycle_time") or ""),
        basin_version_id=str(values.get("basin_version_id") or ""),
        river_network_version_id=str(values.get("river_network_version_id") or ""),
        canonical_product_id=str(values.get("canonical_product_id") or ""),
        forcing_version_id=str(values.get("forcing_version_id") or ""),
        hydro_run_id=str(values.get("hydro_run_id") or ""),
        published_manifest_id=str(values.get("published_manifest_id") or ""),
        pipeline_job_id=(
            str(values.get("pipeline_job_id")) if values.get("pipeline_job_id") not in (None, "") else None
        ),
        pipeline_event_id=(
            str(values.get("pipeline_event_id")) if values.get("pipeline_event_id") not in (None, "") else None
        ),
    )


def validate_same_production_identity(
    expected: Mapping[str, Any] | ProductionIdentity,
    actual: Mapping[str, Any] | ProductionIdentity,
) -> ProductionIdentity:
    expected_identity = production_identity_from_payload(expected)
    actual_identity = production_identity_from_payload(actual)
    expected_values = expected_identity.to_dict()
    actual_values = actual_identity.to_dict()
    for field in PRODUCTION_IDENTITY_FIELDS:
        if actual_values.get(field) != expected_values.get(field):
            raise ProductionContractError(
                "PRODUCTION_IDENTITY_MISMATCH",
                f"Production identity field {field} does not match.",
                field=field,
                expected=expected_values.get(field),
                actual=actual_values.get(field),
            )
    for field in PRODUCTION_EVIDENCE_CORRELATION_FIELDS:
        expected_value = expected_values.get(field)
        actual_value = actual_values.get(field)
        if expected_value not in (None, "") and actual_value not in (None, "") and actual_value != expected_value:
            raise ProductionContractError(
                "PRODUCTION_IDENTITY_MISMATCH",
                f"Production evidence correlation field {field} does not match.",
                field=field,
                expected=expected_value,
                actual=actual_value,
            )
    return actual_identity


def validate_compatible_production_identity(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    fields: Sequence[str] = PRODUCTION_IDENTITY_FIELDS,
    correlation_fields: Sequence[str] = PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
) -> dict[str, Any]:
    expected_values = _identity_values(expected)
    actual_values = _identity_values(actual)
    compared: dict[str, Any] = {}
    for field in fields:
        actual_value = actual_values.get(field)
        expected_value = expected_values.get(field)
        if actual_value in (None, "") or expected_value in (None, ""):
            continue
        compared[field] = actual_value
        if actual_value != expected_value:
            raise ProductionContractError(
                "PRODUCTION_IDENTITY_MISMATCH",
                f"Production identity field {field} does not match.",
                field=field,
                expected=expected_value,
                actual=actual_value,
            )
    for field in correlation_fields:
        actual_value = actual_values.get(field)
        expected_value = expected_values.get(field)
        if actual_value in (None, "") or expected_value in (None, ""):
            continue
        compared[field] = actual_value
        if actual_value != expected_value:
            raise ProductionContractError(
                "PRODUCTION_IDENTITY_MISMATCH",
                f"Production evidence correlation field {field} does not match.",
                field=field,
                expected=expected_value,
                actual=actual_value,
            )
    return compared


def production_stage_for(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "download_gfs": "download",
        "download_source_cycle": "download",
        "raw_download": "download",
        "canonical": "convert",
        "convert_canonical": "convert",
        "canonical_convert": "convert",
        "produce_forcing": "forcing",
        "produce_forcing_array": "forcing",
        "run_shud_forecast": "forecast",
        "run_shud_forecast_array": "forecast",
        "analysis_run": "forecast",
        "parse_output": "parse",
        "parse_output_array": "parse",
        "publish": "q_down_publish",
        "publish_tiles": "q_down_publish",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in PRODUCTION_STAGE_TAXONOMY else "production_run"


def production_status_for(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "queued": "pending",
        "submitted": "running",
        "downloading": "running",
        "forecast_running": "running",
        "raw_complete": "ready",
        "canonical_ready": "ready",
        "forcing_ready": "ready",
        "created": "pending",
        "staged": "pending",
        "complete": "succeeded",
        "published": "succeeded",
        "parsed": "succeeded",
        "skipped": "superseded",
        "skip": "superseded",
        "not_run": "unavailable",
        "planned": "pending",
        "lock_contended": "blocked",
        "partially_failed": "partial",
        "parsed_partial": "partial",
        "forcing_ready_partial": "partial",
        "preflight_blocked": "blocked",
        "resource_limit_blocked": "blocked",
        "submission_failed": "failed",
        "permanently_failed": "failed",
        "failed_download": "failed",
        "failed_convert": "failed",
        "failed_forcing": "failed",
        "failed_run": "failed",
        "failed_parse": "failed",
        "failed_publish": "failed",
        "source_cycle_unavailable": "unavailable",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in PRODUCTION_STATUS_TAXONOMY else "failed"


def validate_display_artifact_evidence(
    evidence: Mapping[str, Any],
    expected_identity: Mapping[str, Any] | ProductionIdentity,
    *,
    uri_field: str | None = None,
    published_root: Path | str | None = None,
    allowed_published_roots: Sequence[Path | str] = (),
    allowed_s3_bucket: str | None = None,
    allowed_s3_prefix: str = "",
    require_run_id_in_uri: bool = True,
) -> dict[str, Any]:
    identity = validate_same_production_identity(expected_identity, evidence)
    uri = _artifact_uri_from_evidence(evidence, uri_field=uri_field)
    boundary = validate_display_readable_uri(
        uri,
        published_root=published_root,
        allowed_published_roots=allowed_published_roots,
        allowed_s3_bucket=allowed_s3_bucket,
        allowed_s3_prefix=allowed_s3_prefix,
    )
    if require_run_id_in_uri and not _uri_has_exact_path_segment(boundary, identity.run_id):
        raise ProductionContractError(
            "DISPLAY_URI_IDENTITY_MISMATCH",
            "Display-readable artifact URI is not bound to the production run_id.",
            field="uri",
            expected=identity.run_id,
            actual=_safe_uri_summary(str(boundary["normalized_uri"])),
        )
    return {
        "schema_version": PRODUCTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": PRODUCTION_CONTRACT_ID,
        "identity": identity.to_dict(),
        "uri_boundary": boundary,
        "display_readable": True,
    }


def validate_display_readable_uri(
    uri: Any,
    *,
    published_root: Path | str | None = None,
    allowed_published_roots: Sequence[Path | str] = (),
    allowed_s3_bucket: str | None = None,
    allowed_s3_prefix: str = "",
    uri_prefix: str = DEFAULT_PUBLISHED_URI_PREFIX,
) -> dict[str, Any]:
    raw_uri = str(uri or "").strip()
    if not raw_uri:
        raise ProductionContractError("DISPLAY_URI_MISSING", "Display-readable URI is missing.", field="uri")
    parsed = _reject_control_or_credential_uri(raw_uri)
    if raw_uri.startswith(uri_prefix) or parsed.scheme == "published":
        return _published_uri_boundary(raw_uri, uri_prefix=uri_prefix)
    if raw_uri.startswith("file://"):
        return _file_uri_boundary(
            raw_uri,
            published_root=published_root,
            allowed_published_roots=allowed_published_roots,
        )
    if raw_uri.startswith("s3://"):
        return _s3_uri_boundary(raw_uri, allowed_bucket=allowed_s3_bucket, allowed_prefix=allowed_s3_prefix)
    if _LOCAL_URI_SCHEME_RE.match(raw_uri):
        raise ProductionContractError(
            "DISPLAY_URI_UNSUPPORTED_SCHEME",
            "Display-readable artifact URI scheme is unsupported.",
            field="uri",
            actual=_safe_uri_summary(raw_uri),
        )
    path = Path(raw_uri)
    if not path.is_absolute():
        raise ProductionContractError(
            "DISPLAY_URI_RELATIVE_LOCAL_PATH",
            "Display-readable artifact URI must be published:// or an allowlisted absolute published path.",
            field="uri",
            actual=_safe_uri_summary(raw_uri),
        )
    return _local_path_boundary(
        path,
        raw_uri=raw_uri,
        published_root=published_root,
        allowed_published_roots=allowed_published_roots,
    )


def _identity_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field, aliases in _IDENTITY_ALIASES.items():
        value = _first_nested_value(payload, aliases)
        if value in (None, "") and field == "hydro_run_id":
            value = _first_nested_value(payload, _IDENTITY_ALIASES["run_id"])
        if value in (None, ""):
            values[field] = None
            continue
        if field == "source":
            values[field] = _normalize_source(value)
        elif field == "cycle_time":
            values[field] = _normalize_cycle_time(value)
        else:
            values[field] = _clean_text(value)
    return values


def _first_nested_value(payload: Mapping[str, Any], aliases: Sequence[tuple[str, ...]]) -> Any:
    for path in aliases:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


def _normalize_source(value: Any) -> str:
    raw = _clean_text(value)
    try:
        return normalize_source_id(raw)
    except ValueError:
        return raw


def _normalize_cycle_time(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc(value)
    raw = _clean_text(value)
    try:
        return _format_utc(parse_cycle_time(raw))
    except ValueError:
        try:
            return _format_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ProductionContractError(
                "PRODUCTION_IDENTITY_INVALID_CYCLE_TIME",
                "Production identity cycle_time is not a valid UTC timestamp.",
                field="cycle_time",
                actual=raw,
            ) from exc


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ProductionContractError(
            "PRODUCTION_IDENTITY_BLANK_FIELD",
            "Production identity field is blank.",
            actual=str(value),
        )
    return text


def _artifact_uri_from_evidence(evidence: Mapping[str, Any], *, uri_field: str | None) -> str:
    paths: tuple[tuple[str, ...], ...]
    if uri_field is not None:
        paths = tuple((part,) for part in (uri_field,))
    else:
        paths = (
            ("uri",),
            ("artifact_uri",),
            ("display_uri",),
            ("published_uri",),
            ("log_uri",),
            ("manifest_uri",),
            ("published_manifest_uri",),
            ("outputs", "log_uri"),
            ("outputs", "run_manifest_uri"),
            ("published_manifest", "uri"),
        )
    value = _first_nested_value(evidence, paths)
    if value in (None, ""):
        raise ProductionContractError("DISPLAY_URI_MISSING", "Display-readable artifact evidence has no URI.")
    return str(value)


def _published_uri_boundary(uri: str, *, uri_prefix: str) -> dict[str, Any]:
    parsed = _urlsplit_or_contract_error(uri)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Published artifact URI must not include credentials, query strings, or fragments.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    if uri.startswith(uri_prefix):
        namespace = uri.removeprefix(uri_prefix).lstrip("/")
    elif parsed.scheme == "published":
        namespace = f"{parsed.netloc}/{parsed.path.lstrip('/')}" if parsed.netloc else parsed.path.lstrip("/")
    else:
        raise ProductionContractError(
            "DISPLAY_URI_UNSUPPORTED_SCHEME",
            "Published artifact URI prefix is unsupported.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    relative = _safe_relative_public_path(namespace)
    return {
        "kind": "published",
        "normalized_uri": f"{_published_uri_prefix(uri_prefix)}{relative}",
        "relative_path": relative,
        "display_readable": True,
    }


def _file_uri_boundary(
    uri: str,
    *,
    published_root: Path | str | None,
    allowed_published_roots: Sequence[Path | str],
) -> dict[str, Any]:
    parsed = _urlsplit_or_contract_error(uri)
    if parsed.netloc not in {"", "localhost"}:
        raise ProductionContractError(
            "DISPLAY_URI_UNSUPPORTED_FILE_HOST",
            "File artifact URI host is unsupported.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    if not parsed.path.startswith("/"):
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "File artifact URI must contain an absolute path.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    return _local_path_boundary(
        Path(_safe_decoded_path(parsed.path)),
        raw_uri=uri,
        published_root=published_root,
        allowed_published_roots=allowed_published_roots,
    )


def _local_path_boundary(
    path: Path,
    *,
    raw_uri: str,
    published_root: Path | str | None,
    allowed_published_roots: Sequence[Path | str],
) -> dict[str, Any]:
    decoded_path = Path(_safe_decoded_path(str(path)))
    private_reason = _private_local_path_reason(decoded_path)
    if private_reason is not None:
        raise _private_local_path_error(raw_uri, private_reason)
    roots = _published_roots(published_root, allowed_published_roots)
    for root in roots:
        if _path_is_relative_to(decoded_path, root):
            private_root_reason = _private_local_path_reason(root)
            if private_root_reason is not None:
                raise _private_local_path_error(raw_uri, private_root_reason)
            relative = _absolute_path(decoded_path).relative_to(_absolute_path(root)).as_posix()
            _safe_relative_public_path(relative)
            return {
                "kind": "published_root_file",
                "normalized_uri": _absolute_path(decoded_path).as_uri(),
                "published_root": str(_absolute_path(root)),
                "relative_path": relative,
                "display_readable": True,
            }
    raise ProductionContractError(
        "DISPLAY_URI_NOT_ALLOWLISTED",
        "Local artifact path is outside the allowlisted published roots.",
        field="uri",
        actual=_safe_uri_summary(raw_uri),
    )


def _s3_uri_boundary(uri: str, *, allowed_bucket: str | None, allowed_prefix: str) -> dict[str, Any]:
    parsed = _urlsplit_or_contract_error(uri)
    bucket = parsed.netloc
    if not bucket:
        raise ProductionContractError("DISPLAY_URI_MALFORMED", "S3 artifact URI is missing a bucket.", field="uri")
    key = _safe_relative_public_path(parsed.path.lstrip("/"))
    prefix = allowed_prefix.strip("/")
    if allowed_bucket is None or bucket != allowed_bucket:
        raise ProductionContractError(
            "DISPLAY_URI_NOT_ALLOWLISTED",
            "S3 artifact URI is outside the published artifact allowlist.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    if prefix:
        allowed = key == prefix or key.startswith(f"{prefix}/")
    else:
        allowed = key.split("/", maxsplit=1)[0] in _PUBLIC_S3_PREFIXES
    if not allowed:
        raise ProductionContractError(
            "DISPLAY_URI_NOT_ALLOWLISTED",
            "S3 artifact URI key is outside the published artifact prefix.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    return {
        "kind": "published_s3",
        "normalized_uri": f"s3://{bucket}/{key}",
        "bucket": bucket,
        "key": key,
        "display_readable": True,
    }


def _safe_relative_public_path(raw_path: str) -> str:
    decoded = _safe_decoded_path(raw_path)
    if decoded.startswith("/"):
        raise ProductionContractError(
            "DISPLAY_URI_TRAVERSAL",
            "Published artifact URI path must be relative.",
            field="uri",
            actual="[redacted]",
        )
    parts = PurePosixPath(decoded).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProductionContractError(
            "DISPLAY_URI_TRAVERSAL",
            "Published artifact URI path contains unsafe components.",
            field="uri",
            actual="[redacted]",
        )
    for part in parts:
        if _CREDENTIAL_WORD_RE.search(part):
            raise ProductionContractError(
                "DISPLAY_URI_CREDENTIAL_COMPONENT",
                "Published artifact URI path contains credential-like components.",
                field="uri",
                actual="[redacted]",
            )
        if not _SAFE_PUBLIC_SEGMENT_RE.fullmatch(part):
            raise ProductionContractError(
                "DISPLAY_URI_MALFORMED",
                "Published artifact URI path contains unsupported characters.",
                field="uri",
                actual="[redacted]",
            )
    return "/".join(parts)


def _published_uri_prefix(uri_prefix: str) -> str:
    prefix = str(uri_prefix or DEFAULT_PUBLISHED_URI_PREFIX).strip()
    if prefix == "published:":
        return DEFAULT_PUBLISHED_URI_PREFIX
    if prefix.endswith("://"):
        return prefix
    if prefix.startswith("published://"):
        return prefix.rstrip("/") + "/"
    return prefix.rstrip("/") + "/"


def _uri_has_exact_path_segment(boundary: Mapping[str, Any], expected_segment: str) -> bool:
    if expected_segment in (None, ""):
        return False
    segment = str(expected_segment)
    path_value = boundary.get("relative_path") or boundary.get("key")
    if path_value not in (None, ""):
        parts = PurePosixPath(str(path_value)).parts
        return segment in parts
    normalized = str(boundary.get("normalized_uri") or "")
    try:
        parsed = _urlsplit_or_contract_error(normalized)
    except ProductionContractError:
        return False
    path = f"{parsed.netloc}/{parsed.path.lstrip('/')}" if parsed.scheme == "published" else parsed.path
    try:
        return segment in PurePosixPath(_safe_decoded_path(path)).parts
    except ProductionContractError:
        return False


def _safe_decoded_path(raw_path: str) -> str:
    if "\\" in raw_path or _ENCODED_FORBIDDEN_RE.search(raw_path):
        raise ProductionContractError(
            "DISPLAY_URI_TRAVERSAL",
            "Artifact URI path contains unsafe separators or traversal.",
            field="uri",
            actual="[redacted]",
        )
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Artifact URI path contains malformed characters.",
            field="uri",
            actual="[redacted]",
        )
    if any(part in {".", ".."} for part in PurePosixPath(decoded).parts):
        raise ProductionContractError(
            "DISPLAY_URI_TRAVERSAL",
            "Artifact URI path contains unsafe components.",
            field="uri",
            actual="[redacted]",
        )
    return decoded


def _reject_control_or_credential_uri(uri: str) -> SplitResult:
    if any(ord(character) < 32 or ord(character) == 127 for character in uri):
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Artifact URI contains control characters.",
            field="uri",
            actual="[redacted]",
        )
    parsed = _urlsplit_or_contract_error(uri)
    _port_or_contract_error(parsed)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Artifact URI must not include credentials, query strings, or fragments.",
            field="uri",
            actual=_safe_uri_summary(uri),
        )
    return parsed


def _published_roots(
    published_root: Path | str | None,
    allowed_published_roots: Sequence[Path | str],
) -> tuple[Path, ...]:
    roots = []
    if published_root is not None:
        roots.append(Path(published_root))
    roots.extend(Path(root) for root in allowed_published_roots)
    return tuple(_absolute_path(root) for root in roots)


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        _absolute_path(path).resolve(strict=False).relative_to(_absolute_path(root).resolve(strict=False))
    except ValueError:
        return False
    return True


def _private_local_path_reason(path: Path) -> str | None:
    absolute = _absolute_path(path)
    paths = (absolute, absolute.resolve(strict=False))
    for candidate in paths:
        reason = _private_local_path_candidate_reason(candidate)
        if reason is not None:
            return reason
    return None


def _private_local_path_candidate_reason(path: Path) -> str | None:
    normalized = path.as_posix()
    parts = tuple(part.lower() for part in PurePosixPath(normalized).parts)
    if ".nhms-workspace" in parts or ".nhms-runs" in parts or "workspace" in parts:
        return "workspace_private_path"
    if normalized == "/scratch" or normalized.startswith("/scratch/"):
        return "scratch_private_path"
    if normalized.startswith("/var/spool/slurm") or normalized.startswith("/var/log/slurm"):
        return "slurm_private_path"
    if "slurm" in parts or "sbatch" in parts:
        return "slurm_private_path"
    return None


def _private_local_path_error(raw_uri: str, reason: str) -> ProductionContractError:
    return ProductionContractError(
        "DISPLAY_URI_PRIVATE_COMPUTE_PATH",
        "Private compute workspace paths are outside the display-readable artifact boundary.",
        field="uri",
        actual=_safe_uri_summary(raw_uri),
        details={"reason": reason},
    )


def _safe_uri_summary(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return "[redacted]"
    if parsed.scheme == "file":
        return "file://[redacted]"
    if parsed.username or parsed.password:
        if parsed.scheme:
            return f"{parsed.scheme}://[redacted]"
        return "[redacted]"
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        try:
            parsed_port = parsed.port
        except ValueError:
            return f"{parsed.scheme}://[redacted]"
        port = f":{parsed_port}" if parsed_port is not None else ""
        return f"{parsed.scheme}://{host}{port}/[redacted]"
    if parsed.scheme:
        return f"{parsed.scheme}://[redacted]"
    if Path(uri).is_absolute():
        return "[local-path-redacted]"
    if _CREDENTIAL_WORD_RE.search(uri):
        return "[redacted]"
    return "[relative-path-redacted]"


def _urlsplit_or_contract_error(uri: str) -> SplitResult:
    try:
        return urlsplit(uri)
    except ValueError as exc:
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Display-readable artifact URI is malformed.",
            field="uri",
            actual="[redacted]",
        ) from exc


def _port_or_contract_error(parsed: SplitResult) -> int | None:
    try:
        return parsed.port
    except ValueError as exc:
        raise ProductionContractError(
            "DISPLAY_URI_MALFORMED",
            "Display-readable artifact URI port is malformed.",
            field="uri",
            actual=f"{parsed.scheme}://[redacted]" if parsed.scheme else "[redacted]",
        ) from exc
