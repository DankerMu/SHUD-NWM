"""#1944: a read-only census of job-id scope divergence in the file journal.

The #1760 gate ``_require_job_id_cycle_scope`` rejects, on every write lane, a
pipeline-job row whose ``job_id`` encodes a ``(source, cycle)`` that
contradicts the row's own pair.  The gate is correct and stays.  What was
missing is an observation surface: a legacy row already on disk in that shape
is invisible until it stops a reconcile scan or refuses a transition, and the
only evidence that none exists was a migration-input measurement.  This module
is that observation surface.

Design commitments this module keeps, all of them load-bearing:

* **The gate's own predicate is the only classifier.**  Every candidate row --
  and every reconcile-inventory anchor, which carries the row's own pair -- is
  handed to ``repository._require_job_id_cycle_scope({"payload": row},
  record_type="pipeline_job")`` and classified on whether it raises
  ``file_journal_job_id_scope_mismatch``.  There is no second comparison of
  ``_cycle_scope_from_job_id`` output anywhere here: a second implementation is
  exactly how "census reports zero while the gate fires" happens.
* **Zero bytes are written under the journal root.**  Anchors are enumerated
  with ``list_directory_no_follow_limited`` and validated with the pure
  ``_validated_reconcile_inventory_anchor``.  The census never calls
  ``_iter_reconcile_inventory_records`` (which takes the write lock and a cycle
  flock per anchor, restores derived directs and prunes anchors),
  ``_iter_reconcile_pipeline_job_records`` (which runs the inventory
  migration), ``_ensure_reconcile_inventory_migrated`` or
  ``_reconcile_inventory_entry_names_unlocked`` (which DELETES ``.tmp``
  residue).  Residue is counted and left in place.  ``--output`` refuses any
  path under the verified root, so the receipt cannot become the first byte the
  census writes into the tree.
* **Every surface the reconcile scan and the canonical lookup read is
  covered**, because the shape that aborts the reconcile scan -- a row that
  lives only in journal segments, with an inventory anchor and no flat direct
  file -- is invisible to a flat-only scan.  An absent directory is reported as
  absent, never omitted.
* **Every reader fault other than a scope mismatch fails the census loud.**
  An inventory entry that is neither a well-formed anchor name nor residue
  raises ``file_journal_reconcile_inventory_invalid``, mirroring the journal's
  own listing rules; a torn read raises ``file_journal_unreadable``.  Skipping
  is never added.

Exit codes and stderr formats deliberately depart from this CLI's existing
journal-error convention (``services/orchestrator/cli.py``'s
``print(str(error))`` + exit 2 on ``FileOrchestrationJournalError``): for this
command exit **2 means "divergent rows found"**, so a typed failure must be
distinguishable and uses exit **1**.  On stderr an ``OrchestratorError`` prints
``error_code: message`` (the ``plan-production`` convention) and a
``FileOrchestrationJournalError``, which carries ``reason``/``field`` rather
than a code, prints ``reason: field``.  Neither prints a traceback.

The receipt is echoed to stdout BEFORE ``--output`` is written, so an
unwritable receipt path (``CENSUS_OUTPUT_UNWRITABLE``, exit 1) reports the
failure without discarding the census that was already paid for; the in-root
refusal still happens before the census runs, so nothing is echoed for it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from packages.common.safe_fs import (
    SafeFilesystemError,
    list_directory_no_follow_limited,
    stat_no_follow,
)

from .chain_types import OrchestratorError
from .file_orchestration_journal import (
    _LEGACY_ACTIVE_RECONCILE_DIRECTORY,
    _RECONCILE_INVENTORY_DIRECTORY,
    _RECONCILE_INVENTORY_TEMP_RE,
    _SAFE_SEGMENT_RE,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _iter_jsonl_files,
    _iter_regular_json_files,
    _relative_evidence,
    _safe_segment,
)
from .journal_root_authority import verify_journal_root_authority

CENSUS_JOB_ID_SCOPE_COMMAND = "census-job-id-scope"
CENSUS_SCHEMA_VERSION = "nhms.scheduler.job_id_scope_census.v1"
CENSUS_JOB_ID_SCOPE_HELP = (
    "Census the file journal for pipeline-job rows whose job_id contradicts "
    "their own (source, cycle) -- the #1760 scope gate's own predicate, applied "
    "read-only to every surface the reconcile scan and the canonical lookup "
    "read (flat directs, the by-cycle partition, the journal replay, the "
    "reconcile-inventory anchors and the legacy active-reconcile directory). "
    "Writes nothing under the journal root: --output refuses any path inside "
    "it, and inventory .tmp residue is reported, never removed. Exit 0 when no "
    "divergent row exists, 2 when one or more do, 1 on a typed failure. A "
    "production-sized tree can legitimately exceed the default replay record "
    "budget (file_journal_record_limit_exceeded: pipeline_job_records, exit 1); "
    "--max-records raises it -- node-22 needed 5000000 on 2026-09-02."
)

#: The row-bearing surfaces a divergent id's ``surfaces`` list may name.
#: Anchors are NOT rows: an anchor is reported on the ``reconcile_inventory``
#: surface and through the per-id ``anchor_present`` axis, never here.
ROW_SURFACES = ("flat_direct", "by_cycle_direct", "journal_replay", "active_reconcile")

_OUTPUT_INSIDE_ROOT_MESSAGE = (
    "census receipt --output must not be inside the journal root: the census writes nothing under the root it reads"
)
#: Constant, path-free and traceback-free like every other typed line this
#: command emits.  It names stdout deliberately: the census already ran and its
#: receipt is complete on stdout, so the operator loses the file, not the run.
OUTPUT_UNWRITABLE_MESSAGE = "census receipt could not be written; the receipt above on stdout is complete"

MAX_FILES_HELP = (
    "Override the per-walk discovered-file budget (default 100000). A trip is "
    "file_journal_file_limit_exceeded on the offending directory, or "
    "'file_journal_record_limit_exceeded: reconcile_inventory' for the anchor "
    "listing. It does NOT widen the replay record budget: for "
    "'file_journal_record_limit_exceeded: pipeline_job_records', raise "
    "--max-records instead."
)

MAX_RECORDS_HELP = (
    "Override the journal replay record budget (default MAX_FILE_JOURNAL_RECORDS "
    "= 100000, charged across latest views, segment records and direct records "
    "together). A trip fails loud with 'file_journal_record_limit_exceeded: "
    "pipeline_job_records' and exit 1; node-22's live tree needed 5000000 on "
    "2026-09-02. Raising the budget is the documented remedy, never a skip."
)

_OUTPUT_HELP = "Write the receipt to this path (must be outside the root); stdout carries it either way."


class _Divergence:
    """One divergent ``job_id``, accumulated across surfaces."""

    def __init__(self, *, own_scope: str, job_id_scope: str) -> None:
        self.own_scope = own_scope
        self.job_id_scope = job_id_scope
        self.surfaces: set[str] = set()


def _scope_mismatch(
    repository: FileOrchestrationJournalRepository,
    row: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Ask the WRITE GATE whether ``row`` is divergent; ``None`` when it is not.

    Returns ``(own_scope, job_id_scope)`` straight out of the gate's own
    evidence.  Any other journal error propagates: fail loud is the contract,
    and a non-scope rejection of a legitimately old row is a finding to route,
    never a skip to add.
    """

    try:
        repository._require_job_id_cycle_scope({"payload": row}, record_type="pipeline_job")
    except FileOrchestrationJournalError as error:
        if error.reason != "file_journal_job_id_scope_mismatch":
            raise
        return str(error.evidence.get("expected") or ""), str(error.evidence.get("actual") or "")
    return None


