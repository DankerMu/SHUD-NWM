"""Retention cluster of the file orchestration journal, as a repository mixin.

Hot-root retention discovery, the non-blocking retention cycle window, and the
member inventory, inspection and durable removal behind the journal retention
archive.  The retention result dataclasses, their discovery budget and the
journal's error type stay in the facade: the dataclasses are imported under
``TYPE_CHECKING`` for annotations and, with the others, inside the methods that
construct or raise them.  The facade-resident patched no-follow stat primitive
is looked up on the facade module at call time.
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from packages.common.contracts.time_normalization import format_cycle_time
from packages.common.safe_fs import (
    SafeFilesystemError,
    read_bytes_durable_no_follow,
    unlink_no_follow_durable,
)
from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_journal_candidates import _released_identity_blocked_row
from services.orchestrator.file_journal_paths import (
    _cycle_read_source_segments,
    _iter_retention_surface_files,
    _journal_segment_identity_from_path,
    _latest_identity_from_path,
    _relative_evidence,
)
from services.orchestrator.file_journal_primitives import _normalize_file_source_id
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_retry_helpers import _job_blocks_rollback_quiescence
from services.orchestrator.scheduler_state import _ensure_utc

if TYPE_CHECKING:
    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetentionCycle,
        FileJournalRetentionCycleInspection,
        FileJournalRetentionDiscovery,
        FileJournalRetentionMember,
        FileJournalRetentionRemoval,
    )


@_install_public_read_attribution
class _FileJournalRetentionMixin:
    def discover_retention_cycles(
        self,
        *,
        max_files: int | None = None,
        max_depth: int | None = None,
    ) -> FileJournalRetentionDiscovery:
        """Discover the complete union of the three retention-owned hot roots.

        The aggregate file budget is deliberately independent of the repository
        read budget: callers can tighten it for an operational invocation, but
        no root may consume a separate allowance and make a truncated union look
        complete.  Every path is shape-validated by the journal owner before its
        source/cycle identity is admitted.
        """

        from services.orchestrator.file_orchestration_journal import (
            FileJournalRetentionDiscovery,
            FileOrchestrationJournalError,
        )

        limit_files = self.max_files if max_files is None else int(max_files)
        limit_depth = self.max_depth if max_depth is None else int(max_depth)
        if limit_files < 1 or limit_depth < 0:
            return FileJournalRetentionDiscovery(status="blocked", reason="discovery_limit_invalid")
        try:
            return self._discover_retention_cycles_unlocked(
                max_files=limit_files,
                max_depth=limit_depth,
            )
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionDiscovery(status="blocked", reason=error.reason)

    @contextmanager
    def open_retention_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> Iterable[FileJournalRetentionCycle]:
        """Acquire the existing cycle flock without waiting, or return ``busy``.

        This owns the full retention transaction window.  It intentionally does
        not use the regular write context because retention must not wait behind
        a scheduler writer, while its lock order remains ``_write_lock`` then
        the existing cycle flock and never enters the inventory lock.
        """

        from services.orchestrator.file_orchestration_journal import FileJournalRetentionCycle

        canonical_source = _normalize_file_source_id(source_id, field="source_id")
        normalized_cycle = _ensure_utc(cycle_time)
        if not self._write_lock.acquire(blocking=False):
            yield FileJournalRetentionCycle(
                None,
                source_id=canonical_source,
                cycle_time=normalized_cycle,
                status="busy",
                reason="in_flight",
            )
            return
        try:
            self._ensure_root_unlocked()
            with self._cycle_file_lock_unlocked(
                source_id=canonical_source,
                cycle_time=normalized_cycle,
                non_blocking=True,
            ) as acquired:
                if not acquired:
                    yield FileJournalRetentionCycle(
                        None,
                        source_id=canonical_source,
                        cycle_time=normalized_cycle,
                        status="busy",
                        reason="in_flight",
                    )
                    return
                with self._cache_lock:
                    self._cycle_rows_cache.clear()
                    self._cycle_job_records_cache.clear()
                    self._direct_jobs_cycle_cache.clear()
                window = FileJournalRetentionCycle(
                    self,
                    source_id=canonical_source,
                    cycle_time=normalized_cycle,
                    status="locked",
                )
                try:
                    yield window
                finally:
                    window.close()
                    with self._cache_lock:
                        self._cycle_rows_cache.clear()
                        self._cycle_job_records_cache.clear()
                        self._direct_jobs_cycle_cache.clear()
        finally:
            self._write_lock.release()

    def _discover_retention_cycles_unlocked(
        self,
        *,
        max_files: int,
        max_depth: int,
    ) -> FileJournalRetentionDiscovery:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            FileJournalRetentionDiscovery,
            FileOrchestrationJournalError,
            _RetentionDiscoveryBudget,
        )

        cycles: set[tuple[str, datetime]] = set()
        budget = _RetentionDiscoveryBudget(limit=max_files)
        for surface in ("latest", "journal", "pipeline-events"):
            directory = self.root / surface
            try:
                directory_stat = file_orchestration_journal.stat_no_follow(directory, containment_root=self.root)
            except FileNotFoundError:
                continue
            except (OSError, SafeFilesystemError) as error:
                raise FileOrchestrationJournalError(
                    "file_journal_unreadable",
                    field=surface,
                    evidence={"error_type": type(error).__name__},
                ) from error
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise FileOrchestrationJournalError(
                    "file_journal_unsafe_scanned_entry",
                    field=surface,
                    evidence={"entry_type": "not_directory"},
                )
            for path in _iter_retention_surface_files(
                directory,
                root=self.root,
                surface=surface,
                max_files=max_files,
                max_depth=max_depth,
                budget=budget,
            ):
                if surface == "latest":
                    source_id, cycle_time, _model_id = _latest_identity_from_path(path, root=self.root)
                else:
                    source_id, cycle_time, _segment_index = _journal_segment_identity_from_path(
                        path,
                        root=self.root,
                        surface=surface,
                    )
                cycles.add((source_id, cycle_time))
        return FileJournalRetentionDiscovery(
            status="ok",
            cycles=tuple(sorted(cycles, key=lambda item: (item[0], format_cycle_time(item[1])))),
        )

    def _inspect_retention_cycle_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> FileJournalRetentionCycleInspection:
        """Classify one hot slice through the canonical cycle replay.

        Retention deliberately owns only the member inventory below.  Replay,
        direct-authority merge, source case handling, and live predicates stay
        in the normal cycle reader, including its filename-filtered flat direct
        leg and the by-cycle direct partition.  A flat direct can never become
        an archive member merely because it is canonical replay evidence.
        """

        from services.orchestrator.file_orchestration_journal import (
            FileJournalRetentionCycleInspection,
            FileOrchestrationJournalError,
        )

        members: tuple[FileJournalRetentionMember, ...] = ()
        try:
            source_id = _normalize_file_source_id(source_id, field="source_id")
            cycle_time = _ensure_utc(cycle_time)
            members = self._retention_cycle_members_unlocked(source_id=source_id, cycle_time=cycle_time)
            # The canonical reader includes both direct legs.  Flat direct
            # filenames intentionally fall open toward reading evidence, so an
            # unrelated malformed flat record is an integrity blocker for a
            # destructive retention pass rather than a reason to delete a
            # potentially recoverable hot slice.
            rows = self._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
            # Direct rows are projection evidence whose merge precedence is
            # intentionally lower than journal/latest state for normal query
            # semantics.  Retention liveness is stricter: any canonical direct
            # row for this slice remains recovery authority even when a stale
            # hot projection carries the same job id.  Do not alter the public
            # replay merge; only widen this destructive-operation predicate.
            direct_rows = self._direct_pipeline_job_records_for_cycle_cached(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            live_rows = [*rows.pipeline_jobs.values(), *direct_rows]
            try:
                blocking = any(_job_blocks_rollback_quiescence(job) for job in live_rows)
                released = any(_released_identity_blocked_row(job) for job in live_rows)
            except AcceptedSubmitEvidenceError:
                # A malformed current accepted master is recovery authority until
                # an owner transition proves otherwise; never archive it.
                blocking = True
                released = False
            return FileJournalRetentionCycleInspection(
                status="live" if blocking or released else "eligible",
                source_id=source_id,
                cycle_time=cycle_time,
                members=members,
                reason="live_row" if blocking or released else None,
            )
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionCycleInspection(
                status="blocked",
                source_id=source_id,
                cycle_time=cycle_time,
                reason=error.reason,
            )
        except AcceptedSubmitEvidenceError:
            return FileJournalRetentionCycleInspection(
                status="live",
                source_id=source_id,
                cycle_time=cycle_time,
                members=members,
                reason="live_row",
            )
        except (OSError, SafeFilesystemError, OrchestratorError):
            return FileJournalRetentionCycleInspection(
                status="blocked",
                source_id=source_id,
                cycle_time=cycle_time,
                reason="file_journal_unreadable",
            )

    def _retention_cycle_members_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
    ) -> tuple[FileJournalRetentionMember, ...]:
        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        cycle_segment = format_cycle_time(cycle_time)
        source_segments = _cycle_read_source_segments(
            source_id=source_id,
            source_segment_override=None,
            root=self.root,
        )
        members: list[FileJournalRetentionMember] = []
        for source_segment in source_segments:
            for path in self._latest_paths(source_segment, cycle_segment, model_id=None):
                path_source, path_cycle, _model_id = _latest_identity_from_path(path, root=self.root)
                if path_source != source_id or path_cycle != cycle_time:
                    raise FileOrchestrationJournalError(
                        "file_journal_path_identity_mismatch",
                        field=str(_relative_evidence(path, self.root)),
                    )
                members.append(self._retention_member_for_path(path))
            for surface in ("journal", "pipeline-events"):
                directory = self.root / surface / source_segment
                for path in self._cycle_segment_paths(directory, cycle_segment):
                    path_source, path_cycle, _segment_index = _journal_segment_identity_from_path(
                        path,
                        root=self.root,
                        surface=surface,
                    )
                    if path_source != source_id or path_cycle != cycle_time:
                        raise FileOrchestrationJournalError(
                            "file_journal_path_identity_mismatch",
                            field=str(_relative_evidence(path, self.root)),
                        )
                    # Read each selected event-log member now, under the same
                    # owner parser used by replay, so malformed or disappearing
                    # authority cannot be archived as a merely opaque byte blob.
                    self._read_jsonl(path, segment_index=_segment_index)
                    members.append(self._retention_member_for_path(path))
        return tuple(sorted(members, key=lambda item: item.relative_path))

    def _retention_member_for_path(self, path: Path) -> FileJournalRetentionMember:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            FileJournalRetentionMember,
            FileOrchestrationJournalError,
        )

        try:
            entry = file_orchestration_journal.stat_no_follow(path, containment_root=self.root)
        except (OSError, SafeFilesystemError) as error:
            raise FileOrchestrationJournalError(
                "file_journal_unreadable",
                field=str(_relative_evidence(path, self.root)),
                evidence={"error_type": type(error).__name__},
            ) from error
        if not stat.S_ISREG(entry.st_mode):
            raise FileOrchestrationJournalError(
                "file_journal_unsafe_scanned_entry",
                field=str(_relative_evidence(path, self.root)),
                evidence={"entry_type": "not_regular_file"},
            )
        return FileJournalRetentionMember(
            relative_path=_relative_evidence(path, self.root).as_posix(),
            size_bytes=int(entry.st_size),
        )

    def _remove_retention_members_unlocked(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        members: Sequence[FileJournalRetentionMember],
    ) -> FileJournalRetentionRemoval:
        """Durably unlink only the currently remaining manifest-bound members.

        A retry legitimately sees a strict subset after a prior interrupted
        cleanup.  It may never see a new member, and every remaining pathname
        must still carry the exact archive digest before the first unlink.  The
        preflight is deliberately complete before mutation so a same-size swap
        cannot turn a per-member failure into a partial cleanup.
        """

        from services.orchestrator.file_orchestration_journal import (
            FileJournalRetentionRemoval,
            FileOrchestrationJournalError,
        )

        try:
            expected_paths = {member.relative_path: member for member in members}
            if len(expected_paths) != len(members) or any(member.sha256 is None for member in members):
                return FileJournalRetentionRemoval(status="blocked", reason="manifest_member_invalid")
            if not expected_paths:
                return FileJournalRetentionRemoval(status="removed")
            current = self._retention_cycle_members_unlocked(source_id=source_id, cycle_time=cycle_time)
            current_paths = {member.relative_path: member for member in current}
            if any(path not in expected_paths for path in current_paths):
                return FileJournalRetentionRemoval(status="blocked", reason="member_set_changed")
            for relative_path, current_member in current_paths.items():
                expected = expected_paths[relative_path]
                if current_member.size_bytes != expected.size_bytes:
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
                try:
                    content = read_bytes_durable_no_follow(
                        self.root / relative_path,
                        max_bytes=max(expected.size_bytes, 1),
                        containment_root=self.root,
                    )
                except (OSError, SafeFilesystemError):
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
                if len(content) != expected.size_bytes or hashlib.sha256(content).hexdigest() != expected.sha256:
                    return FileJournalRetentionRemoval(status="blocked", reason="member_identity_changed")
            removed: list[str] = []
            for relative_path in sorted(current_paths):
                path = self.root / relative_path
                expected = expected_paths[relative_path]
                try:
                    # Repeat the durable byte proof immediately before every
                    # unlink.  The cycle flock excludes writers; this closes the
                    # remaining out-of-band same-size replacement window.
                    content = read_bytes_durable_no_follow(
                        path,
                        max_bytes=max(expected.size_bytes, 1),
                        containment_root=self.root,
                    )
                    if len(content) != expected.size_bytes or hashlib.sha256(content).hexdigest() != expected.sha256:
                        return FileJournalRetentionRemoval(
                            status="partial" if removed else "blocked",
                            removed_paths=tuple(removed),
                            reason="member_identity_changed",
                        )
                    unlink_no_follow_durable(path, containment_root=self.root, missing_ok=False)
                except (OSError, SafeFilesystemError):
                    return FileJournalRetentionRemoval(
                        status="partial" if removed else "blocked",
                        removed_paths=tuple(removed),
                        reason="member_unlink_failed",
                    )
                removed.append(relative_path)
            return FileJournalRetentionRemoval(status="removed", removed_paths=tuple(removed))
        except FileOrchestrationJournalError as error:
            return FileJournalRetentionRemoval(status="blocked", reason=error.reason)
