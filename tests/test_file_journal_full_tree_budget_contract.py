"""#1953: the whole-tree fall-open is fall-CLOSED on a production-sized tree.

#1734 D4 accepted the whole-tree replay as the safe fallback for an underivable
key on the argument that it is "merely as slow as the prior behaviour".  The
node-22 measurement published under
``docs/runbooks/receipts/journal-scope-census/`` says otherwise: 54,258 latest
rows + 94,123 segment records + 5,328 direct records = 153,709 raw budget
consumes against a default budget of 100,000.  Every full-tree replay on that
tree now refuses.

Two consequences, and this module pins both:

1. A whole-tree refusal and a cycle-scoped refusal carry the SAME reason token
   and the SAME field, so without a lane tag an operator cannot tell "the whole
   journal is too big" from "this one cycle is".
2. The five query entrypoints turn that refusal into a synthetic row.  The row
   is load-bearing -- it keeps the duplicate-submission guards closed -- so it
   must stay present and non-terminal; what it must NOT do is report an unread
   journal as a job that is running.

The third reach point is adjudicated differently and is pinned here too:
``query_released_identity_blocked_jobs`` wraps no handler, so it RAISES, and its
only consumer renders that as one typed CLI line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli as cli_module
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.chain_runtime_utils import (
    TERMINAL_JOB_STATUSES as RUNTIME_TERMINAL_JOB_STATUSES,
)
from services.orchestrator.chain_types import TERMINAL_JOB_STATUSES
from services.orchestrator.file_orchestration_journal import (
    FILE_JOURNAL_READ_BLOCKED_STATUS,
    RELEASED_RESERVATION_RECOVERY_COMMAND,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.test_file_orchestration_journal import (
    _dt,
    _populate_narrowing_journal,
    _released_identity_blocked_master,
)
from workers.data_adapters.base import cycle_id_for

#: The two static pins below read repository files, so they must not depend on
#: the process working directory (the suite may be invoked from anywhere).
REPO_ROOT = Path(__file__).resolve().parents[1]

_ENTRYPOINTS = ("click", "argparse")
_NARROWED_CYCLE = _dt("2026-06-28T00:00:00Z")
#: Neither shape resolves to a ``(source_id, cycle)``, so both entrypoints below
#: take the whole-tree fall-open rather than a cycle-scoped read.
_UNDERIVABLE_CYCLE_ID = "unknown-source_2026062800"


def _budgeted(tmp_path: Path, *, max_records: int = 1) -> FileOrchestrationJournalRepository:
    """A journal too large for its own budget, built by the production writers."""

    repository = _populate_narrowing_journal(tmp_path)
    return FileOrchestrationJournalRepository(repository.root, max_records=max_records)


def _invoke(entrypoint: str, args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    if entrypoint == "click":
        try:
            code = cli_module._click_main(args)
        except SystemExit as exit_error:
            code = int(exit_error.code or 0)
    else:
        code = cli_module._argparse_main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# 1. The refusal names its lane (task 2.1 / 2.5)
# ---------------------------------------------------------------------------
def test_whole_tree_and_cycle_scoped_budget_refusals_name_different_lanes(tmp_path: Path) -> None:
    """Same reason token, same field, different lane -- that is the whole point.

    The token and the field are deliberately unchanged: the census runbook, the
    census's own stderr assertions and #1810's pinned tests all match on them.
    The lane is the only new evidence, and it is what separates "this tree is
    too big to replay whole" from "this one cycle is".
    """

    budgeted = _budgeted(tmp_path)

    whole_tree = budgeted.query_pipeline_job_by_slurm_id("no-such-slurm-id")
    cycle_scoped = budgeted.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", _NARROWED_CYCLE))

    assert whole_tree is not None
    assert len(cycle_scoped) == 1
    for row in (whole_tree, cycle_scoped[0]):
        assert row["error_code"] == "file_journal_record_limit_exceeded"
        assert row["file_journal"]["reason"] == "file_journal_record_limit_exceeded"
        assert row["file_journal"]["field"] == "pipeline_job_records"
    assert whole_tree["file_journal"]["evidence"] == {"lane": "full_tree_replay"}
    assert cycle_scoped[0]["file_journal"]["evidence"] == {"lane": "cycle_replay"}


def test_a_budget_without_a_lane_adds_nothing_to_its_evidence() -> None:
    """The third construction site (``rollback_scope_records``) is untouched.

    A defaulted lane STRING would silently turn that lane's evidence from ``{}``
    into ``{"lane": ...}``, which is a payload change on a surface #1953 does not
    touch.  The default is therefore "no lane", and the key appears only when a
    caller names one.
    """

    budget = journal_module._RecordBudget(1, "rollback_scope_records")
    budget.consume()

    with pytest.raises(FileOrchestrationJournalError) as caught:
        budget.consume()

    assert caught.value.reason == "file_journal_record_limit_exceeded"
    assert caught.value.field == "rollback_scope_records"
    assert caught.value.evidence == {}


# ---------------------------------------------------------------------------
# 2. The blocked row's vocabulary and its guard-visible properties (2.2 / 2.4)
# ---------------------------------------------------------------------------
def test_blocked_read_does_not_claim_the_job_is_running(tmp_path: Path) -> None:
    """The row says it is a blocked read; every identifier is unchanged.

    ``"running"`` was a lie with consequences: the recovery listing, the retry
    route and any operator reading a receipt saw an unread journal reported as a
    job in flight, and the row was indistinguishable from a genuinely running
    one except by the ``file_journal`` marker.
    """

    budgeted = _budgeted(tmp_path)

    by_cycle = budgeted.query_pipeline_jobs_by_cycle(_UNDERIVABLE_CYCLE_ID)

    assert by_cycle == [
        {
            "job_id": "file_journal_read_blocked",
            "idempotency_key": None,
            "cycle_id": _UNDERIVABLE_CYCLE_ID,
            "run_id": None,
            "slurm_job_id": "unknown_after_attempt",
            "status": FILE_JOURNAL_READ_BLOCKED_STATUS,
            "stage": "file_journal_read",
            "error_code": "file_journal_record_limit_exceeded",
            "file_journal": {
                "status": "blocked",
                "reason": "file_journal_record_limit_exceeded",
                "field": "pipeline_job_records",
                "evidence": {"lane": "full_tree_replay"},
            },
        }
    ]
    assert FILE_JOURNAL_READ_BLOCKED_STATUS != "running"


def test_get_pipeline_job_blocked_row_keeps_the_real_job_id(tmp_path: Path) -> None:
    """``job_id`` defaults are unchanged by #1953 (design D7).

    The retry route's 503 pin depends on ``get_pipeline_job`` keeping the REAL
    id, so only the status literal moves.  #2385/#2387: the retry-lane consumers
    no longer compare ``job_id`` at all -- they key on the ``file_journal``
    marker through ``_is_blocked_query_job``, because the by-run and by-cycle
    lanes keep the DEFAULT id while this lane keeps the real one, so no identity
    field discriminates all five lanes.
    """

    budgeted = _budgeted(tmp_path)
    # No direct record, and a shape that resolves to no ``(source, cycle)``, so
    # the lookup falls open to the whole-tree replay and meets the budget there.
    job_id = "cycle_gfs_2026062800_retry_active"
    assert journal_module._cycle_scope_from_job_id(job_id) is None

    row = budgeted.get_pipeline_job(job_id)

    assert row is not None
    assert row["job_id"] == job_id
    assert row["status"] == FILE_JOURNAL_READ_BLOCKED_STATUS
    assert row["file_journal"]["status"] == "blocked"
    assert row["file_journal"]["evidence"] == {"lane": "full_tree_replay"}


def test_blocked_row_stays_in_flight_for_every_scheduling_guard(tmp_path: Path) -> None:
    """Task 2.4: present, non-terminal, not reusable, and still a conflict.

    Returning ``None`` or ``[]`` would re-enable duplicate reservation and
    duplicate cycle scheduling, so the row's PRESENCE and its non-terminality
    are the properties the new literal must not disturb.  The terminal check is
    made against the imported sets rather than a literal list, so a future
    member added to either set is caught here.
    """

    budgeted = _budgeted(tmp_path)

    single = budgeted.query_pipeline_job_by_slurm_id("no-such-slurm-id")
    by_cycle = budgeted.query_pipeline_jobs_by_cycle(_UNDERIVABLE_CYCLE_ID)
    by_run = budgeted.query_pipeline_jobs_by_run("no-such-run-id")
    candidate = budgeted.query_candidate_state("no-such-idempotency-key")

    assert TERMINAL_JOB_STATUSES == RUNTIME_TERMINAL_JOB_STATUSES
    assert single is not None
    assert candidate is not None
    assert len(by_cycle) == 1
    assert len(by_run) == 1
    for row in (single, candidate, by_cycle[0], by_run[0]):
        assert row["status"] == FILE_JOURNAL_READ_BLOCKED_STATUS
        assert row["status"] not in TERMINAL_JOB_STATUSES
        assert row["status"] not in journal_module.TERMINAL_PIPELINE_STATUSES
        assert row["file_journal"]["status"] == "blocked"
        assert journal_module._file_auto_retry_job_can_be_reused(row) is False

    # The duplicate-submission guard keys on presence, and presence survives.
    conflicting = {"job_id": "job_cycle_gfs_2026062800_forecast", "idempotency_key": None}
    assert budgeted._pipeline_job_conflicts_unlocked(conflicting) is True


def test_the_three_sibling_blocked_sentinels_keep_their_own_vocabulary(tmp_path: Path) -> None:
    """Design D7a: only ``_blocked_query_job`` changes, and deliberately so.

    ``_file_journal_blocked_candidate_state``'s ``pipeline_status`` feeds the
    ALLOWLIST ``ACTIVE_PIPELINE_STATUSES``, so a new literal there would flip
    the db-free scheduler from fail-closed to fail-open.  The other two are
    projections whose consumers adjudicate on different allowlists again.
    """

    from services.orchestrator.scheduler_state_types import ACTIVE_PIPELINE_STATUSES

    error = FileOrchestrationJournalError("file_journal_unreadable", field="journal")

    stage_status = journal_module._blocked_stage_status(
        error,
        source_id="gfs",
        cycle_time=_NARROWED_CYCLE,
        model_id="model_a",
    )
    candidate_state = journal_module._file_journal_blocked_candidate_state(
        error,
        source_id="gfs",
        cycle_time=_NARROWED_CYCLE,
        model_id="model_a",
        run_id="fcst_gfs_2026062800_model_a",
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id="candidate_a",
        retry_limit=None,
        job_limit=10,
        event_limit=10,
    )

    assert stage_status["status"] == "running"
    assert candidate_state["pipeline_status"] == "running"
    assert candidate_state["pipeline_jobs"][0]["status"] == "running"
    # The allowlist is why: a status outside it reads as "not active".
    assert candidate_state["pipeline_status"] in ACTIVE_PIPELINE_STATUSES
    assert FILE_JOURNAL_READ_BLOCKED_STATUS not in ACTIVE_PIPELINE_STATUSES


def test_the_blocked_status_is_outside_every_closed_status_enum() -> None:
    """The literal is synthesised per call and must never be written or published.

    ``PIPELINE_JOB_STATUS_VALUES`` is a CLOSED enum published into the OpenAPI
    document, and ``schemas/pipeline_job.schema.json`` carries the durable one.
    The blocked row is safe by reachability, not by permissiveness, so both must
    keep rejecting the literal.
    """

    import json

    from apps.api.routes.pipeline import _ACTIVE_JOB_STATUSES, PIPELINE_JOB_STATUS_VALUES

    schema = json.loads(
        (REPO_ROOT / "schemas" / "pipeline_job.schema.json").read_text(encoding="utf-8")
    )
    durable_enum = schema["properties"]["status"]["enum"]

    assert FILE_JOURNAL_READ_BLOCKED_STATUS not in PIPELINE_JOB_STATUS_VALUES
    assert FILE_JOURNAL_READ_BLOCKED_STATUS not in _ACTIVE_JOB_STATUSES
    assert FILE_JOURNAL_READ_BLOCKED_STATUS not in durable_enum


# ---------------------------------------------------------------------------
# 3. Reach point 3: the unscoped branch raises (task 2.5a / design D7b)
# ---------------------------------------------------------------------------
def test_unscoped_released_identity_listing_raises_instead_of_synthesising_a_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``unscoped`` fall-open wraps no handler, and that is the right shape.

    A synthetic row here would be a fabricated wedge entry an operator might act
    on.  Raising hands the refusal to the one consumer that can render it.  The
    precondition is injected at the same seam #1810's own fall-open test uses:
    every row the flat reader yields carries a round-tripping ``cycle_id``, so
    ``_released_candidate_cycle_scope`` cannot return ``None`` from data.
    """

    repository, _record = _released_identity_blocked_master(tmp_path)
    monkeypatch.setattr(journal_module, "_released_candidate_cycle_scope", lambda job: None)
    budgeted = FileOrchestrationJournalRepository(repository.root, max_records=1)

    with pytest.raises(FileOrchestrationJournalError) as caught:
        budgeted.query_released_identity_blocked_jobs()

    assert caught.value.reason == "file_journal_record_limit_exceeded"
    assert caught.value.field == "pipeline_job_records"
    assert caught.value.evidence == {"lane": "full_tree_replay"}


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_recovery_command_renders_the_raised_refusal_as_one_typed_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """#1953 acceptance item 2: a typed single line, never a traceback.

    The scoped confirmation branch raises for the same reason on the same
    budget, so this reaches the consumer without injecting anything.
    """

    repository, _record = _released_identity_blocked_master(tmp_path)
    monkeypatch.setattr(
        journal_module.FileOrchestrationJournalRepository,
        "__init__",
        _budget_capped_init(journal_module.FileOrchestrationJournalRepository.__init__),
    )

    code, out, err = _invoke(
        entrypoint,
        [RELEASED_RESERVATION_RECOVERY_COMMAND, "--journal-root", str(repository.root)],
        capsys,
    )

    assert code == 2
    assert out.strip() == ""
    assert err.strip() == "file_journal_record_limit_exceeded"
    assert "Traceback" not in err
    assert str(repository.root) not in err


