from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.orchestrator.chain_types import ForcingContext, ModelContext
from services.orchestrator.scheduler_state import _ensure_utc


def _model_context_from_mapping(row: Mapping[str, Any], *, model_id: str) -> ModelContext:
    from services.orchestrator.file_orchestration_journal import (
        _required_safe_identity,
        _validate_model_context_identity,
    )
    _validate_model_context_identity(row, model_id=model_id)
    return ModelContext(
        model_id=_required_safe_identity(row, "model_id"),
        basin_id=_optional_str(row.get("basin_id"), field="basin_id"),
        basin_version_id=_required_context_str(row, "basin_version_id"),
        river_network_version_id=_required_context_str(row, "river_network_version_id"),
        segment_count=_required_int(row, "segment_count"),
        model_package_uri=_required_context_str(row, "model_package_uri"),
        output_segment_count=_optional_int(row.get("output_segment_count"), field="output_segment_count"),
        model_package_checksum=_optional_str(
            row.get("model_package_checksum") or row.get("package_checksum"),
            field="model_package_checksum",
        ),
    )


def _forcing_context_from_mapping(row: Mapping[str, Any]) -> ForcingContext:
    lineage = _lineage_mapping(row.get("lineage_json"))
    return ForcingContext(
        _optional_str(row.get("forcing_version_id"), field="forcing_version_id"),
        _optional_str(row.get("forcing_package_uri"), field="forcing_package_uri"),
        _optional_datetime(row.get("start_time"), field="start_time"),
        _optional_datetime(row.get("end_time"), field="end_time"),
        _optional_str(row.get("source_id"), field="source_id"),
        _optional_int(
            _fallback_value(row.get("max_lead_hours"), lineage.get("max_lead_hours")),
            field="max_lead_hours",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_uri"),
                lineage.get("forcing_package_manifest_uri"),
            ),
            field="forcing_package_manifest_uri",
        ),
        _optional_str(
            _fallback_value(
                row.get("forcing_package_manifest_checksum"),
                lineage.get("forcing_package_manifest_checksum"),
            ),
            field="forcing_package_manifest_checksum",
        ),
    )


def _fallback_value(primary: Any, fallback: Any) -> Any:
    return fallback if primary in (None, "") else primary


def _lineage_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return value
    return {}


def _optional_str(value: Any, *, field: str = "value") -> str | None:
    from services.orchestrator.file_orchestration_journal import _scalar_text
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_context_str(row: Mapping[str, Any], field: str) -> str:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError, _scalar_text
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_field")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    value = row.get(field)
    if value in (None, ""):
        raise FileOrchestrationJournalError("file_journal_missing_field", field=field)
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_int(value: Any, *, field: str) -> int | None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error


def _optional_datetime(value: Any, *, field: str) -> datetime | None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as error:
        raise FileOrchestrationJournalError("file_journal_invalid_field", field=field) from error