def _directory_present(repository: FileOrchestrationJournalRepository, directory: Path) -> bool:
    """Absent vs present, judged the way the journal's own walk judges it.

    ``_iter_regular_json_files`` returns silently on an absent directory, so it
    cannot tell "absent" from "empty" -- and the receipt must report absence
    rather than omit the surface.  A symlinked or non-directory slot is not
    absence: it fails loud with the journal's own scanned-entry token.
    """

    try:
        mode = stat_no_follow(directory, containment_root=repository.root).st_mode
    except FileNotFoundError:
        return False
    except (OSError, SafeFilesystemError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_unsafe_scanned_entry",
            field=str(_relative_evidence(directory, repository.root)),
            evidence={"error_type": type(error).__name__},
        ) from error
    if not stat.S_ISDIR(mode):
        raise FileOrchestrationJournalError(
            "file_journal_unsafe_scanned_entry",
            field=str(_relative_evidence(directory, repository.root)),
            evidence={"entry_type": "not_directory"},
        )
    return True


def _record_divergence(
    divergences: dict[str, _Divergence],
    job_id: str,
    scopes: tuple[str, str],
    *,
    surface: str | None,
) -> None:
    entry = divergences.get(job_id)
    if entry is None:
        entry = _Divergence(own_scope=scopes[0], job_id_scope=scopes[1])
        divergences[job_id] = entry
    if surface is not None:
        if surface not in ROW_SURFACES:
            raise ValueError(f"not a row-bearing census surface: {surface}")
        entry.surfaces.add(surface)


