"""Parity lock for the hydro-run status sets consolidated by #1581 (design D5).

Every hydro-run durable-success and error-code-clearing consumer reads ONE
shared object defined in ``scheduler_state_types``, and every member of those
sets and of the three ``ACTIVE_HYDRO_STATUSES`` copies is a ``hydro.run_status``
enum member as declared by the migrations -- with ``"complete"`` as the single
named exception, kept because it is unreachable on the closed-enum database lane
but is written by the file journal's test construction face on a lane that never
validates ``hydro_run.status``, so removing it would change journal decisions.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.orchestrator import chain as chain_module
from services.orchestrator import chain_forecast_trigger as chain_forecast_trigger_module
from services.orchestrator import chain_repository as chain_repository_module
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import scheduler_state_failure as scheduler_state_failure_module
from services.orchestrator import scheduler_state_types as scheduler_state_types_module

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENUM_DECLARATION_MIGRATION = _REPO_ROOT / "db" / "migrations" / "000003_enums.sql"
_ENUM_REMEDIATION_MIGRATION = _REPO_ROOT / "db" / "migrations" / "000013_enum_remediation.sql"

_CREATE_RUN_STATUS_RE = re.compile(
    r"CREATE\s+TYPE\s+hydro\.run_status\s+AS\s+ENUM\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_ADD_RUN_STATUS_VALUE_RE = re.compile(
    r"ALTER\s+TYPE\s+hydro\.run_status\s+ADD\s+VALUE(?:\s+IF\s+NOT\s+EXISTS)?\s+'([^']+)'",
    re.IGNORECASE,
)


def _hydro_run_status_enum_members() -> frozenset[str]:
    """The declared ``hydro.run_status`` members, read from the migrations as text.

    The migrations are the oracle here rather than a live database: this suite is
    the local (DB-free) lane, and the enum is closed -- no ``ADD VALUE`` on
    ``hydro.run_status`` exists after ``000013``.
    """

    declaration = _CREATE_RUN_STATUS_RE.search(_ENUM_DECLARATION_MIGRATION.read_text(encoding="utf-8"))
    assert declaration is not None, f"no CREATE TYPE hydro.run_status in {_ENUM_DECLARATION_MIGRATION}"
    members = set(re.findall(r"'([^']+)'", declaration.group(1)))
    members.update(_ADD_RUN_STATUS_VALUE_RE.findall(_ENUM_REMEDIATION_MIGRATION.read_text(encoding="utf-8")))
    # Self-check on the parse itself. A regex that drifted onto the neighbouring
    # `hydro.run_type` / `met.cycle_status` declarations would still return a
    # non-empty set, so the two named members pin that BOTH files were read as
    # intended: `succeeded` comes from the CREATE block, `pending` only from the
    # `000013` ADD VALUE.
    assert members, "hydro.run_status enum parse produced no members"
    assert "succeeded" in members
    assert "pending" in members
    return frozenset(members)


def test_durable_success_aliases_are_the_same_object() -> None:
    """`chain` / `chain_repository` / the trigger seam bind the one shared set."""

    durable = scheduler_state_types_module.DURABLE_HYDRO_SUCCESS_STATUSES
    assert chain_module.COMPLETED_HYDRO_STATUSES is durable
    assert chain_repository_module.COMPLETED_HYDRO_STATUSES is durable
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

    assert "complete" not in enum_members
    assert durable - {"complete"} <= enum_members
    assert code_clearing - {"complete"} <= enum_members


def test_active_hydro_status_divergence_is_locked_not_adjudicated() -> None:
    """`"pending"` sits in the scheduler copy only; #1581 locks that, it does not decide it."""

    types_active = scheduler_state_types_module.ACTIVE_HYDRO_STATUSES
    assert "pending" in types_active
    assert chain_module.ACTIVE_HYDRO_STATUSES == types_active - {"pending"}
    assert chain_repository_module.ACTIVE_HYDRO_STATUSES == types_active - {"pending"}
