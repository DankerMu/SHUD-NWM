"""Pure file-journal identity primitives.

Source/cycle identity normalization and path-segment-safe text validators
shared by the file orchestration journal clusters. The journal's error type
stays in the facade; each function imports it right before the raise that
needs it so the normal path never executes the import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from packages.common.contracts.time_normalization import cycle_id_for, parse_cycle_time
from packages.common.source_identity import normalize_source_id

MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS = 255
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _normalize_file_source_id(value: Any, *, field: str) -> str:
    if value in (None, ""):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    text = _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")
    text = _safe_identity_text(text, field=field)
    try:
        return normalize_source_id(text)
    except ValueError as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_invalid_identity",
            field=field,
            evidence={"actual": text[:80]},
        ) from error


def _optional_source_id(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _normalize_file_source_id(value, field=field)


def _cycle_id_for_file_source(source_id: str, cycle_time: datetime) -> str:
    return cycle_id_for(_normalize_file_source_id(source_id, field="source_id"), cycle_time)


def _blocked_cycle_id(source_id: str, cycle_time: datetime) -> str:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        return _cycle_id_for_file_source(source_id, cycle_time)
    except FileOrchestrationJournalError:
        return "file_journal_read_blocked"


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    return _scalar_text(value, field=field, invalid_reason="file_journal_invalid_identity")


def _required_safe_identity(row: Mapping[str, Any], field: str) -> str:
    return _safe_identity_text(_required_text(row, field), field=field)


def _scalar_text(value: Any, *, field: str, invalid_reason: str) -> str:
    if isinstance(value, Mapping) or (isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    if isinstance(value, bytes | bytearray):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    return str(value)


def _safe_identity_text(value: str, *, field: str) -> str:
    if (
        not value
        or len(value) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or value in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(value) is None
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_unsafe_identity", field=field)
    return value


def _parse_cycle_time_field(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if value in (None, ""):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_missing_identity", field=field)
    try:
        return parse_cycle_time(str(value))
    except (TypeError, ValueError) as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error
