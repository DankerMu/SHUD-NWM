"""Behavioural seams of the file-journal repository mixins (#3350).

Tests patch names on ``services.orchestrator.file_orchestration_journal``.  A
repository-mixin method that moved into a ``file_journal_*`` sibling only sees
such a patch when it reads the name off the facade module at call time.  The
cluster map (``tests/fixtures/file_journal_cluster_map.json``) records, per
facade-resident patched name, every mixin that reads it; each such
``(patched name, mixin cluster)`` pair gets one driver here, on a real
``FileOrchestrationJournalRepository`` under ``tmp_path``.

Oracle.  "The stub was called" proves nothing for a mixin: every mixin write
also goes through the facade's append path and ``_journal_record_for_write``,
which read ``_utcnow`` themselves, and ``file_journal_paths`` and facade code
call the IO names too.  So each patched name is replaced by a recorder that
notes the module of its caller (``sys._getframe(1).f_globals["__name__"]``)
and delegates to the real callable, and the test asserts that the mixin's own
module is among the recorded callers.  A driver seeds first and returns its
act; the recorders are installed only around the act, so seeding calls never
count.  Each act also asserts that it took the path it is meant to drive, so a
silent refusal cannot pass for a seam read.

A driver pins the reads its act executes, not every read in the mixin: one
method that bypasses the facade while another still reads through it is the
origin rule's job (``origin_bypass_problems`` in
``tests/test_file_journal_cluster_map.py``), which covers every import-bound
name statically.  ``_stat_signature`` is the exception: the facade defines it
itself, so it has no origin and its seam driver is its only guard.  That pair
is therefore site-complete (``SITE_COMPLETE_PAIRS``): the recorder also notes
the caller's line, the act executes every read, and the test asserts that the
set of lines that read the name in the mixin's module equals the set of call
sites the mixin's source has for it.  The expected sites are every call whose
callee is named ``_stat_signature`` (the facade attribute call or a bare
``_stat_signature(...)``), matched by line number: a read inside a comprehension or generator expression runs in that
expression's frame, whose ``co_name`` is not the method's but whose line is the
read's.  Deriving the sites from the facade attribute reads alone would be
blind to the bypass it exists for (a local copy turns the attribute read into a
bare call, which would leave both sets); a copy under another name is not seen.

The recorders patch only names already in the map's ``patched`` table, with
literal attribute names, so the map's patched-name scanner stays exact.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import services.orchestrator.file_orchestration_journal as file_orchestration_journal
from packages.common.contracts.time_normalization import utc_now
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

REPO_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = REPO_ROOT / "tests" / "fixtures" / "file_journal_cluster_map.json"
_MIXIN_UNIT_RE = re.compile(r"_FileJournal\w+Mixin")
CYCLE_TIME = datetime(2026, 6, 28, tzinfo=UTC)
MASTER_JOB_ID = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
# The on-disk name of the facade's private `_RECONCILE_INVENTORY_MIGRATION_MARKER`.
MIGRATION_MARKER = "reconcile-inventory-migration-v1.json"

Act = Callable[[], None]


@dataclass(frozen=True)
class CallerSite:
    module: str
    function: str
    line: int


def _install_recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[CallerSite]]:
    """Replace the five driven patched names with caller-recording proxies; return ``name -> caller sites``."""

    callers: dict[str, list[CallerSite]] = {}

    def recorder(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        seen = callers.setdefault(name, [])

        def proxy(*args: Any, **kwargs: Any) -> Any:
            frame = sys._getframe(1)
            seen.append(CallerSite(frame.f_globals["__name__"], frame.f_code.co_name, frame.f_lineno))
            return real(*args, **kwargs)

        return proxy

    # The recorder delegates to `utc_now`, which is what the facade binds as `_utcnow` today; reading the
    # facade attribute here would make `_utcnow` a read outside the siblings
    # (tests/test_file_orchestration_journal_facade.py).  The oracle is the caller's module, so the
    # delegate's identity does not matter.
    monkeypatch.setattr(file_orchestration_journal, "_utcnow", recorder("_utcnow", utc_now))
    monkeypatch.setattr(
        file_orchestration_journal,
        "stat_no_follow",
        recorder("stat_no_follow", file_orchestration_journal.stat_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "unlink_no_follow",
        recorder("unlink_no_follow", file_orchestration_journal.unlink_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "list_directory_no_follow_limited",
        recorder("list_directory_no_follow_limited", file_orchestration_journal.list_directory_no_follow_limited),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "_stat_signature",
        recorder("_stat_signature", file_orchestration_journal._stat_signature),
    )
    return callers


def _repository(tmp_path: Path) -> FileOrchestrationJournalRepository:
    return FileOrchestrationJournalRepository(tmp_path / "journal")


def _versioned_master_record() -> dict[str, Any]:
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    return _versioned_master_reservation_record(member_count=2)


def _attempt_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    return {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(row["submission_attempt"]),
        "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
    }


# ---------------------------------------------------------------------------
# Drivers: seed under tmp_path, return the act
# ---------------------------------------------------------------------------


def _events_update_forecast_cycle_status(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        row = repository.update_forecast_cycle_status(source_id="gfs", cycle_time=CYCLE_TIME, status="discovered")
        assert row is not None and row["status"] == "discovered"

    return act


def _release_recover_released_reservation(tmp_path: Path) -> Act:
    from tests.gateway_reconcile_helpers import _file_cohort_repository

    repository = _file_cohort_repository(tmp_path, member_count=2, with_runtime_rows=False)
    kwargs = _attempt_kwargs(repository.get_accepted_submit_pipeline_job(MASTER_JOB_ID))
    assert repository.release_identity_blocked_reservation(MASTER_JOB_ID, identity_blocked_streak=1, **kwargs) == 1

    def act() -> None:
        recovered = repository.recover_released_identity_blocked_reservation(MASTER_JOB_ID, **kwargs)
        assert recovered is not None
        assert recovered[file_orchestration_journal.OPERATOR_RECOVERY_ATTESTATION_FIELD] not in (None, "")

    return act


def _reclaim_dead_reservation(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)
    record = {
        "job_id": "job_cycle_gfs_2026062800_forecast",
        "run_id": "fcst_gfs_2026062800_model_a",
        "cycle_id": "gfs_2026062800",
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "reserved",
        "stage": "forecast",
        "idempotency_key": "gfs:gfs_2026062800:basin_a:forecast",
        "candidate_id": "candidate_a",
    }
    assert repository.reserve_pipeline_job(dict(record)) is not None
    repository.update_pipeline_job_status(record["job_id"], "submission_failed", error_code="SBATCH_REJECTED")

    def act() -> None:
        reclaimed = repository.reclaim_pipeline_job_reservation(dict(record))
        assert reclaimed is not None and reclaimed["status"] == "reserved"

    return act


def _operator_demote_held_reservation(tmp_path: Path) -> Act:
    from tests.orchestrator_demote_reserved_job_helpers import JOB_ID, _demote_kwargs, _held_cohort_repository

    repository = _held_cohort_repository(tmp_path)
    kwargs = _demote_kwargs(repository.get_accepted_submit_pipeline_job(JOB_ID))

    def act() -> None:
        assert repository.demote_operator_verified_reserved_job(JOB_ID, **kwargs) is not None
        assert repository.get_accepted_submit_pipeline_job(JOB_ID)["status"] == "reservation_lost"

    return act


def _cohort_defer_mismatched_master(tmp_path: Path) -> Act:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    repository = _repository(tmp_path)
    record = _versioned_master_record()
    assert repository.reserve_pipeline_job(dict(record)) is not None
    assert repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id="7100",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).committed

    def act() -> None:
        # A terminal master id that does not match the durable accepted submit defers the projection.
        result = repository.project_forecast_cohort_tasks(
            str(record["job_id"]),
            master_slurm_job_id="7199",
            projections=[],
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert result == {"total": 1, "pipeline_status": 1, "pipeline_event": 0}

    return act


def _submit_transition_evidence(tmp_path: Path) -> Act:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository = _repository(tmp_path)
    record = _versioned_master_record()
    assert repository.reserve_pipeline_job(dict(record)) is not None

    def act() -> None:
        result = repository.transition_pipeline_job_submit_evidence(
            record["job_id"],
            AcceptedSubmitTransition.timeout(),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
        assert result.wrote

    return act


def _retention_discovery_on_an_empty_root(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert repository.discover_retention_cycles().cycles == ()

    return act


def _reconcile_inventory_migration_marker(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_pipeline_job_records()) == []
        marker = json.loads((repository.root / MIGRATION_MARKER).read_text(encoding="utf-8"))
        assert marker["completed_at"]

    return act


def _reconcile_inventory_direct_job_scan(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_direct_pipeline_job_records()) == []

    return act


def _reconcile_inventory_migration_residue_removal(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)
    repository.root.mkdir(parents=True)
    residue = repository.root / f".{MIGRATION_MARKER}.{'0' * 32}.tmp"
    residue.write_text("residue", encoding="utf-8")

    def act() -> None:
        assert list(repository._iter_reconcile_pipeline_job_records()) == []
        assert not residue.exists()

    return act


def _reconcile_inventory_anchor_listing(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_inventory_records()) == []

    return act


def _reconcile_inventory_stat_signature_reads(tmp_path: Path) -> Act:
    """Every ``_stat_signature`` read of the reconcile-inventory mixin, through the calls production makes.

    The steady-state pass reads it in ``_canonical_reconcile_job_unlocked``'s non-strict snapshot.  The strict
    inventory scan (``query_rollback_unsettled_jobs`` makes exactly this call) adds the strict recheck, and the
    strict direct-job walk with an expected root signature (the call ``_iter_rollback_scope_pipeline_job_records``
    makes) adds both root-signature comparisons.  The public rollback query itself needs a prepared rollback
    fence, so the act drives the two mixin calls it is built from.
    """

    from tests.gateway_reconcile_helpers import _file_cohort_repository

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    # The first pass runs the one-time migration; the act's passes are steady state.
    assert [row["job_id"] for row in repository._iter_reconcile_pipeline_job_records()] == [MASTER_JOB_ID]
    # Captured before the recorders are installed, so the test's own root signature is not a recorded read.
    real_signature = file_orchestration_journal._stat_signature
    direct_root = repository.root / "pipeline-jobs"

    def act() -> None:
        assert [row["job_id"] for row in repository._iter_reconcile_pipeline_job_records()] == [MASTER_JOB_ID]
        strict = repository._iter_reconcile_inventory_records(quiescence=True, strict_disappearance=True)
        assert [row["job_id"] for row in strict] == [MASTER_JOB_ID]
        walked = repository._iter_reconcile_direct_pipeline_job_paths(
            strict_disappearance=True,
            expected_root_signature=real_signature(direct_root),
        )
        assert [path.name for path in walked] == [f"{MASTER_JOB_ID}.json"]

    return act


PAIR_DRIVERS: dict[tuple[str, str], Callable[[Path], Act]] = {
    ("_utcnow", "events"): _events_update_forecast_cycle_status,
    ("_utcnow", "release"): _release_recover_released_reservation,
    ("_utcnow", "reclaim"): _reclaim_dead_reservation,
    ("_utcnow", "operator"): _operator_demote_held_reservation,
    ("_utcnow", "cohort"): _cohort_defer_mismatched_master,
    ("_utcnow", "submit"): _submit_transition_evidence,
    ("_utcnow", "reconcile_inventory"): _reconcile_inventory_migration_marker,
    ("stat_no_follow", "retention"): _retention_discovery_on_an_empty_root,
    ("stat_no_follow", "reconcile_inventory"): _reconcile_inventory_direct_job_scan,
    ("unlink_no_follow", "reconcile_inventory"): _reconcile_inventory_migration_residue_removal,
    ("list_directory_no_follow_limited", "reconcile_inventory"): _reconcile_inventory_anchor_listing,
    ("_stat_signature", "reconcile_inventory"): _reconcile_inventory_stat_signature_reads,
}
# Pairs whose act executes every call site of the name in the mixin, checked line by line.
SITE_COMPLETE_PAIRS = frozenset({("_stat_signature", "reconcile_inventory")})


def _call_site_lines(path: Path, name: str) -> set[int]:
    """Lines of every call in ``path`` whose callee is named ``name`` (an attribute or a bare name)."""

    lines: set[int] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if callee == name:
                lines.add(node.lineno)
    return lines


def _mixin_pairs_from_the_map() -> set[tuple[str, str]]:
    """Every ``(patched name, caller cluster)`` whose recorded caller units include a ``_FileJournal...Mixin``."""

    patched = json.loads(MAP_PATH.read_text(encoding="utf-8"))["patched"]
    return {
        (name, cluster)
        for name, entry in patched.items()
        for cluster, units in entry["callers"].items()
        if any(_MIXIN_UNIT_RE.fullmatch(unit) for unit in units)
    }


def test_the_maps_mixin_names_are_the_repository_bases() -> None:
    """The pair derivation keys on the ``_FileJournal...Mixin`` name; a mixin named otherwise must not drop out."""

    clusters = json.loads(MAP_PATH.read_text(encoding="utf-8"))["clusters"]
    mapped = {name for names in clusters.values() for name in names if _MIXIN_UNIT_RE.fullmatch(name)}
    bases = {base.__name__ for base in FileOrchestrationJournalRepository.__bases__} - {"object"}
    assert mapped == bases


def test_every_mixin_caller_pair_of_the_map_has_exactly_one_driver() -> None:
    derived = _mixin_pairs_from_the_map()
    missing = sorted(derived - set(PAIR_DRIVERS))
    stale = sorted(set(PAIR_DRIVERS) - derived)
    assert missing == [], f"map pairs (patched name, mixin cluster) without a seam driver: {missing}"
    assert stale == [], f"seam drivers for pairs the map no longer records: {stale}"


@pytest.mark.parametrize(
    ("name", "cluster"),
    sorted(PAIR_DRIVERS),
    ids=[f"{name}-{cluster}" for name, cluster in sorted(PAIR_DRIVERS)],
)
def test_mixin_reads_the_patched_name_off_the_facade(
    name: str, cluster: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    act = PAIR_DRIVERS[(name, cluster)](tmp_path)
    callers = _install_recorders(monkeypatch)

    act()

    module = f"services.orchestrator.file_journal_{cluster}"
    modules = sorted({site.module for site in callers[name]})
    assert module in modules, (
        f"{module} did not read `{name}` off the facade at call time; the recorded callers are {modules}"
    )
    if (name, cluster) in SITE_COMPLETE_PAIRS:
        source = REPO_ROOT / "services" / "orchestrator" / f"file_journal_{cluster}.py"
        expected = _call_site_lines(source, name)
        assert expected, f"no `{name}` call site found in {source}; the site oracle would be vacuous"
        seen = {site.line for site in callers[name] if site.module == module}
        assert seen == expected, (
            f"`{name}` call sites in {source} that did not read the facade attribute: {sorted(expected - seen)}; "
            f"facade reads at no call site: {sorted(seen - expected)}"
        )
