"""Parity lock for the hydro-run status sets consolidated by #1581 (design D5).

Every hydro-run active, durable-success and error-code-clearing consumer reads
ONE shared object defined in ``scheduler_state_types``, and every member of
those sets is a ``hydro.run_status`` enum member as declared by the migrations
-- with ``"complete"`` as the single named exception, kept because it is
unreachable on the closed-enum database lane but is written by the file
journal's test construction face on a lane that never validates
``hydro_run.status``, so removing it would change journal decisions.

#1581 left ``ACTIVE_HYDRO_STATUSES`` as three copies and this suite locked that
divergence as UNADJUDICATED. It is adjudicated now: ``"pending"`` is active, the
copies are gone, and the lock asserts identity across all six sites instead.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from services.orchestrator import chain as chain_module
from services.orchestrator import chain_forecast_trigger as chain_forecast_trigger_module
from services.orchestrator import chain_repository as chain_repository_module
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import scheduler_state_decision as scheduler_state_decision_module
from services.orchestrator import scheduler_state_failure as scheduler_state_failure_module
from services.orchestrator import scheduler_state_manual_retry as scheduler_state_manual_retry_module
from services.orchestrator import scheduler_state_types as scheduler_state_types_module

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Swept whole, not two named files: the enum's membership is whatever the WHOLE
# migration tree declares, so a migration added tomorrow is inside the oracle.
_MIGRATIONS_DIR = _REPO_ROOT / "db" / "migrations"

# Postgres accepts either segment of the identifier bare or double-quoted, and
# treats all four spellings as the same type. A sweep that reads only the bare
# form is blind to `hydro."run_status"`, so a migration written that way would
# add a member this oracle never sees.
_RUN_STATUS_IDENT = r'"?hydro"?\."?run_status"?'
_CREATE_RUN_STATUS_RE = re.compile(
    rf"CREATE\s+TYPE\s+{_RUN_STATUS_IDENT}\s+AS\s+ENUM\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_ADD_RUN_STATUS_VALUE_RE = re.compile(
    rf"ALTER\s+TYPE\s+{_RUN_STATUS_IDENT}\s+ADD\s+VALUE(?:\s+IF\s+NOT\s+EXISTS)?\s+'([^']+)'",
    re.IGNORECASE,
)
# Not modelled, refused: `RENAME VALUE 'a' TO 'b'` and `RENAME TO other_type`
# both leave CREATE/ADD VALUE text untouched, so the swept member table would go
# on naming a label the database no longer has -- green while wrong.
_RENAME_RUN_STATUS_RE = re.compile(
    rf"ALTER\s+TYPE\s+{_RUN_STATUS_IDENT}\s+RENAME\s+(?:VALUE|TO)\b",
    re.IGNORECASE,
)


def _hydro_run_status_enum_members() -> frozenset[str]:
    """The declared ``hydro.run_status`` members, swept from the migration tree as text.

    The migrations are the oracle here rather than a live database: this suite is
    the local (DB-free) lane. The sweep reads EVERY ``db/migrations/**/*.sql``
    file rather than the two files that happen to mention the type today, which
    is what makes "the enum is closed after ``000013``" an executable claim
    instead of a comment: a future ``ALTER TYPE hydro.run_status ADD VALUE
    'complete'`` migration enters ``added`` here and turns
    ``test_every_member_is_a_declared_enum_member_except_complete`` red, where a
    two-path parser would never have looked at it.

    Both identifier spellings count: each segment may be bare or double-quoted
    (``hydro.run_status``, ``"hydro".run_status``, ``hydro."run_status"``,
    ``"hydro"."run_status"``), because Postgres resolves all four to the same
    type and a migration may be written in any of them.

    Renames are NOT modelled -- the sweep fails closed on them. ``ALTER TYPE
    ... RENAME VALUE`` / ``RENAME TO`` changes what the database holds without
    touching any ``CREATE TYPE`` or ``ADD VALUE`` text, so this member table
    would keep asserting a label that no longer exists. The assertion names the
    offending file and says so, rather than guessing at the post-rename set.
    """

    migrations = sorted(_MIGRATIONS_DIR.rglob("*.sql"))
    assert migrations, f"no migration SQL found under {_MIGRATIONS_DIR}"

    declaring: list[Path] = []
    declared: set[str] = set()
    added: set[str] = set()
    renames: list[str] = []
    for migration in migrations:
        text = migration.read_text(encoding="utf-8")
        for declaration in _CREATE_RUN_STATUS_RE.finditer(text):
            declaring.append(migration)
            declared.update(re.findall(r"'([^']+)'", declaration.group(1)))
        added.update(_ADD_RUN_STATUS_VALUE_RE.findall(text))
        if _RENAME_RUN_STATUS_RE.search(text):
            renames.append(migration.name)

    # Checked FIRST: after a rename every downstream conclusion drawn from
    # `declared | added` is a fiction, including the self-checks below.
    assert not renames, (
        f"migration(s) {sorted(renames)} rename the hydro.run_status type or one of its values; "
        "this oracle does not model renames; extend it before merging such a migration"
    )

    # One declaration across the tree: a second CREATE would mean the sweep is
    # reading a type this suite does not reason about (a rename, a shadow schema).
    assert len(declaring) == 1, f"expected one CREATE TYPE hydro.run_status, found {declaring}"
    # Self-check on the parse itself. A regex that drifted onto the neighbouring
    # `hydro.run_type` / `met.cycle_status` declarations would still return a
    # non-empty set, so the two named members pin that BOTH kinds of statement
    # were reached: `succeeded` comes from the CREATE block, `pending` only from
    # an ADD VALUE (`000013`).
    assert "succeeded" in declared, f"CREATE block parsed as {sorted(declared)}"
    assert "pending" in added, f"ADD VALUE sweep parsed as {sorted(added)}"
    return frozenset(declared | added)


def test_durable_success_aliases_are_the_same_object() -> None:
    """`chain` / `chain_repository` / journal / scheduler decision / trigger seam bind the one shared set."""

    durable = scheduler_state_types_module.DURABLE_HYDRO_SUCCESS_STATUSES
    assert chain_module.COMPLETED_HYDRO_STATUSES is durable
    assert chain_repository_module.COMPLETED_HYDRO_STATUSES is durable
    # The journal's completed-pipeline probes (`:1280` / `:1361`) decide on the
    # name its own `from services.orchestrator.chain_repository import ...` at
    # `:72` bound, so the module attribute is the surface to pin, not the alias
    # it was copied from.
    assert journal_module.COMPLETED_HYDRO_STATUSES is durable
    # Same shape for the scheduler candidate decision at
    # `scheduler_state_decision:228`: a from-import binding of its own.
    assert scheduler_state_decision_module.DURABLE_HYDRO_SUCCESS_STATUSES is durable
    # `_completed_hydro_statuses` reads `chain` by `getattr` (a monkeypatch seam),
    # so it is the surface the forecast trigger actually decides on.
    assert chain_forecast_trigger_module._completed_hydro_statuses() is durable
    assert durable == {"succeeded", "parsed", "published", "complete"}


def test_durable_output_predicate_consults_the_shared_set() -> None:
    """The formerly inline literal at `scheduler_state_failure:149` reads the shared object.

    A copy would keep the equality assertions above green while deciding on its
    own membership; only a mutation of the shared object separates the two.
    """

    durable = scheduler_state_types_module.DURABLE_HYDRO_SUCCESS_STATUSES
    sentinel = "parity_probe_status"
    assert sentinel not in durable

    durable.add(sentinel)
    try:
        assert scheduler_state_failure_module._durable_shud_output_exists({"hydro_status": sentinel}) is True
    finally:
        durable.discard(sentinel)

    assert scheduler_state_failure_module._durable_shud_output_exists({"hydro_status": sentinel}) is False
    assert sentinel not in scheduler_state_types_module.DURABLE_HYDRO_SUCCESS_STATUSES


def test_code_clearing_set_is_one_shared_frozenset() -> None:
    """No mutation probe: the set is a `frozenset` by the `:27962` top-level type pin."""

    shared = scheduler_state_types_module.HYDRO_RUN_CODE_CLEARING_STATUSES
    assert isinstance(shared, frozenset)
    assert shared == {"pending", "created", "succeeded", "complete", "parsed", "published"}
    assert scheduler_state_failure_module._HYDRO_RUN_CODE_CLEARING_STATUSES is shared
    assert journal_module.HYDRO_RUN_CODE_CLEARING_STATUSES is shared


def test_every_member_is_a_declared_enum_member_except_complete() -> None:
    """`"complete"` is the ONE out-of-enum member; any other one turns this red."""

    enum_members = _hydro_run_status_enum_members()
    durable = scheduler_state_types_module.DURABLE_HYDRO_SUCCESS_STATUSES
    code_clearing = scheduler_state_types_module.HYDRO_RUN_CODE_CLEARING_STATUSES

    assert scheduler_state_types_module.ACTIVE_HYDRO_STATUSES <= enum_members
    assert chain_module.ACTIVE_HYDRO_STATUSES <= enum_members
    assert chain_repository_module.ACTIVE_HYDRO_STATUSES <= enum_members
    assert journal_module.ACTIVE_HYDRO_STATUSES <= enum_members

    assert "complete" not in enum_members
    assert durable - {"complete"} <= enum_members
    assert code_clearing - {"complete"} <= enum_members


def test_active_hydro_statuses_are_the_same_object() -> None:
    """The six sites bind ONE set, and `"pending"` is in it.

    Six sites: the four binding sites -- ``scheduler_state_types`` (the
    definition), ``chain``, ``chain_repository``, ``file_orchestration_journal``
    -- plus the two lanes that DECIDE on the set without re-exporting it:
    ``scheduler_state_decision`` (``:194``, the candidate-state active/duplicate
    verdict) and ``scheduler_state_manual_retry`` (``:524``, ``:729``, ``:885``,
    the manual-retry blocker verdicts).

    Requirement-driven replacement of #1581's
    ``test_active_hydro_status_divergence_is_locked_not_adjudicated``, which
    asserted the very divergence this change removes. ``"pending"`` is the
    status manual retry writes to ``hydro_run`` once the retry job is submitted,
    so it is in flight; the SQL active-pipeline probe and the file journal now
    agree with the scheduler decision lane, which always counted it.

    Identity, not equality: two equal copies would keep an equality assertion
    green while each module decided on its own membership, which is exactly the
    drift that produced the ``"pending"``-less literals.

    Deliberately NOT pinned: the pure re-export surfaces that import the name
    and never test membership against it -- ``scheduler.py:74``,
    ``scheduler_state.py:157`` and the ``scheduler_state_compat.py:12``
    ``__all__`` entry. A copy there decides nothing, so pinning them would
    lock import plumbing rather than behaviour.
    """

    active = scheduler_state_types_module.ACTIVE_HYDRO_STATUSES
    assert chain_module.ACTIVE_HYDRO_STATUSES is active
    assert chain_repository_module.ACTIVE_HYDRO_STATUSES is active
    # The journal's own ``from ... import ACTIVE_HYDRO_STATUSES`` binding is the
    # surface its five decision sites read -- the ``has_active_pipeline`` probe
    # (``:1241``), the three attempt-scoped write paths
    # (``reject_pipeline_job_submit_attempt:3703``,
    # ``permit_pipeline_job_retry:4064``,
    # ``demote_operator_verified_reserved_job:4226``) and the cohort projection
    # (``project_forecast_cohort_tasks:4968``) -- so the module attribute is
    # what must be pinned, not the module it used to be copied from.
    assert journal_module.ACTIVE_HYDRO_STATUSES is active
    # The two deciding lanes hold no re-export of their own: they `from ...
    # import` the name and test membership against it directly, so an equal
    # copy here would silently restore the divergence #1581 left behind.
    assert scheduler_state_decision_module.ACTIVE_HYDRO_STATUSES is active
    assert scheduler_state_manual_retry_module.ACTIVE_HYDRO_STATUSES is active
    assert active == {"created", "staged", "pending", "submitted", "running"}


# The two shapes the sweep is built to survive, as executable probes rather
# than the manual scratch procedure design D5 describes (task 3.4): a migration
# that spells the type with a quoted segment, and a rename the sweep cannot
# model -- in BOTH of the spellings Postgres offers, `RENAME VALUE` and
# `RENAME TO`. All are written into a COPY of the real migration tree, so the
# real `db/migrations/` stays the oracle for every other assertion in this file.
_QUOTED_ADD_VALUE_PROBE = "ALTER TYPE hydro.\"run_status\" ADD VALUE IF NOT EXISTS 'complete';\n"
_RUN_STATUS_RENAME_PROBE = "ALTER TYPE hydro.run_status RENAME VALUE 'succeeded' TO 'done';\n"
_TYPE_RENAME_PROBE = "ALTER TYPE hydro.run_status RENAME TO run_status_v2;\n"
# Negative control: a rename of the NEIGHBOURING enum. The refusal must be
# scoped to `hydro.run_status`, or every unrelated enum rename would fail-close
# this suite and the refusal would be noise instead of a signal.
_NEIGHBOUR_RENAME_PROBE = "ALTER TYPE hydro.run_type RENAME VALUE 'forecast' TO 'fcst';\n"


def _migrations_copy_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: dict[str, str]
) -> Path:
    """Point the sweep at a tmp copy of `db/migrations` plus the named probe files.

    A copy, not a bare directory holding only the probe: the sweep's own
    self-checks (one `CREATE TYPE`, `succeeded` from the CREATE block,
    `pending` from an `ADD VALUE`) must still be satisfied, so the probe is
    measured as one more migration in the real tree -- which is exactly how a
    future migration would arrive.
    """

    copy = tmp_path / "migrations"
    shutil.copytree(_MIGRATIONS_DIR, copy)
    for name, sql in probes.items():
        (copy / name).write_text(sql, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_MIGRATIONS_DIR", copy)
    return copy


def test_sweep_reads_a_quoted_identifier_add_value_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `hydro."run_status"` ADD VALUE enters the member table and turns the lock red.

    Postgres treats `hydro."run_status"` as the same type as
    `hydro.run_status`, so a migration written that way really does add an enum
    member. A bare-identifier-only sweep never sees it: the member table would
    go on saying `"complete"` is out-of-enum -- green while wrong -- which is
    the one exception the whole parity lock rests on.
    """

    declared_today = _hydro_run_status_enum_members()
    assert "complete" not in declared_today

    _migrations_copy_with(tmp_path, monkeypatch, {"000098_quoted.sql": _QUOTED_ADD_VALUE_PROBE})

    assert _hydro_run_status_enum_members() == declared_today | {"complete"}
    # ... and the exception assertion that reads the table is the thing that
    # goes red, not just the table itself.
    with pytest.raises(AssertionError, match="complete"):
        test_every_member_is_a_declared_enum_member_except_complete()


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(_RUN_STATUS_RENAME_PROBE, id="rename-value"),
        pytest.param(_TYPE_RENAME_PROBE, id="rename-to"),
    ],
)
def test_sweep_refuses_a_run_status_rename_migration(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both `ALTER TYPE ... RENAME VALUE` and `... RENAME TO` fail the sweep closed, naming the migration.

    A value rename leaves every `CREATE TYPE` / `ADD VALUE` string in the tree
    untouched, so the swept table would keep asserting `'succeeded'` -- a label
    the database no longer has. A type rename is the quieter of the two: once
    the type is `hydro.run_status_v2`, every later `ALTER TYPE
    hydro.run_status_v2 ADD VALUE` is invisible to this sweep, while the
    self-checks (one CREATE, `succeeded` from the CREATE block, `pending` from
    an ADD VALUE) all keep passing against the stale name -- green while wrong,
    with nothing in the tree to hint at it. The sweep refuses on either
    spelling instead of guessing, and the message has to name the offending
    file or the operator cannot act on it.
    """

    _migrations_copy_with(tmp_path, monkeypatch, {"000099_rename.sql": probe})

    with pytest.raises(AssertionError, match="does not model renames") as refusal:
        _hydro_run_status_enum_members()
    assert "000099_rename.sql" in str(refusal.value)


def test_sweep_ignores_a_rename_of_a_neighbouring_enum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: renaming `hydro.run_type` must NOT fail this sweep closed.

    The refusal above is only useful if it is scoped to the one type this
    suite reasons about; a refusal that fired on any enum rename would be
    indistinguishable from noise and would be deleted the first time an
    unrelated migration tripped it.
    """

    declared_today = _hydro_run_status_enum_members()

    _migrations_copy_with(tmp_path, monkeypatch, {"000099_rename.sql": _NEIGHBOUR_RENAME_PROBE})

    assert _hydro_run_status_enum_members() == declared_today
