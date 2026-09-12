"""Post-target catalog observer. Does not reuse the pre-target census CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    ALLOWED_HYPERTABLES,
    compute_cutoff,
    json_ready,
    recompressed_group_is_complete,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    collect_residency_group,
    compute_window_parity,
    derive_bound_inventories,
    load_catalog_chunk,
    ranked_candidates_from_execute,
    snapshot_group,
    window_parity_from_dict,
)
from packages.common.display_watermark import fetch_display_watermark
from packages.common.node27_issue1895_private_receipt import read_held_private_json
from packages.common.node27_issue1895_receipt import DURABLE_FIELDS, durable_key
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from scripts.node27_cold_residency_census import close_observer_connection, open_readonly_connection

Execute = Callable[..., Sequence[Mapping[str, Any]]]
REQUIRE_COUNT = 6
ARTIFACT = "nhms-issue1895-post-target-observe"
MAX_BYTES = 262144


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def durable_from_mapping(durable: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in DURABLE_FIELDS if field not in durable]
    if missing:
        raise Issue1895ReadinessError(
            "durable identity is incomplete",
            code="POST_TARGET_DURABLE_INVALID",
            stage="post-target",
        )
    payload = {field: durable[field] for field in DURABLE_FIELDS}
    for field in ("range_start", "range_end"):
        value = payload[field]
        if isinstance(value, datetime):
            payload[field] = iso_utc(value)
    return payload


def _binder(connection: Any) -> Execute:
    def execute(sql: str, params: object = None) -> list[Mapping[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.description is None:
                return []
            names = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [
            dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
            for row in rows
        ]

    return execute


_PARITY_FIELDS = (
    "row_count",
    "non_null_counts",
    "checksum",
    "inventory_digest",
    "range_start",
    "range_end",
)


def _raise_parity_invalid(message: str) -> None:
    raise Issue1895ReadinessError(message, code="POST_TARGET_PARITY_INVALID", stage="post-target")


def require_baseline_window_parity(value: object) -> dict[str, Any]:
    if value is None:
        raise Issue1895ReadinessError(
            "baseline parity is missing",
            code="POST_TARGET_PARITY_MISSING",
            stage="post-target",
        )
    if not isinstance(value, Mapping):
        _raise_parity_invalid("baseline parity is not an object")
    missing = [field for field in _PARITY_FIELDS if field not in value]
    if missing:
        _raise_parity_invalid("baseline parity is missing required fields")
    extra = [field for field in value if field not in _PARITY_FIELDS]
    if extra:
        _raise_parity_invalid("baseline parity has extra fields")
    row_count = value["row_count"]
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        _raise_parity_invalid("baseline parity row_count is invalid")
    counts = value["non_null_counts"]
    if not isinstance(counts, Mapping):
        _raise_parity_invalid("baseline parity non_null_counts is invalid")
    for name, count in counts.items():
        if not isinstance(name, str) or not name:
            _raise_parity_invalid("baseline parity non_null_counts keys are invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _raise_parity_invalid("baseline parity non_null_counts values are invalid")
        if count > row_count:
            _raise_parity_invalid("baseline parity non_null_counts exceed row_count")
    checksum = value["checksum"]
    digest = value["inventory_digest"]
    if not isinstance(checksum, str) or not checksum:
        _raise_parity_invalid("baseline parity checksum is invalid")
    if not isinstance(digest, str) or not digest:
        _raise_parity_invalid("baseline parity inventory_digest is invalid")
    range_start = value["range_start"]
    range_end = value["range_end"]
    if not isinstance(range_start, str) or not isinstance(range_end, str):
        _raise_parity_invalid("baseline parity window is invalid")
    try:
        parsed = window_parity_from_dict(
            {
                "row_count": row_count,
                "non_null_counts": dict(counts),
                "checksum": checksum,
                "inventory_digest": digest,
                "range_start": range_start,
                "range_end": range_end,
            }
        )
    except Exception as error:
        raise Issue1895ReadinessError(
            "baseline parity is invalid",
            code="POST_TARGET_PARITY_INVALID",
            stage="post-target",
        ) from error
    if parsed.range_start >= parsed.range_end:
        _raise_parity_invalid("baseline parity window is invalid")
    return parsed.as_dict()


def observe_named_group(
    execute: Execute,
    *,
    durable: Mapping[str, Any],
    inventories: BoundInventories,
    expected_parity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = durable_from_mapping(durable)
    chunk = load_catalog_chunk(
        execute,
        hypertable_schema=str(identity["hypertable_schema"]),
        hypertable_name=str(identity["hypertable_name"]),
        origin_schema=str(identity["origin_schema"]),
        origin_name=str(identity["origin_name"]),
    )
    if int(chunk.origin_oid) != int(identity["origin_oid"]):
        raise Issue1895ReadinessError(
            "origin OID drifted for a durable key",
            code="POST_TARGET_ORIGIN_DRIFT",
            stage="post-target",
        )
    if iso_utc(chunk.range_start) != identity["range_start"] or iso_utc(chunk.range_end) != identity["range_end"]:
        raise Issue1895ReadinessError(
            "window identity drifted for a durable key",
            code="POST_TARGET_WINDOW_DRIFT",
            stage="post-target",
        )
    group = collect_residency_group(execute, chunk)
    snapshot = snapshot_group(group)
    if not recompressed_group_is_complete(group):
        raise Issue1895ReadinessError(
            "durable key is not a complete compressed target group",
            code="POST_TARGET_INCOMPLETE",
            stage="post-target",
        )
    if snapshot.get("residency") not in {"already_target", "complete_target"}:
        raise Issue1895ReadinessError(
            "durable key members are not fully nhms_cold",
            code="POST_TARGET_RESIDENCY",
            stage="post-target",
        )
    inventory = inventories.for_hypertable(chunk.hypertable_schema, chunk.hypertable_name)
    parity = compute_window_parity(execute, inventory, chunk).as_dict()
    if expected_parity is not None:
        required = require_baseline_window_parity(expected_parity)
        if json_ready(parity) != json_ready(required):
            raise Issue1895ReadinessError(
                "business-window parity changed",
                code="POST_TARGET_PARITY_DRIFT",
                stage="post-target",
            )
    return {
        "key": durable_key(identity),
        "durable": identity,
        "residency": snapshot.get("residency"),
        "compressed": snapshot.get("compressed"),
        "members": snapshot.get("members"),
        "parity": parity,
        "inventory_digest": inventory.digest,
        "complete_target": True,
    }


def classify_current_candidates(
    execute: Execute,
    *,
    cutoff: datetime,
    inventories: BoundInventories,
    per_table_limit: int = 64,
    max_catalog_bytes: int = 16 * 1024**2,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ranked = ranked_candidates_from_execute(
        execute,
        cutoff=cutoff,
        per_table_limit=per_table_limit,
        max_catalog_bytes=max_catalog_bytes,
    )
    complete_source: list[str] = []
    complete_target: list[str] = []
    for _rank, _end, _schema, _name, _oid, chunk in ranked:
        group = collect_residency_group(execute, chunk)
        snapshot = snapshot_group(group)
        identity = durable_from_mapping(snapshot["durable"])
        key = durable_key(identity)
        residency = snapshot.get("residency")
        if residency == "all_source":
            complete_source.append(key)
        elif recompressed_group_is_complete(group):
            complete_target.append(key)
        else:
            raise Issue1895ReadinessError(
                "eligible group is mixed or incomplete",
                code="POST_TARGET_MIXED",
                stage="post-target",
            )
    return tuple(dict.fromkeys(complete_source)), tuple(dict.fromkeys(complete_target))


def observe_post_target(
    *,
    baseline_groups: Sequence[Mapping[str, Any]],
    execute: Execute,
    cutoff: datetime,
    watermark: datetime,
    lag_seconds: int,
    reviewed_sha: str,
) -> dict[str, Any]:
    if len(baseline_groups) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "baseline is not exactly six durable groups",
            code="POST_TARGET_BASELINE_COUNT",
            stage="post-target",
        )
    validated: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for group in baseline_groups:
        if not isinstance(group, Mapping):
            raise Issue1895ReadinessError(
                "baseline group is not an object",
                code="POST_TARGET_BASELINE_INVALID",
                stage="post-target",
            )
        durable = group.get("durable")
        if not isinstance(durable, Mapping):
            raise Issue1895ReadinessError(
                "baseline durable identity is missing",
                code="POST_TARGET_DURABLE_INVALID",
                stage="post-target",
            )
        if "parity" not in group:
            raise Issue1895ReadinessError(
                "baseline parity is missing",
                code="POST_TARGET_PARITY_MISSING",
                stage="post-target",
            )
        validated.append((durable, require_baseline_window_parity(group.get("parity"))))
    inventories = derive_bound_inventories(execute)
    observed: list[dict[str, Any]] = []
    for durable, expected_parity in validated:
        observed.append(
            observe_named_group(
                execute,
                durable=durable,
                inventories=inventories,
                expected_parity=expected_parity,
            )
        )
    complete_source, complete_target = classify_current_candidates(
        execute,
        cutoff=cutoff,
        inventories=inventories,
    )
    baseline_keys = tuple(item["key"] for item in observed)
    return {
        "artifact": ARTIFACT,
        "head_sha": reviewed_sha,
        "watermark": iso_utc(watermark),
        "cutoff": iso_utc(cutoff),
        "lag_seconds": lag_seconds,
        "baseline_keys": list(baseline_keys),
        "groups": observed,
        "complete_source_keys": list(complete_source),
        "complete_target_keys": list(complete_target),
        "inventories_digest": inventories.digest,
        "hypertables": [f"{schema}.{name}" for schema, name in ALLOWED_HYPERTABLES],
    }


def newly_terminal_keys(*, pre_target_keys: Sequence[str], post_target_keys: Sequence[str]) -> tuple[str, ...]:
    return tuple(key for key in post_target_keys if key not in set(pre_target_keys))


def run_post_target_observation(
    *,
    baseline_path: Path,
    output_path: Path,
    reviewed_sha: str,
    lag_seconds: int,
    connect: Callable[[str], Any] | None = None,
    execute: Execute | None = None,
    watermark: datetime | None = None,
    dsn: str | None = None,
) -> dict[str, Any]:
    try:
        _raw, baseline, _facts = read_held_private_json(
            Path(baseline_path),
            label="post-target baseline",
            stage="post-target",
            unreadable_code="POST_TARGET_BASELINE_INVALID",
            identity_code="POST_TARGET_BASELINE_INVALID",
            toctou_code="POST_TARGET_BASELINE_INVALID",
            json_code="POST_TARGET_BASELINE_INVALID",
        )
    except Issue1895ReadinessError as error:
        if error.code.startswith("READINESS_INPUT_"):
            raise Issue1895ReadinessError(
                "baseline artifact is invalid",
                code="POST_TARGET_BASELINE_INVALID",
                stage="post-target",
            ) from error
        raise
    if not isinstance(baseline, dict) or not isinstance(baseline.get("groups"), list):
        raise Issue1895ReadinessError(
            "baseline artifact is invalid",
            code="POST_TARGET_BASELINE_INVALID",
            stage="post-target",
        )
    owned = None
    try:
        live_execute = execute
        if live_execute is None:
            if not dsn:
                raise Issue1895ReadinessError(
                    "readonly DSN provenance is missing",
                    code="DSN_MISSING",
                    stage="post-target",
                )
            opener = connect if connect is not None else open_readonly_connection
            owned = opener(dsn)
            live_execute = _binder(owned)
        live_watermark = watermark
        if live_watermark is None:
            live_watermark = fetch_display_watermark(str(dsn or ""), connect=connect)
        cutoff = compute_cutoff(live_watermark, lag_seconds)
        document = observe_post_target(
            baseline_groups=baseline["groups"],
            execute=live_execute,
            cutoff=cutoff,
            watermark=live_watermark,
            lag_seconds=lag_seconds,
            reviewed_sha=reviewed_sha,
        )
        publish_post_target(output_path, document)
        return document
    finally:
        if owned is not None:
            close_observer_connection(owned)


def publish_post_target(path: Path, document: Mapping[str, Any]) -> None:
    from packages.common.node27_issue1895_performance_live import publish_performance_receipt

    publish_performance_receipt(path, document)