def _budget_capped_init(original: Any) -> Any:
    """Force every repository built inside the CLI onto a one-record budget.

    The command offers no ``--max-records`` knob, so the budget is the only
    boundary that can be moved to reproduce the node-22 refusal locally.
    """

    def wrapper(self: Any, journal_root: Any, **kwargs: Any) -> None:
        original(self, journal_root, **{**kwargs, "max_records": 1})

    return wrapper


# ---------------------------------------------------------------------------
# 4. The kept entrypoint stays caller-free (task 2.6 / design D8)
# ---------------------------------------------------------------------------
#: Where the name is ALLOWED to appear: the Protocol declaration, the two
#: concrete lane definitions and the two parity lists.  #1734 recorded "leave"
#: for this entrypoint; the fact that keeps that decision cheap is that nothing
#: in production calls it, and that fact is what rots silently.
_ALLOWED_SLURM_ID_LOOKUP_SITES = {
    "services/orchestrator/chain.py",
    "services/orchestrator/chain_repository.py",
    "services/orchestrator/file_orchestration_journal.py",
    "services/orchestrator/chain_compat_static.py",
}
_PRODUCTION_ROOTS = ("services", "apps", "workers", "packages", "scripts")


def test_query_pipeline_job_by_slurm_id_has_no_production_caller() -> None:
    """Static pin, in the spirit of the existing #1734 D1a / I8 reachability pin.

    A behavioural pin can only show that the entrypoint still works; it cannot
    show that nothing reaches it.  ``query_pipeline_job_by_slurm_id`` is the one
    lookup that still replays the whole tree, so on a production-sized journal a
    new caller would inherit a ~91 s refusal.  Keeping it is only defensible
    while it stays uncalled.
    """

    found: dict[str, list[str]] = {}
    for root in _PRODUCTION_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if "query_pipeline_job_by_slurm_id" in line
            ]
            if lines:
                found[path.relative_to(REPO_ROOT).as_posix()] = lines

    assert set(found) == _ALLOWED_SLURM_ID_LOOKUP_SITES, sorted(found)
    for location, lines in found.items():
        for line in lines:
            # A definition (``def ...``) or a parity-list entry (a bare string),
            # never an invocation.
            assert ".query_pipeline_job_by_slurm_id(" not in line, f"{location}: {line}"
