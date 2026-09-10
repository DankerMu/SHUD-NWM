"""G7 GFS/IFS complete-cycle / public-product proof, identity-only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.source_identity import normalize_source_id
from services.orchestrator.scheduler_file_providers import _registry_readiness_identities_by_source

CANONICAL_SOURCES: tuple[str, ...] = ("gfs", "IFS")
COMPLETE_STATUSES: frozenset[str] = frozenset({"succeeded", "parsed", "published"})
IDENTITY_ONLY_FIELDS: tuple[str, ...] = (
    "source_id",
    "run_id",
    "cycle_time",
    "model_id",
    "basin_id",
)
FORBIDDEN_BODY_FIELDS: frozenset[str] = frozenset(
    {"series", "values", "headers", "river_series", "station_series", "coverage"}
)
COMPLETE_RUN_SQL = (
    "SELECT hr.source_id, hr.cycle_time, hr.status AS run_status, hr.model_id, bv.basin_id "
    "FROM hydro.hydro_run hr "
    "JOIN core.model_instance mi ON mi.model_id = hr.model_id "
    "JOIN core.basin_version bv ON bv.basin_version_id = hr.basin_version_id "
    "WHERE hr.run_type = 'forecast' "
    "AND hr.status IN ('succeeded','parsed','published') "
    "AND hr.source_id = %s AND hr.cycle_time = %s "
    "ORDER BY hr.model_id, bv.basin_id"
)


def utc_instant(value: object) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                raise Issue1895ReadinessError(
                    "publication instant is empty",
                    code="PUBLICATION_CYCLE_MISSING",
                    stage="publication",
                )
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(UTC)
    except Issue1895ReadinessError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise Issue1895ReadinessError(
            "publication instant is not calendar-valid and timezone-aware",
            code="PUBLICATION_CYCLE_INVALID",
            stage="publication",
        ) from None


def iso_utc(value: object) -> str:
    return utc_instant(value).isoformat().replace("+00:00", "Z")


def registry_expected_identities(
    registry_models: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[str] = ("gfs", "IFS"),
) -> dict[str, frozenset[tuple[str, str]]]:
    normalized_sources = tuple(dict.fromkeys(normalize_source_id(str(source)) for source in sources))
    projected = _registry_readiness_identities_by_source(registry_models, sources=normalized_sources)
    return {
        normalize_source_id(str(source)): frozenset((str(model_id), str(basin_id)) for model_id, basin_id in identities)
        for source, identities in projected.items()
    }


def _require_mapping(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Issue1895ReadinessError("publication payload is not an object", code=code, stage="publication")
    return value


def bind_source_product(
    product: Mapping[str, Any],
    *,
    expected_source: str,
) -> dict[str, Any]:
    document = _require_mapping(product, code="PUBLICATION_PRODUCT_INVALID")
    forbidden = sorted(field for field in FORBIDDEN_BODY_FIELDS if field in document)
    if forbidden:
        raise Issue1895ReadinessError(
            "publication proof read a river-series body",
            code="PUBLICATION_BODY_FORBIDDEN",
            stage="publication",
        )
    missing = [field for field in IDENTITY_ONLY_FIELDS if not str(document.get(field) or "")]
    if missing:
        raise Issue1895ReadinessError(
            "identity-only product is missing required fields",
            code="PUBLICATION_IDENTITY_INCOMPLETE",
            stage="publication",
        )
    cycle_status = str(document.get("run_status") or document.get("status") or "")
    try:
        normalized = normalize_source_id(str(document["source_id"]))
        expected = normalize_source_id(expected_source)
    except ValueError:
        raise Issue1895ReadinessError(
            "publication source_id is not a canonical source",
            code="PUBLICATION_SOURCE_INVALID",
            stage="publication",
        ) from None
    if normalized != expected:
        raise Issue1895ReadinessError(
            "publication source_id does not bind the requested source",
            code="PUBLICATION_SOURCE_MISMATCH",
            stage="publication",
        )
    if cycle_status not in COMPLETE_STATUSES:
        raise Issue1895ReadinessError(
            "publication cycle is not complete",
            code="PUBLICATION_CYCLE_INCOMPLETE",
            stage="publication",
        )
    cycle_time = iso_utc(document["cycle_time"])
    return {
        "source_id": normalized,
        "run_id": str(document["run_id"]),
        "cycle_time": cycle_time,
        "model_id": str(document["model_id"]),
        "basin_id": str(document["basin_id"]),
        "status": cycle_status,
    }


def observed_identity_set(
    *,
    source_id: str,
    cycle_time: str,
    rows: Sequence[Mapping[str, Any]],
) -> frozenset[tuple[str, str]]:
    expected = normalize_source_id(source_id)
    expected_cycle = utc_instant(cycle_time)
    identities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        document = _require_mapping(row, code="PUBLICATION_ROW_INVALID")
        forbidden = sorted(field for field in FORBIDDEN_BODY_FIELDS if field in document)
        if forbidden:
            raise Issue1895ReadinessError(
                "publication proof read a river-series body",
                code="PUBLICATION_BODY_FORBIDDEN",
                stage="publication",
            )
        try:
            row_source = normalize_source_id(str(document.get("source_id")))
        except ValueError:
            raise Issue1895ReadinessError(
                "publication row source_id is not canonical",
                code="PUBLICATION_SOURCE_INVALID",
                stage="publication",
            ) from None
        if row_source != expected:
            raise Issue1895ReadinessError(
                "publication row is mixed across sources",
                code="PUBLICATION_SOURCE_MISMATCH",
                stage="publication",
            )
        if utc_instant(document.get("cycle_time")) != expected_cycle:
            raise Issue1895ReadinessError(
                "publication row is not the current cycle",
                code="PUBLICATION_CYCLE_MIXED",
                stage="publication",
            )
        cycle_status = str(document.get("run_status") or document.get("status") or "")
        if cycle_status not in COMPLETE_STATUSES:
            raise Issue1895ReadinessError(
                "publication row is not a complete status",
                code="PUBLICATION_CYCLE_INCOMPLETE",
                stage="publication",
            )
        pair = (str(document.get("model_id") or ""), str(document.get("basin_id") or ""))
        if not pair[0] or not pair[1]:
            raise Issue1895ReadinessError(
                "publication row identity is incomplete",
                code="PUBLICATION_IDENTITY_INCOMPLETE",
                stage="publication",
            )
        if pair in seen:
            raise Issue1895ReadinessError(
                "publication row identity is duplicated",
                code="PUBLICATION_IDENTITY_DUPLICATE",
                stage="publication",
            )
        seen.add(pair)
        identities.append(pair)
    return frozenset(identities)


def assert_complete_cycle_identities(
    *,
    source_id: str,
    cycle_time: str,
    rows: Sequence[Mapping[str, Any]],
    expected_identities: Sequence[tuple[str, str]] | frozenset[tuple[str, str]],
) -> int:
    expected = frozenset((str(model_id), str(basin_id)) for model_id, basin_id in expected_identities)
    if not expected:
        raise Issue1895ReadinessError(
            "registry expected identity set is empty",
            code="PUBLICATION_EXPECTED_EMPTY",
            stage="publication",
        )
    observed = observed_identity_set(source_id=source_id, cycle_time=cycle_time, rows=rows)
    extra = observed - expected
    missing = expected - observed
    if extra or missing:
        raise Issue1895ReadinessError(
            "current complete-cycle identity set drifted from the registry",
            code="PUBLICATION_IDENTITY_MISMATCH",
            stage="publication",
        )
    return len(observed)


def assert_valid_time_frontier(
    *,
    current: Sequence[object],
    baseline: Sequence[object],
) -> None:
    if not current:
        raise Issue1895ReadinessError(
            "public valid-times is empty",
            code="VALID_TIMES_EMPTY",
            stage="publication",
        )
    if not baseline:
        raise Issue1895ReadinessError(
            "valid-times baseline is empty",
            code="VALID_TIMES_BASELINE_EMPTY",
            stage="publication",
        )
    current_times = [utc_instant(item) for item in current]
    baseline_times = [utc_instant(item) for item in baseline]
    if max(current_times) < max(baseline_times):
        raise Issue1895ReadinessError(
            "valid-time frontier regressed",
            code="VALID_TIMES_REGRESSED",
            stage="publication",
        )


def prove_gfs_ifs_products(
    *,
    gfs: Mapping[str, Any],
    ifs: Mapping[str, Any],
    gfs_rows: Sequence[Mapping[str, Any]],
    ifs_rows: Sequence[Mapping[str, Any]],
    current_valid_times: Sequence[object],
    baseline_valid_times: Sequence[object],
    expected_gfs: Sequence[tuple[str, str]] | frozenset[tuple[str, str]],
    expected_ifs: Sequence[tuple[str, str]] | frozenset[tuple[str, str]],
) -> dict[str, Any]:
    gfs_identity = bind_source_product(gfs, expected_source="gfs")
    ifs_identity = bind_source_product(ifs, expected_source="IFS")
    if gfs_identity["run_id"] == ifs_identity["run_id"] and gfs_identity["source_id"] == ifs_identity["source_id"]:
        raise Issue1895ReadinessError(
            "GFS and IFS identities collapsed",
            code="PUBLICATION_SOURCE_COLLAPSED",
            stage="publication",
        )
    gfs_count = assert_complete_cycle_identities(
        source_id="gfs",
        cycle_time=gfs_identity["cycle_time"],
        rows=gfs_rows,
        expected_identities=expected_gfs,
    )
    ifs_count = assert_complete_cycle_identities(
        source_id="IFS",
        cycle_time=ifs_identity["cycle_time"],
        rows=ifs_rows,
        expected_identities=expected_ifs,
    )
    assert_valid_time_frontier(current=current_valid_times, baseline=baseline_valid_times)
    return {
        "gfs": gfs_identity,
        "ifs": ifs_identity,
        "gfs_count": gfs_count,
        "ifs_count": ifs_count,
    }
