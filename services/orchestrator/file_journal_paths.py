"""File-journal path, segment and identity-validation helpers.

Source-segment discovery and case-alias dedup, journal segment naming and
lineage, path-derived identities, the bounded no-follow directory walkers, and
the record/payload identity validators behind the file orchestration journal.
The journal's error type and segment cap stay in the facade and are imported
inside the functions that use them; the three names tests patch on the facade
(the stat signature probe and the no-follow stat and listing primitives) are
looked up on the facade module at call time by the walkers and the segment
lineage check.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packages.common.contracts.time_normalization import format_cycle_time, parse_cycle_time
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator.file_journal_candidates import _pipeline_event_entity_type
from services.orchestrator.file_journal_primitives import (
    _SAFE_SEGMENT_RE,
    MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS,
    _cycle_id_for_file_source,
    _normalize_file_source_id,
    _optional_source_id,
    _parse_cycle_time_field,
    _required_safe_identity,
    _safe_identity_text,
    _scalar_text,
)
from services.orchestrator.run_identity import FORECAST_RUN_ID_RE as _FORECAST_RUN_ID_RE
from services.orchestrator.scheduler_state import _format_utc

if TYPE_CHECKING:
    from services.orchestrator.file_orchestration_journal import _RetentionDiscoveryBudget


_UNSET = object()
_JOURNAL_SEGMENT_INDEX_RE = re.compile(r"[1-9][0-9]*")
# The forecast and cohort shapes now live in run_identity (#1405) so retention
# adjudicates deletions against the same canonical identities. Only the
# forecast shape is imported above (under its historical private name); this
# module does not use the cohort shape, which stays imported in the facade
# `file_orchestration_journal`. This strict cohort variant stays here — its
# only consumer, the cohort branch of `_model_id_from_run_identity`, needs an
# exact cohort id.
_CYCLE_RUN_ID_RE = re.compile(r"^cycle_([^_]+)_(\d{10})$")


@dataclass(frozen=True)
class _CycleSourceDiscovery:
    source_id: str
    source_segments: tuple[str, ...]

    @property
    def source_segment(self) -> str:
        return self.source_segments[0]


def _cycle_source_discovery_from_segment(source_segment: str) -> _CycleSourceDiscovery:
    source_segment = _safe_segment(source_segment)
    return _CycleSourceDiscovery(
        source_id=_normalize_file_source_id(source_segment, field="source_id"),
        source_segments=(source_segment,),
    )


def _merge_cycle_source_discovery(
    sources: dict[str, _CycleSourceDiscovery],
    source: _CycleSourceDiscovery,
    *,
    root: Path | None = None,
) -> None:
    """Merge one surface's discovery into the per-source segment list.

    #1761 D3: when ``root`` is given, a candidate segment is dropped only when
    ``_names_same_directory`` proves by ``(st_dev, st_ino)`` that it names a
    directory already kept — the same identity discipline the primary alias
    branch of ``_cycle_read_source_segments`` uses.  On a case-insensitive
    volume the mixed pair this merge used to produce (``latest/IFS`` +
    ``journal/ifs``) collapses to one segment, so every record is read once; on
    a case-sensitive volume no surface can prove identity and both real
    directories stay.  A symlinked alias keeps its own inode under the no-follow
    stat and therefore stays too, where the read path fails it closed.  With
    ``root is None`` the historical string dedup is kept unchanged.
    """

    existing = sources.get(source.source_id)
    if existing is None:
        sources[source.source_id] = source
        return
    source_segments = list(existing.source_segments)
    # The identity probe costs one no-follow stat per surface per kept segment,
    # and this merge runs once per matching file under ``latest/`` and once per
    # matching segment under ``journal/``/``pipeline-events/``.  So it is
    # deferred until a candidate actually survives the cheap string check: the
    # common case, where every surface spells the source the same way, pays
    # nothing beyond the string compare it already paid.
    kept_identities: list[dict[str, tuple[int, int]]] | None = None
    for source_segment in source.source_segments:
        if source_segment in source_segments:
            continue
        if root is not None:
            if kept_identities is None:
                kept_identities = [
                    _source_segment_directory_identities(root, segment) for segment in source_segments
                ]
            if any(_names_same_directory(root, source_segment, identities) for identities in kept_identities):
                continue
            kept_identities.append(_source_segment_directory_identities(root, source_segment))
        source_segments.append(source_segment)
    sources[source.source_id] = _CycleSourceDiscovery(
        source_id=existing.source_id,
        source_segments=tuple(source_segments),
    )


def _cycle_read_source_segment(*, source_id: str, source_segment_override: str | None) -> str:
    if source_segment_override is None:
        return _safe_segment(source_id)
    source_segment = _safe_segment(source_segment_override)
    segment_source_id = _normalize_file_source_id(source_segment, field="source_id")
    if segment_source_id != source_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": source_id, "actual": segment_source_id[:80]},
        )
    return source_segment


_SOURCE_SEGMENT_SURFACES: tuple[str, ...] = (
    "latest",
    "journal",
    "pipeline-events",
    "pipeline-jobs/by-cycle",
)


def _source_segment_directory_identities(root: Path, segment: str) -> dict[str, tuple[int, int]]:
    """`(st_dev, st_ino)` of every per-source directory this segment names."""

    identities: dict[str, tuple[int, int]] = {}
    for surface in _SOURCE_SEGMENT_SURFACES:
        try:
            entry_stat = os.stat(root.joinpath(*surface.split("/"), segment), follow_symlinks=False)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            identities[surface] = (entry_stat.st_dev, entry_stat.st_ino)
    return identities


def _names_same_directory(
    root: Path,
    segment: str,
    other_identities: Mapping[str, tuple[int, int]],
) -> bool:
    """True only when a surface proves both segments name ONE directory.

    On a case-insensitive filesystem (macOS) ``gfs`` and ``GFS`` resolve to
    the same inode, so reading both would enumerate every record twice and
    silently inflate record/file budgets.  On a case-sensitive filesystem the
    two directories are genuinely distinct (or one is absent) and no surface
    can prove identity, so both segments are kept and both are read.
    Symlinked aliases keep a distinct inode under ``follow_symlinks=False``
    and therefore stay in the list, where the read path's containment
    discipline still fails them closed.
    """

    if not other_identities:
        return False
    identities = _source_segment_directory_identities(root, segment)
    return any(identities.get(surface) == identity for surface, identity in other_identities.items())


def _cycle_read_source_segments(
    *,
    source_id: str,
    source_segment_override: str | None,
    source_segment_overrides: tuple[str, ...] | None = None,
    root: Path | None = None,
) -> tuple[str, ...]:
    if source_segment_overrides is not None:
        segments: list[str] = []
        # #1761 D3: mirror the primary branch's identity dedup.  Every override
        # still goes through `_cycle_read_source_segment` first, so the
        # per-item source-mismatch validation (`file_journal_source_mismatch`)
        # is unchanged, and an override list that is empty after collapsing
        # still fails closed with `file_journal_missing_identity`.
        override_identities: list[dict[str, tuple[int, int]]] = []
        for source_segment_override_item in source_segment_overrides:
            segment = _cycle_read_source_segment(
                source_id=source_id,
                source_segment_override=source_segment_override_item,
            )
            if segment in segments:
                continue
            if root is not None:
                if any(_names_same_directory(root, segment, identities) for identities in override_identities):
                    continue
                override_identities.append(_source_segment_directory_identities(root, segment))
            segments.append(segment)
        if not segments:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="source_id")
        return tuple(segments)
    primary = _cycle_read_source_segment(
        source_id=source_id,
        source_segment_override=source_segment_override,
    )
    if source_segment_override is not None:
        return (primary,)
    segments = [primary]
    kept_identities: list[dict[str, tuple[int, int]]] = (
        [_source_segment_directory_identities(root, primary)] if root is not None else []
    )
    for alias in (source_id.lower(), source_id.upper()):
        segment = _safe_segment(alias)
        if segment in segments:
            continue
        if root is not None:
            if any(_names_same_directory(root, segment, identities) for identities in kept_identities):
                continue
            kept_identities.append(_source_segment_directory_identities(root, segment))
        segments.append(segment)
    return tuple(segments)


def _required_source_id(row: Mapping[str, Any], field: str) -> str:
    return _normalize_file_source_id(row.get(field), field=field)


def _canonical_candidate_run_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = _FORECAST_RUN_ID_RE.fullmatch(str(value))
    if match is None:
        return value
    run_source, run_cycle, run_model = match.groups()
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        matches_source = _normalize_file_source_id(run_source, field="run_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and run_cycle == cycle_stamp and run_model == model_id:
        return f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_forcing_version_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    cycle_stamp = format_cycle_time(cycle_time)
    match = re.fullmatch(r"forc_([^_]+)_(\d{10})_(.+)", str(value))
    if match is None:
        return value
    forcing_source, forcing_cycle, forcing_model = match.groups()
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        matches_source = _normalize_file_source_id(forcing_source, field="forcing_version_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if matches_source and forcing_cycle == cycle_stamp and forcing_model == model_id:
        return f"forc_{source_id.lower()}_{cycle_stamp}_{model_id}"
    return value


def _canonical_candidate_id(value: str, *, source_id: str, cycle_time: datetime, model_id: str) -> str:
    text = str(value)
    candidate_source, separator, remainder = text.partition(":")
    if not separator:
        return value
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        matches_source = _normalize_file_source_id(candidate_source, field="candidate_id") == source_id
    except FileOrchestrationJournalError:
        return value
    if not matches_source:
        return value
    remainder = remainder.replace(f"forecast_{candidate_source}_", f"forecast_{source_id}_", 1)
    return f"{source_id}:{remainder}"


def _optional_text(
    row: Mapping[str, Any],
    field: str,
    *,
    invalid_reason: str = "file_journal_invalid_field",
) -> str | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _scalar_text(value, field=field, invalid_reason=invalid_reason)


def _optional_safe_identity(row: Mapping[str, Any], field: str) -> str | None:
    value = _optional_text(row, field, invalid_reason="file_journal_invalid_identity")
    if value is None:
        return None
    return _safe_identity_text(value, field=field)


def _validated_actual_writer_generation(value: str) -> str:
    return _validated_git_writer_generation(
        value,
        field="actual_writer_generation",
        invalid_reason="file_journal_rollback_writer_generation_unresolvable",
    )


def _validated_git_writer_generation(
    value: str,
    *,
    field: str,
    invalid_reason: str,
) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) is None:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(invalid_reason, field=field)
    return value.lower()


def _validate_scheduler_visible_fields(row: Mapping[str, Any]) -> None:
    for visible_field in (
        "status",
        "stage",
        "slurm_job_id",
        "idempotency_key",
        "error_code",
        "event_type",
        "status_from",
        "status_to",
    ):
        _optional_text(row, visible_field)


def _require_source_cycle(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    expected_source = _normalize_file_source_id(source_id, field="source_id")
    actual_source = _required_source_id(row, "source_id")
    if actual_source != expected_source:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_source_mismatch",
            field="source_id",
            evidence={"expected": expected_source, "actual": actual_source[:80]},
        )
    parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
    if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_cycle_mismatch",
            field="cycle_time",
            evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
        )


def _require_cycle_id(row: Mapping[str, Any], expected_cycle_id: str) -> None:
    actual = _required_safe_identity(row, "cycle_id")
    if actual != expected_cycle_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_cycle_id_mismatch",
            field="cycle_id",
            evidence={"expected": expected_cycle_id, "actual": actual[:80]},
        )


def _require_model_id(row: Mapping[str, Any], expected_model_id: str, *, required: bool) -> None:
    actual = _optional_safe_identity(row, "model_id")
    if actual is None:
        if required:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        return
    if actual != expected_model_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": expected_model_id, "actual": actual[:80]},
        )


_RECORD_PAYLOAD_IDENTITY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "hydro_run": (
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "forecast_cycle": (("cycle_id", "file_journal_cycle_id_mismatch"),),
    "forcing_version": (
        ("forcing_version_id", "file_journal_forcing_version_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "model_context": (("model_id", "file_journal_model_mismatch"),),
    "pipeline_job": (
        ("job_id", "file_journal_job_mismatch"),
        ("run_id", "file_journal_run_mismatch"),
        ("model_id", "file_journal_model_mismatch"),
    ),
    "pipeline_event": (
        ("event_id", "file_journal_event_mismatch"),
        ("entity_id", "file_journal_job_mismatch"),
    ),
}


def _require_record_payload_identity_match(
    record_type: str,
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    for identity_field, reason in _RECORD_PAYLOAD_IDENTITY_FIELDS.get(record_type, ()):
        envelope_value = _optional_safe_identity(record, identity_field)
        payload_value = _optional_safe_identity(payload, identity_field)
        if envelope_value is not None and payload_value is not None and envelope_value != payload_value:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                reason,
                field=identity_field,
                evidence={"expected": envelope_value, "actual": payload_value[:80]},
            )


def _record_model_id(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    envelope_run_model_id = _model_id_from_run_identity(
        record.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    payload_run_model_id = _model_id_from_run_identity(
        payload.get("run_id"),
        source_id=source_id,
        cycle_time=cycle_time,
    )
    if (
        envelope_run_model_id is not None
        and payload_run_model_id is not None
        and envelope_run_model_id != payload_run_model_id
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": envelope_run_model_id, "actual": payload_run_model_id[:80]},
        )
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    explicit_model_id = envelope_model_id if envelope_model_id is not None else payload_model_id
    inferred_run_model_id = envelope_run_model_id if envelope_run_model_id is not None else payload_run_model_id
    if (
        explicit_model_id is not None
        and inferred_run_model_id is not None
        and explicit_model_id != inferred_run_model_id
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": explicit_model_id, "actual": inferred_run_model_id[:80]},
        )
    return explicit_model_id if explicit_model_id is not None else inferred_run_model_id


def _explicit_record_model_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    envelope_model_id = _optional_safe_identity(record, "model_id")
    payload_model_id = _optional_safe_identity(payload, "model_id")
    if envelope_model_id is not None and payload_model_id is not None and envelope_model_id != payload_model_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_model_mismatch",
            field="model_id",
            evidence={"expected": envelope_model_id, "actual": payload_model_id[:80]},
        )
    return envelope_model_id if envelope_model_id is not None else payload_model_id


def _model_id_from_run_identity(value: Any, *, source_id: str, cycle_time: datetime) -> str | None:
    if value in (None, ""):
        return None
    run_id = _safe_identity_text(
        _scalar_text(value, field="run_id", invalid_reason="file_journal_invalid_identity"),
        field="run_id",
    )
    cycle_stamp = format_cycle_time(cycle_time)
    forecast_match = _FORECAST_RUN_ID_RE.fullmatch(run_id)
    if forecast_match is not None:
        run_source, run_cycle, run_model = forecast_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return _safe_identity_text(run_model, field="run_id")
        return None
    cycle_match = _CYCLE_RUN_ID_RE.fullmatch(run_id)
    if cycle_match is not None:
        run_source, run_cycle = cycle_match.groups()
        if _normalize_file_source_id(run_source, field="run_id") == source_id and run_cycle == cycle_stamp:
            return None
    return None


def _validate_hydro_run_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_model_id(row, model_id, required=True)
    actual_run_id = _required_safe_identity(row, "run_id")
    cycle_stamp = format_cycle_time(cycle_time)
    expected_forecast_run_id = f"fcst_{source_id.lower()}_{cycle_stamp}_{model_id}"
    expected_cycle_run_prefix = f"cycle_{source_id.lower()}_{cycle_stamp}"
    if actual_run_id != expected_forecast_run_id and (
        actual_run_id != expected_cycle_run_prefix and not actual_run_id.startswith(f"{expected_cycle_run_prefix}_")
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={
                "expected": f"{expected_forecast_run_id}|{expected_cycle_run_prefix}",
                "actual": actual_run_id[:80],
            },
        )
    _validate_scheduler_visible_fields(row)


def _validate_forecast_cycle_identity(row: Mapping[str, Any], *, source_id: str, cycle_time: datetime) -> None:
    _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)


def _validate_forcing_version_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    require_forcing_version_id: bool = True,
    require_source_cycle: bool = False,
    require_model_id: bool = False,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    if require_source_cycle:
        _require_source_cycle(row, source_id=source_id, cycle_time=cycle_time)
    else:
        actual_source = _optional_source_id(row, "source_id")
        if actual_source is not None and actual_source != source_id:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
        if row.get("cycle_time") not in (None, ""):
            parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
            if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
                from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
                raise FileOrchestrationJournalError(
                    "file_journal_cycle_mismatch",
                    field="cycle_time",
                    evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
                )
    _require_model_id(row, model_id, required=require_model_id)
    forcing_version_id = row.get("forcing_version_id")
    if forcing_version_id in (None, ""):
        if require_forcing_version_id:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="forcing_version_id")
        return
    expected_prefix = f"forc_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    actual_forcing_version_id = _required_safe_identity(row, "forcing_version_id")
    if actual_forcing_version_id != expected_prefix:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_forcing_version_mismatch",
            field="forcing_version_id",
            evidence={"expected": expected_prefix, "actual": actual_forcing_version_id[:80]},
        )


def _validate_model_context_identity(row: Mapping[str, Any], *, model_id: str) -> None:
    _require_model_id(row, model_id, required=True)


def _validate_pipeline_job_identity(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
    expected_job_id: str | None = None,
) -> None:
    source_id = _normalize_file_source_id(source_id, field="source_id")
    job_id = _required_safe_identity(row, "job_id")
    if expected_job_id is not None and job_id != expected_job_id:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_job_mismatch",
            field="job_id",
            evidence={"expected": expected_job_id, "actual": job_id[:80]},
        )
    actual_source = _optional_source_id(row, "source_id")
    if actual_source is not None:
        if actual_source != source_id:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_source_mismatch",
                field="source_id",
                evidence={"expected": source_id, "actual": actual_source[:80]},
            )
    if row.get("cycle_time") not in (None, ""):
        parsed_cycle_time = _parse_cycle_time_field(row, "cycle_time")
        if _format_utc(parsed_cycle_time) != _format_utc(cycle_time):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_cycle_mismatch",
                field="cycle_time",
                evidence={"expected": _format_utc(cycle_time), "actual": _format_utc(parsed_cycle_time)},
            )
    _require_cycle_id(row, _cycle_id_for_file_source(source_id, cycle_time))
    _validate_scheduler_visible_fields(row)
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if model_id not in (None, ""):
        _require_model_id(row, str(model_id), required=False)
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        cycle_run_prefix = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        run_id = _required_safe_identity(row, "run_id")
        if run_id != candidate_run_id and run_id != cycle_run_id and not run_id.startswith(f"{cycle_run_prefix}_"):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_run_mismatch",
                field="run_id",
                evidence={"expected": f"{candidate_run_id}|{cycle_run_prefix}", "actual": run_id[:80]},
            )
        return
    run_id = _required_safe_identity(row, "run_id")
    if (
        run_id != cycle_run_id
        and not run_id.startswith(f"{cycle_run_id}_")
        and not run_id.startswith(f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_")
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_run_mismatch",
            field="run_id",
            evidence={"expected": f"{cycle_run_id}|{cycle_run_id}_<cohort>", "actual": run_id[:80]},
        )


def _validate_event_identity(
    row: Mapping[str, Any],
    *,
    source_id: str | None = None,
    cycle_time: datetime | None = None,
) -> None:
    _optional_text(row, "event_id")
    entity_id = _required_safe_identity(row, "entity_id")
    entity_type = _pipeline_event_entity_type(row.get("entity_type") or "pipeline_job")
    if entity_type == "forecast_cycle" and source_id is not None and cycle_time is not None:
        expected_cycle_id = _cycle_id_for_file_source(source_id, cycle_time)
        if entity_id != expected_cycle_id:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_event_entity_mismatch",
                field="entity_id",
                evidence={"expected": expected_cycle_id, "actual": entity_id[:80]},
            )
    _validate_scheduler_visible_fields(row)


def _validate_payload_identity(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str | None,
) -> None:
    if record_type == "hydro_run":
        if model_id in (None, ""):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_hydro_run_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "forecast_cycle":
        _validate_forecast_cycle_identity(payload, source_id=source_id, cycle_time=cycle_time)
    elif record_type == "forcing_version":
        if model_id in (None, ""):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_forcing_version_identity(payload, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    elif record_type == "model_context":
        if model_id in (None, ""):
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError("file_journal_missing_identity", field="model_id")
        _validate_model_context_identity(payload, model_id=model_id)


def _latest_identity_from_path(path: Path, *, root: Path) -> tuple[str, datetime, str]:
    parts = path.relative_to(root).parts
    if len(parts) != 4 or parts[0] != "latest":
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_segment = _safe_segment(parts[2])
    model_id = _safe_segment(Path(parts[3]).stem)
    return source_id, _parse_cycle_segment(cycle_segment, field=str(_relative_evidence(path, root))), model_id


def _journal_identity_from_path(path: Path, *, root: Path, surface: str) -> tuple[str, datetime]:
    source_id, cycle_time, _segment_index = _journal_segment_identity_from_path(
        path,
        root=root,
        surface=surface,
    )
    return source_id, cycle_time


def _journal_segment_identity_from_path(
    path: Path,
    *,
    root: Path,
    surface: str,
) -> tuple[str, datetime, int]:
    """Resolve (source, cycle, segment index) for one cycle event-log file.

    Continuation segments belong to their base cycle; genuinely unparseable
    names keep raising ``file_journal_invalid_cycle_time`` exactly as before.
    """
    parts = path.relative_to(root).parts
    if len(parts) != 3 or parts[0] != surface:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_path_identity_mismatch",
            field=str(_relative_evidence(path, root)),
        )
    source_id = _normalize_file_source_id(parts[1], field="source_id")
    cycle_stem, segment_index = _split_journal_segment_stem(Path(parts[2]).stem)
    cycle_time = _parse_cycle_segment(
        _safe_segment(cycle_stem),
        field=str(_relative_evidence(path, root)),
    )
    _require_journal_segment_lineage(
        path,
        root=root,
        cycle_segment=cycle_stem,
        segment_index=segment_index,
    )
    return source_id, cycle_time, segment_index


def _journal_segment_name(cycle_segment: str, segment_index: int) -> str:
    """Canonical file name of one per-cycle event-log segment."""

    if segment_index <= 0:
        return f"{cycle_segment}.jsonl"
    return f"{cycle_segment}.{segment_index}.jsonl"


def _journal_segment_names(cycle_segment: str) -> tuple[str, ...]:
    """Every segment slot a cycle may own, plus the first illegal one.

    The extra probe narrows — it does not eliminate — the asymmetry between
    the recursive walkers and the cycle-level readers: it pulls the first
    over-cap segment into enumeration and the cache fingerprint, so the
    asymmetry is pushed out to the window boundary.  An index beyond
    ``MAX_FILE_JOURNAL_CYCLE_SEGMENTS`` is still walker-detected only,
    because widening the window further would mean globbing the directory.
    """
    from services.orchestrator.file_orchestration_journal import MAX_FILE_JOURNAL_CYCLE_SEGMENTS
    return tuple(
        _journal_segment_name(cycle_segment, index)
        for index in range(MAX_FILE_JOURNAL_CYCLE_SEGMENTS + 1)
    )


def _journal_segment_stem_in_cycle_scope(stem: str, cycle_segment: str) -> bool:
    """Does this segment file name belong to (or fail to disclaim) one cycle?

    Same fall-open shape D2a fixes on the flat direct surface: skip a name ONLY
    when it resolves to a *different* cycle, exactly as the whole-tree scan
    would attribute it. A name that resolves to nothing — ``<cycle>.x.jsonl``,
    ``not-a-cycle.jsonl`` — is handed to ``_journal_identity_from_path`` so it
    fails closed with the same reason the whole-tree scan raises, instead of
    being quietly skipped.
    """

    base, _segment_index = _split_journal_segment_stem(stem)
    if base == cycle_segment:
        return True
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    try:
        parse_cycle_time(_safe_segment(base))
    except (TypeError, ValueError, FileOrchestrationJournalError):
        return True
    return False


def _split_journal_segment_stem(stem: str) -> tuple[str, int]:
    """Split a journal file stem into (cycle segment, segment index).

    ``<cycle>`` is segment 0 and ``<cycle>.<n>`` (n >= 1) is segment n.
    Anything else — a non-numeric suffix, or an explicit ``.0`` — is returned
    unchanged as segment 0, so foreign names keep today's parsing behaviour
    byte-identically.
    """
    base, dot, suffix = stem.rpartition(".")
    if dot and base and _JOURNAL_SEGMENT_INDEX_RE.fullmatch(suffix):
        return base, int(suffix)
    return stem, 0


def _require_journal_segment_lineage(
    path: Path,
    *,
    root: Path,
    cycle_segment: str,
    segment_index: int,
) -> None:
    """Fail closed on orphan, gapped or over-cap continuation segments.

    One rule for the cycle-level enumeration and every recursive walker, so a
    corrupt segment gets the same answer from every reader rather than being
    ignored by one and read by another.
    """
    if segment_index <= 0:
        return
    directory = path.parent
    from services.orchestrator import file_orchestration_journal
    from services.orchestrator.file_orchestration_journal import MAX_FILE_JOURNAL_CYCLE_SEGMENTS
    for index in range(min(segment_index, MAX_FILE_JOURNAL_CYCLE_SEGMENTS)):
        if file_orchestration_journal._stat_signature(
            directory / _journal_segment_name(cycle_segment, index)
        ) is None:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
            raise FileOrchestrationJournalError(
                "file_journal_segment_gap",
                field=str(_relative_evidence(path, root)),
            )
    if segment_index >= MAX_FILE_JOURNAL_CYCLE_SEGMENTS:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError(
            "file_journal_segment_limit_exceeded",
            field=str(_relative_evidence(path, root)),
        )


def _journal_segment_sort_key(path: Path, *, root: Path, surface: str) -> tuple[str, str, int]:
    """Order journal paths by parsed identity, never by raw path name.

    Lexicographic order puts ``<cycle>.1.jsonl`` before ``<cycle>.jsonl``
    (``'1' < 'j'``), which would replay a continuation segment ahead of the
    base it continues.
    """
    parts = path.relative_to(root).parts if path.is_relative_to(root) else ()
    if len(parts) != 3 or parts[0] != surface:
        return (str(path), "", 0)
    cycle_segment, segment_index = _split_journal_segment_stem(Path(parts[2]).stem)
    return (parts[1], cycle_segment, segment_index)


def _parse_cycle_segment(value: str, *, field: str) -> datetime:
    try:
        return parse_cycle_time(value)
    except (TypeError, ValueError) as error:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_invalid_cycle_time", field=field) from error


def _iter_retention_surface_files(
    directory: Path,
    *,
    root: Path,
    surface: str,
    max_files: int,
    max_depth: int,
    budget: _RetentionDiscoveryBudget,
) -> Iterable[Path]:
    """Walk one exact retention root and reject every unrecognized occupant.

    ``latest`` admits only ``source/cycle/model.json``.  The two event surfaces
    admit only one source directory and canonical bounded cycle segments.  The
    walk counts every directory entry, not just accepted files, because a large
    foreign subtree must not hide beyond a convenient suffix filter.
    """

    from services.orchestrator import file_orchestration_journal
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    def walk(current: Path, depth: int) -> Iterable[Path]:
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_depth_limit_exceeded",
                field=str(_relative_evidence(current, root)),
                evidence={"max_depth": max_depth},
            )
        try:
            current_stat = file_orchestration_journal.stat_no_follow(current, containment_root=root)
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISDIR(current_stat.st_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"entry_type": "not_directory"},
            )
        try:
            names = file_orchestration_journal.list_directory_no_follow_limited(
                current,
                containment_root=root,
                max_entries=max_files - budget.count,
            )
        except FileNotFoundError:
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        for name in sorted(names):
            budget.consume()
            if _SAFE_SEGMENT_RE.fullmatch(name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(current / name, root)),
                )
            path = current / name
            try:
                entry_stat = file_orchestration_journal.stat_no_follow(path, containment_root=root)
            except FileNotFoundError:
                raise FileOrchestrationJournalError(
                    "file_journal_unreadable",
                    field=str(_relative_evidence(path, root)),
                ) from None
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(path, root)),
                    evidence={"error_type": type(error).__name__},
                ) from error
            relative = path.relative_to(directory).parts
            if stat.S_ISDIR(entry_stat.st_mode):
                if (surface == "latest" and len(relative) < 3) or (surface != "latest" and len(relative) <= 1):
                    yield from walk(path, depth + 1)
                    continue
                raise FileOrchestrationJournalError(
                    "file_journal_unrecognised_retention_member",
                    field=str(_relative_evidence(path, root)),
                )
            if not stat.S_ISREG(entry_stat.st_mode):
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(path, root)),
                    evidence={"entry_type": "not_regular_file"},
                )
            if surface == "latest":
                if len(relative) != 3 or not name.endswith(".json"):
                    raise FileOrchestrationJournalError(
                        "file_journal_unrecognised_retention_member",
                        field=str(_relative_evidence(path, root)),
                    )
                _latest_identity_from_path(path, root=root)
            else:
                if len(relative) != 2 or not name.endswith(".jsonl"):
                    raise FileOrchestrationJournalError(
                        "file_journal_unrecognised_retention_member",
                        field=str(_relative_evidence(path, root)),
                    )
                _journal_segment_identity_from_path(path, root=root, surface=surface)
            yield path

    yield from walk(directory, 0)


def _iter_discovered_files(
    directory: Path,
    *,
    root: Path,
    suffix: str,
    recursive: bool,
    max_files: int,
    max_depth: int,
    strict_disappearance: bool = False,
    expected_root_signature: Any = _UNSET,
) -> Iterable[Path]:
    scanned_entries = 0

    from services.orchestrator import file_orchestration_journal
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    def walk(current: Path, depth: int) -> Iterable[Path]:
        nonlocal scanned_entries
        if depth > max_depth:
            raise FileOrchestrationJournalError(
                "file_journal_depth_limit_exceeded",
                field=str(_relative_evidence(current, root)),
                evidence={"max_depth": max_depth},
            )
        strict_authority_walk = strict_disappearance and expected_root_signature is not _UNSET
        directory_signature = (
            file_orchestration_journal._stat_signature(current) if strict_authority_walk else _UNSET
        )
        if (
            current == directory
            and expected_root_signature is not _UNSET
            and directory_signature != expected_root_signature
        ):
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field=str(_relative_evidence(current, root)),
            )
        if current != directory and strict_authority_walk and directory_signature is None:
            raise FileOrchestrationJournalError(
                "file_journal_quiescence_authority_changed",
                field=str(_relative_evidence(current, root)),
            )

        def require_directory_unchanged() -> None:
            if strict_authority_walk and file_orchestration_journal._stat_signature(current) != directory_signature:
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(current, root)),
                )

        try:
            current_mode = file_orchestration_journal.stat_no_follow(current, containment_root=root).st_mode
        except FileNotFoundError:
            if strict_disappearance and expected_root_signature is not _UNSET and (
                current != directory or expected_root_signature is not None
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_quiescence_authority_changed",
                    field=str(_relative_evidence(current, root)),
                )
            if strict_disappearance and current != directory:
                raise FileOrchestrationJournalError(
                    "file_journal_reconcile_inventory_migration_invalid",
                    field=str(_relative_evidence(current, root)),
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        require_directory_unchanged()
        if not stat.S_ISDIR(current_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(current, root)),
                evidence={"entry_type": "not_directory"},
            )
        remaining_entries = max_files - scanned_entries
        if remaining_entries < 0:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        try:
            entry_names = file_orchestration_journal.list_directory_no_follow_limited(
                current,
                containment_root=root,
                max_entries=remaining_entries,
            )
        except FileNotFoundError:
            if strict_disappearance:
                raise FileOrchestrationJournalError(
                    (
                        "file_journal_quiescence_authority_changed"
                        if expected_root_signature is not _UNSET
                        else "file_journal_reconcile_inventory_migration_invalid"
                    ),
                    field=str(_relative_evidence(current, root)),
                )
            return
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(current, root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if len(entry_names) > remaining_entries:
            raise FileOrchestrationJournalError(
                "file_journal_file_limit_exceeded",
                field=str(_relative_evidence(directory, root)),
                evidence={"max_files": max_files},
            )
        require_directory_unchanged()
        scanned_entries += len(entry_names)
        for entry_name in sorted(entry_names):
            if _SAFE_SEGMENT_RE.fullmatch(entry_name) is None:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_path_segment",
                    field=str(_relative_evidence(current / entry_name, root)),
                )
            entry = current / entry_name
            try:
                mode = file_orchestration_journal.stat_no_follow(entry, containment_root=root).st_mode
            except FileNotFoundError:
                if strict_disappearance:
                    raise FileOrchestrationJournalError(
                        (
                            "file_journal_quiescence_authority_changed"
                            if expected_root_signature is not _UNSET
                            else "file_journal_reconcile_inventory_migration_invalid"
                        ),
                        field=str(_relative_evidence(entry, root)),
                    )
                continue
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=str(_relative_evidence(entry, root)),
                    evidence={"error_type": type(error).__name__},
                ) from error
            if stat.S_ISDIR(mode):
                if recursive:
                    yield from walk(entry, depth + 1)
                continue
            if entry_name.endswith(suffix):
                if not stat.S_ISREG(mode):
                    raise FileOrchestrationJournalError(
                        "file_journal_unsafe_scanned_entry",
                        field=str(_relative_evidence(entry, root)),
                        evidence={"entry_type": "not_regular_file"},
                    )
                yield entry
        require_directory_unchanged()

    yield from walk(directory, 0)


def _safe_segment(value: str) -> str:
    text = str(value)
    if (
        not text
        or len(text) > MAX_FILE_JOURNAL_PATH_SEGMENT_CHARS
        or text in {".", ".."}
        or _SAFE_SEGMENT_RE.fullmatch(text) is None
    ):
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
        raise FileOrchestrationJournalError("file_journal_unsafe_path_segment", field="path")
    return text


def _relative_evidence(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path("[local-path]")