def _census_flat_direct(
    repository: FileOrchestrationJournalRepository,
    divergences: dict[str, _Divergence],
) -> tuple[dict[str, Any], set[str]]:
    directory = repository.root / "pipeline-jobs"
    present = _directory_present(repository, directory)
    if not present:
        return {"present": False, "files": 0, "rows": 0, "divergent": 0}, set()
    # Non-recursive: ``pipeline-jobs/by-cycle`` is its own surface below.
    paths = sorted(
        _iter_regular_json_files(
            directory,
            root=repository.root,
            max_files=repository.max_files,
            max_depth=repository.max_depth,
        )
    )
    identifiers = {path.stem for path in paths}
    rows = 0
    divergent = 0
    for row in repository._iter_direct_pipeline_job_records():
        rows += 1
        scopes = _scope_mismatch(repository, row)
        if scopes is not None:
            divergent += 1
            _record_divergence(divergences, str(row.get("job_id") or ""), scopes, surface="flat_direct")
    summary = {"present": True, "files": len(paths), "rows": rows, "divergent": divergent}
    return summary, identifiers


def _census_direct_files(
    repository: FileOrchestrationJournalRepository,
    divergences: dict[str, _Divergence],
    *,
    directory: Path,
    surface: str,
    recursive: bool,
) -> tuple[dict[str, Any], set[str]]:
    """The by-cycle partition and the legacy active-reconcile directory.

    Both are read exactly the way their own consumer reads them: one
    ``_read_optional_json`` per file through ``_validated_direct_pipeline_job_record``
    with the filename as the expected identity -- the by-cycle partition as
    ``_iter_direct_pipeline_job_records`` reads the flat one, the legacy
    directory as ``_canonical_reconcile_job_unlocked`` reads it.
    """

    present = _directory_present(repository, directory)
    if not present:
        return {"present": False, "files": 0, "rows": 0, "divergent": 0}, set()
    paths = sorted(
        _iter_regular_json_files(
            directory,
            root=repository.root,
            recursive=recursive,
            max_files=repository.max_files,
            max_depth=repository.max_depth,
        )
    )
    identifiers: set[str] = set()
    rows = 0
    divergent = 0
    for path in paths:
        payload = repository._read_optional_json(path)
        if payload is None:
            continue
        row = repository._validated_direct_pipeline_job_record(payload, expected_job_id=_safe_segment(path.stem))
        rows += 1
        job_id = str(row.get("job_id") or "")
        identifiers.add(job_id)
        scopes = _scope_mismatch(repository, row)
        if scopes is not None:
            divergent += 1
            _record_divergence(divergences, job_id, scopes, surface=surface)
    summary = {"present": True, "files": len(paths), "rows": rows, "divergent": divergent}
    return summary, identifiers


