"""File-journal replay ordering and JSON payload decoding.

Replay-order keys, latest-wins merging, journal record shape accessors and
the bounded JSON decoding used when the file orchestration journal folds its
records into a view. The journal's error type and the latest-view replay
sentinel stay in the facade; each function imports them right before the
statement that needs them so the normal path never executes the import.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.chain_source_cycle import _datetime_sort_key
from services.orchestrator.file_journal_primitives import _required_safe_identity, _scalar_text

_REPLAY_SEQUENCE_FIELD = "_file_journal_replay_sequence"
_REPLAY_ORDER_FIELD = "_file_journal_replay_order"
_STAGE_STATUS_ORDER = {
    "download": 1,
    "download_gfs": 1,
    "download_source_cycle": 1,
    "convert": 2,
    "convert_canonical": 2,
    "forcing": 3,
    "produce_forcing": 3,
    "forecast": 4,
    "run_shud_forecast": 4,
    "parse": 5,
    "state_save_qc": 6,
    "publish": 8,
    "era5_download": 11,
    "canonical_convert": 12,
    "forcing_produce": 13,
    "analysis_run": 14,
    "parse_output": 15,
}
_UNKNOWN_STAGE_STATUS_ORDER = 99


def _payload_or_record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    if "payload" in record:
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            return dict(payload)
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field="payload")
    return dict(record)


def _record_type(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    value = record.get("record_type")
    if value in (None, ""):
        value = payload.get("record_type")
    if value in (None, ""):
        return ""
    return _scalar_text(value, field="record_type", invalid_reason="file_journal_invalid_field")


def _record_list(payload: Mapping[str, Any], *keys: str, single_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    single = payload.get(single_key)
    if isinstance(single, Mapping):
        records.append(dict(single))
    elif single not in (None, ""):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field=single_key)
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
                    raise FileOrchestrationJournalError("file_journal_expected_object", field=f"{key}[{index}]")
                records.append(dict(item))
            continue
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_list", field=key)
    return records


def _first_mapping(payload: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if value not in (None, ""):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_expected_object", field=key)
    return None


def _latest_mapping(current: dict[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if incoming is None:
        return current
    if current is None:
        return dict(incoming)
    current_replay_key = _replay_order_key(current)
    incoming_replay_key = _replay_order_key(incoming)
    if current_replay_key is not None or incoming_replay_key is not None:
        if current_replay_key is None:
            return dict(incoming)
        if incoming_replay_key is None:
            return current
        return dict(incoming) if incoming_replay_key >= current_replay_key else current
    current_time = _datetime_sort_key(current.get("updated_at") or current.get("created_at"))
    incoming_time = _datetime_sort_key(incoming.get("updated_at") or incoming.get("created_at"))
    return dict(incoming) if incoming_time >= current_time else current


def _upsert_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    existing = target.get(row_key)
    target[row_key] = _latest_mapping(existing, row) or dict(row)


def _insert_missing_by_key(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, key: str) -> None:
    row_key = _required_safe_identity(row, key)
    target.setdefault(row_key, dict(row))


def _with_replay_order(payload: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    sequence = _optional_replay_sequence(record)
    if sequence is not None:
        row[_REPLAY_SEQUENCE_FIELD] = sequence
    line_order = record.get(_REPLAY_ORDER_FIELD)
    if isinstance(line_order, int):
        row[_REPLAY_ORDER_FIELD] = line_order
    return row


def _with_latest_replay_order(row: Mapping[str, Any], latest_replay_sequence: int | None) -> dict[str, Any]:
    if latest_replay_sequence is None:
        return dict(row)
    payload = dict(row)
    payload[_REPLAY_SEQUENCE_FIELD] = latest_replay_sequence
    from services.orchestrator.file_orchestration_journal import _LATEST_REPLAY_ORDER_SENTINEL
    payload[_REPLAY_ORDER_FIELD] = _LATEST_REPLAY_ORDER_SENTINEL
    return payload


def _latest_replay_sequence(payload: Mapping[str, Any]) -> int | None:
    replay = payload.get("replay")
    if not isinstance(replay, Mapping):
        return None
    value = replay.get("latest_sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(
        value,
        field="replay.latest_sequence",
        invalid_reason="file_journal_invalid_field",
    )
    try:
        return int(text)
    except ValueError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="replay.latest_sequence") from error


def _optional_replay_sequence(record: Mapping[str, Any]) -> int | None:
    value = record.get("sequence")
    if value in (None, ""):
        return None
    text = _scalar_text(value, field="sequence", invalid_reason="file_journal_invalid_field")
    try:
        return int(text)
    except ValueError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_invalid_field", field="sequence") from error


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _replay_order_key(row: Mapping[str, Any]) -> tuple[int, int] | None:
    sequence = row.get(_REPLAY_SEQUENCE_FIELD)
    line_order = row.get(_REPLAY_ORDER_FIELD)
    if not isinstance(sequence, int) and not isinstance(line_order, int):
        return None
    sequence_value = sequence if isinstance(sequence, int) else -1
    line_order_value = line_order if isinstance(line_order, int) else -1
    return sequence_value, line_order_value


def _dedupe_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for event in events:
        key = event.get("event_id")
        if key in (None, ""):
            unkeyed.append(dict(event))
            continue
        keyed[str(key)] = _latest_mapping(keyed.get(str(key)), event) or dict(event)
    return [*keyed.values(), *unkeyed]


def _db_compatible_pipeline_job_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    submitted_at = row.get("submitted_at")
    submitted_missing = submitted_at in (None, "")
    submitted_key = datetime.max.replace(tzinfo=UTC) if submitted_missing else _datetime_sort_key(submitted_at)
    return (
        submitted_missing,
        submitted_key,
        _datetime_sort_key(row.get("created_at")),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
        str(row.get("slurm_job_id") or ""),
    )


def _db_compatible_stage_status_order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    stage = str(row.get("stage") or "")
    return (
        _STAGE_STATUS_ORDER.get(stage, _UNKNOWN_STAGE_STATUS_ORDER),
        stage,
        str(row.get("source_id") or ""),
        str(row.get("cycle_id") or ""),
        str(row.get("model_id") or ""),
        str(row.get("job_id") or ""),
        str(row.get("run_id") or ""),
    )


def _decode_mapping(content: bytes, *, field: str, max_nodes: int, max_depth: int) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_malformed_json",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    _validate_json_complexity(payload, field=field, max_nodes=max_nodes, max_depth=max_depth)
    return dict(payload)


def _decode_mapping_prevalidated(content: bytes, *, field: str) -> dict[str, Any]:
    """Decode bytes whose complexity validation already passed once.

    The complexity limits are a pure function of the bytes, so the graph
    walk is skipped for byte-identical re-reads; decoding still returns
    fresh objects on every call.
    """
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_malformed_json",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_expected_object", field=field)
    return dict(payload)


def _validate_json_complexity(value: Any, *, field: str, max_nodes: int, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > max_nodes:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_json_node_limit_exceeded",
                field=field,
                evidence={"max_nodes": max_nodes},
            )
        if depth > max_depth:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_json_depth_exceeded",
                field=field,
                evidence={"max_depth": max_depth},
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            stack.extend((child, depth + 1) for child in item)


def _require_schema(payload: Mapping[str, Any], expected: str) -> None:
    if payload.get("schema_version") != expected:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_schema_mismatch",
            field="schema_version",
            evidence={"expected": expected, "actual": str(payload.get("schema_version") or "")[:80]},
        )