def _census_journal_replay(
    repository: FileOrchestrationJournalRepository,
    divergences: dict[str, _Divergence],
) -> tuple[dict[str, Any], set[str]]:
    """Latest views plus journal segments -- where a segment-only row lives.

    ``_validate_pipeline_job_identity`` checks the payload against its path, not
    the id against the payload, so a divergent row replays cleanly and is seen
    here even when no direct file for it exists.
    """

    latest_directory = repository.root / "latest"
    journal_directory = repository.root / "journal"
    latest_present = _directory_present(repository, latest_directory)
    journal_present = _directory_present(repository, journal_directory)
    latest_files = (
        len(
            list(
                _iter_regular_json_files(
                    latest_directory,
                    root=repository.root,
                    recursive=True,
                    max_files=repository.max_files,
                    max_depth=repository.max_depth,
                )
            )
        )
        if latest_present
        else 0
    )
    segment_files = (
        len(
            list(
                _iter_jsonl_files(
                    journal_directory,
                    root=repository.root,
                    max_files=repository.max_files,
                    max_depth=repository.max_depth,
                )
            )
        )
        if journal_present
        else 0
    )
    summary: dict[str, Any] = {
        "present": latest_present or journal_present,
        "files": latest_files + segment_files,
        "latest_files": latest_files,
        "latest_present": latest_present,
        "segment_files": segment_files,
        "segment_present": journal_present,
        "rows": 0,
        "divergent": 0,
    }
    identifiers: set[str] = set()
    if not summary["present"]:
        return summary, identifiers
    rows = 0
    divergent = 0
    for row in repository._iter_pipeline_job_records(include_direct=False):
        rows += 1
        job_id = str(row.get("job_id") or "")
        identifiers.add(job_id)
        scopes = _scope_mismatch(repository, row)
        if scopes is not None:
            divergent += 1
            _record_divergence(divergences, job_id, scopes, surface="journal_replay")
    summary["rows"] = rows
    summary["divergent"] = divergent
    return summary, identifiers


def _census_reconcile_inventory(
    repository: FileOrchestrationJournalRepository,
    divergences: dict[str, _Divergence],
) -> tuple[dict[str, Any], set[str]]:
    """Anchors, enumerated lock-free and validated purely.

    The listing rules mirror ``_reconcile_inventory_entry_names_unlocked``
    exactly -- the over-limit sentinel and a name that is neither a safe
    ``.json`` anchor nor well-formed residue both fail loud -- with the ONE
    difference that residue is counted and left on disk instead of removed.
    An anchor's mapping carries the row's own ``source_id``/``cycle_time``
    (written by ``_sync_reconcile_inventory_for_row_unlocked``), so the same
    gate call classifies it and an anchor of a divergent row is itself
    divergent.
    """

    directory = repository.root / _RECONCILE_INVENTORY_DIRECTORY
    if not _directory_present(repository, directory):
        return {"present": False, "files": 0, "rows": 0, "divergent": 0, "residue": 0}, set()
    try:
        entry_names = list_directory_no_follow_limited(
            directory,
            containment_root=repository.root,
            max_entries=repository.max_files,
        )
    except FileNotFoundError:
        return {"present": False, "files": 0, "rows": 0, "divergent": 0, "residue": 0}, set()
    except (OSError, SafeFilesystemError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_reconcile_inventory_unavailable",
            field="reconcile_inventory",
        ) from error
    if len(entry_names) > repository.max_files:
        raise FileOrchestrationJournalError(
            "file_journal_record_limit_exceeded",
            field="reconcile_inventory",
        )
    anchor_names: list[str] = []
    residue = 0
    for entry_name in sorted(entry_names):
        if entry_name.endswith(".json") and _SAFE_SEGMENT_RE.fullmatch(entry_name) is not None:
            anchor_names.append(entry_name)
            continue
        match = _RECONCILE_INVENTORY_TEMP_RE.fullmatch(entry_name)
        if match is not None and _SAFE_SEGMENT_RE.fullmatch(match.group("target")) is not None:
            # Reported, never removed: residue removal is a WRITE, and it
            # belongs to the lock-holding lane, not to a census.
            residue += 1
            continue
        raise FileOrchestrationJournalError(
            "file_journal_reconcile_inventory_invalid",
            field="reconcile_inventory",
        )
    identifiers: set[str] = set()
    anchors = 0
    divergent = 0
    for entry_name in anchor_names:
        payload = repository._read_optional_json(directory / entry_name)
        if payload is None:
            continue
        expected_job_id = entry_name[: -len(".json")]
        repository._validated_reconcile_inventory_anchor(payload, expected_job_id=expected_job_id)
        anchors += 1
        identifiers.add(expected_job_id)
        scopes = _scope_mismatch(repository, payload)
        if scopes is not None:
            divergent += 1
            # ``surface=None``: an anchor is not a row.  The id is registered so
            # an anchor-only divergent id is still counted and still flagged,
            # but it never joins a row's ``surfaces`` list.
            _record_divergence(divergences, expected_job_id, scopes, surface=None)
    summary = {
        "present": True,
        "files": len(entry_names),
        "rows": anchors,
        "divergent": divergent,
        "residue": residue,
    }
    return summary, identifiers


def census_job_id_scope(
    journal_root: str | Path,
    *,
    max_files: int | None = None,
    max_records: int | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Census one journal root and return the receipt mapping.

    ``max_files`` / ``max_records`` override the repository budgets: the replay
    charges every latest view, segment record and direct record against one
    record budget, and a large production journal can legitimately need more
    than the default.  A trip fails loud; the override is the documented way
    past it, never a skip.
    """

    verified_root = verify_journal_root_authority(journal_root, setting="--journal-root")
    overrides: dict[str, int] = {}
    if max_files is not None:
        overrides["max_files"] = int(max_files)
    if max_records is not None:
        overrides["max_records"] = int(max_records)
    repository = FileOrchestrationJournalRepository(str(verified_root), **overrides)

    divergences: dict[str, _Divergence] = {}
    flat_summary, flat_ids = _census_flat_direct(repository, divergences)
    by_cycle_summary, by_cycle_ids = _census_direct_files(
        repository,
        divergences,
        directory=repository.root / "pipeline-jobs" / "by-cycle",
        surface="by_cycle_direct",
        recursive=True,
    )
    replay_summary, journal_ids = _census_journal_replay(repository, divergences)
    inventory_summary, anchor_ids = _census_reconcile_inventory(repository, divergences)
    active_summary, _active_ids = _census_direct_files(
        repository,
        divergences,
        directory=repository.root / _LEGACY_ACTIVE_RECONCILE_DIRECTORY,
        surface="active_reconcile",
        recursive=False,
    )

    divergent_rows: list[dict[str, Any]] = []
    triggers = 0
    for job_id in sorted(divergences):
        entry = divergences[job_id]
        anchor_present = job_id in anchor_ids
        flat_direct_present = job_id in flat_ids
        trigger = anchor_present and not flat_direct_present
        triggers += 1 if trigger else 0
        divergent_rows.append(
            {
                "job_id": job_id,
                "surfaces": sorted(entry.surfaces),
                "own_scope": entry.own_scope,
                "job_id_scope": entry.job_id_scope,
                "anchor_present": anchor_present,
                "flat_direct_present": flat_direct_present,
                "by_cycle_present": job_id in by_cycle_ids,
                "journal_present": job_id in journal_ids,
                "reconcile_abort_trigger": trigger,
            }
        )
    generated_at = (now() if now is not None else datetime.now(UTC)).astimezone(UTC)
    return {
        "schema_version": CENSUS_SCHEMA_VERSION,
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "journal_root": str(journal_root),
        "journal_root_verified": str(verified_root),
        "limits": {"max_files": repository.max_files, "max_records": repository.max_records},
        "surfaces": {
            "flat_direct": flat_summary,
            "by_cycle_direct": by_cycle_summary,
            "journal_replay": replay_summary,
            "reconcile_inventory": inventory_summary,
            "active_reconcile": active_summary,
        },
        "divergent_rows": divergent_rows,
        "divergent_total": len(divergent_rows),
        "reconcile_abort_triggers": triggers,
        "exit_code": 2 if divergent_rows else 0,
    }


def _require_output_outside_root(output: str, verified_root: Path) -> Path:
    """Refuse a receipt path whose realpath lies at or under the verified root."""

    target = Path(output).expanduser()
    resolved = Path(os.path.realpath(target))
    root_real = Path(os.path.realpath(verified_root))
    if resolved == root_real or root_real in resolved.parents:
        raise OrchestratorError(
            "CENSUS_OUTPUT_INSIDE_JOURNAL_ROOT",
            _OUTPUT_INSIDE_ROOT_MESSAGE,
            {"journal_root": str(verified_root), "output": str(output)},
        )
    return target


def _census_command_result(
    *,
    journal_root: str,
    max_files: int | None,
    max_records: int | None,
    output: str | None,
    emit: Callable[[str], None],
) -> int:
    """The command body shared verbatim by both entrypoints.

    The root is verified here as well as inside :func:`census_job_id_scope` so
    an invalid root is refused BEFORE the ``--output`` containment question can
    even be asked against it; the helper is idempotent and does no I/O beyond
    the no-follow walk.

    ``emit`` publishes the receipt to stdout and is called BEFORE the optional
    ``--output`` write, so a receipt path that turns out to be unwritable costs
    the file and not the census: an unguarded write here would leak an
    ``OSError`` traceback and throw away a run that on node-22 takes minutes.
    The in-root refusal is unaffected -- it raises before the census runs, so
    nothing has been emitted when it fires.
    """

    verified_root = verify_journal_root_authority(journal_root, setting="--journal-root")
    target = _require_output_outside_root(output, verified_root) if output else None
    receipt = census_job_id_scope(journal_root, max_files=max_files, max_records=max_records)
    rendered = json.dumps(receipt, sort_keys=True)
    emit(rendered)
    if target is not None:
        try:
            target.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:
            raise OrchestratorError(
                "CENSUS_OUTPUT_UNWRITABLE",
                OUTPUT_UNWRITABLE_MESSAGE,
                {"error_type": type(error).__name__, "output": str(target)},
            ) from error
    return int(receipt["exit_code"])


def _census_error_line(error: BaseException) -> str:
    if isinstance(error, OrchestratorError):
        return f"{error.error_code}: {error.message}"
    if isinstance(error, FileOrchestrationJournalError):
        return f"{error.reason}: {error.field}"
    raise error


def register_click_census_command(cli: Any) -> None:
    """Register the ``census-job-id-scope`` Click command on ``cli``."""

    import click

    @cli.command(CENSUS_JOB_ID_SCOPE_COMMAND, help=CENSUS_JOB_ID_SCOPE_HELP)
    @click.option("--journal-root", required=True)
    @click.option("--max-files", default=None, type=int, help=MAX_FILES_HELP)
    @click.option("--max-records", default=None, type=int, help=MAX_RECORDS_HELP)
    @click.option("--output", default=None, help=_OUTPUT_HELP)
    def census_job_id_scope_command(
        journal_root: str,
        max_files: int | None,
        max_records: int | None,
        output: str | None,
    ) -> None:
        try:
            exit_code = _census_command_result(
                journal_root=journal_root,
                max_files=max_files,
                max_records=max_records,
                output=output,
                emit=click.echo,
            )
        except (OrchestratorError, FileOrchestrationJournalError) as error:
            click.echo(_census_error_line(error), err=True)
            raise SystemExit(1) from error
        if exit_code != 0:
            raise SystemExit(exit_code)


def add_argparse_census_subparser(subparsers: Any) -> None:
    """Add the ``census-job-id-scope`` argparse subparser to ``subparsers``."""

    census_parser = subparsers.add_parser(
        CENSUS_JOB_ID_SCOPE_COMMAND,
        help=CENSUS_JOB_ID_SCOPE_HELP,
        description=CENSUS_JOB_ID_SCOPE_HELP,
    )
    census_parser.add_argument("--journal-root", required=True)
    census_parser.add_argument("--max-files", type=int, default=None, help=MAX_FILES_HELP)
    census_parser.add_argument("--max-records", type=int, default=None, help=MAX_RECORDS_HELP)
    census_parser.add_argument("--output", default=None, help=_OUTPUT_HELP)


def run_argparse_census_command(args: Any) -> int:
    """Dispatch one argparse ``census-job-id-scope`` invocation."""

    try:
        exit_code = _census_command_result(
            journal_root=args.journal_root,
            max_files=args.max_files,
            max_records=args.max_records,
            output=args.output,
            emit=print,
        )
    except (OrchestratorError, FileOrchestrationJournalError) as error:
        print(_census_error_line(error), file=sys.stderr)
        return 1
    return exit_code


__all__ = [
    "ROW_SURFACES",
    "CENSUS_JOB_ID_SCOPE_COMMAND",
    "CENSUS_JOB_ID_SCOPE_HELP",
    "CENSUS_SCHEMA_VERSION",
    "MAX_FILES_HELP",
    "MAX_RECORDS_HELP",
    "OUTPUT_UNWRITABLE_MESSAGE",
    "add_argparse_census_subparser",
    "census_job_id_scope",
    "register_click_census_command",
    "run_argparse_census_command",
]
