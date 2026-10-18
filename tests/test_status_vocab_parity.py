"""Canonical parity for ``packages/common/status_vocab.py`` (entropy-burndown task 2.1a).

Every named set in the vocabulary is compared with the literal pinned here from
its canonical site at ``f7be77f22``, and every canonical site object is checked
for that literal AND for its pre-migration Python type (a ``set`` site stays a
mutable ``set``, a ``tuple`` site stays an ordered ``tuple``). The public sets
are also compared with the JSON schemas and with the enums folded over
``db/migrations/`` by the one folding helper of
``tests/test_hydro_status_set_parity.py``.

Task 2.1b adds the duplicate-literal scan: every status literal outside the
vocabulary must be adjudicated in ``tests/status_literal_allowlist.json``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import typing
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

from apps.api.routes import hydro_display_constants
from apps.api.routes import pipeline as pipeline_route
from packages.common import forecast_store, met_store
from packages.common import status_vocab as vocab
from scripts import run_qhh_continuous
from services.orchestrator import (
    accepted_submit_identity,
    chain,
    chain_repository_state,
    chain_runtime_utils,
    chain_source_cycle,
    chain_types,
    file_orchestration_journal,
    persistence,
    retry,
    scheduler,
    scheduler_candidates,
    scheduler_state_types,
)
from services.production_closure import readiness_item_contracts, slurm_validation
from services.slurm_gateway import real_backend
from tests.test_hydro_status_set_parity import _fold_enum_members
from workers.output_parser import parser

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIPELINE_JOB_SCHEMA = _REPO_ROOT / "schemas" / "pipeline_job.schema.json"
_RUN_STATUS_SCHEMA = _REPO_ROOT / "schemas" / "run_status.schema.json"
_STATUS_VOCAB_SOURCE = _REPO_ROOT / "packages" / "common" / "status_vocab.py"

# Transitional (task 2.2 deletes it): the runtime-written `reservation_lost` is in
# `PIPELINE_JOB_STATUS_ORDER` but not yet in the public schema / API tuple.
_PENDING_PUBLIC_DELTA: tuple[str, ...] = ("reservation_lost",)

# --- Literals pinned from the canonical sites at f7be77f22 ---------------------------

_PIPELINE_API_TUPLE = (
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "permanently_failed",
    "cancelled",
    "skipped",
)
_PIPELINE_JOB_ORDER = (
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
    "cancelled",
    "skipped",
)
_INTERNAL = frozenset({"reserved", "cancellation_pending", "reconcile_unverified"})
_ACCEPTED_SUBMIT_MASTER = frozenset(
    {
        "reserved",
        "pending",
        "queued",
        "submitted",
        "running",
        "cancellation_pending",
        "reconcile_unverified",
        "submission_failed",
        "reservation_lost",
        "succeeded",
        "partially_failed",
        "failed",
        "permanently_failed",
        "cancelled",
    }
)
_TERMINAL = frozenset(
    {
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "reservation_lost",
        "permanently_failed",
    }
)
_TERMINAL_WITHOUT_RESERVATION = frozenset(
    {"succeeded", "partially_failed", "failed", "cancelled", "submission_failed", "permanently_failed"}
)
_TERMINAL_GUARD = frozenset({"succeeded", "failed", "cancelled", "permanently_failed"})
_TERMINAL_OVERRIDES = frozenset({"partially_failed", "permanently_failed"})
_ACTIVE_MANUAL_RETRY = ("pending", "queued", "submitted", "running")
_SUCCESS = frozenset({"succeeded", "complete", "published"})
_ACTIVE = frozenset({"pending", "queued", "submitted", "running"})
_PENDING = frozenset({"pending", "queued"})
_FAILED_UI = frozenset({"failed", "submission_failed", "permanently_failed", "cancelled"})
_FAILED_PIPELINE = frozenset({"failed", "submission_failed", "partially_failed", "permanently_failed"})
_RESERVATION_ACTIVE = ("reserved", "submitted", "running", "queued", "pending")
_HYDRO_RUN_ORDER = (
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "blocked",
    "unavailable",
    "partial",
    "parsed",
    "published",
    "failed",
    "cancelled",
    "superseded",
)
_ACTIVE_HYDRO = frozenset({"created", "staged", "pending", "submitted", "running"})
_DURABLE_HYDRO_SUCCESS = frozenset({"succeeded", "parsed", "published", "complete"})
_CODE_CLEARING = frozenset({"pending", "created", "succeeded", "complete", "parsed", "published"})
_HYDRO_TERMINAL_UI = frozenset({"succeeded", "parsed", "published", "failed", "cancelled", "superseded"})
_DISPLAY_READY = frozenset({"succeeded", "parsed", "published"})
_QHH_LATEST_READY = ("succeeded", "parsed", "published")
_PARSE_READY = ("succeeded", "parsed", "failed")
_FAILABLE = ("created", "staged", "submitted", "running", "succeeded", "parsed")
_FORECAST_CYCLE_SUCCESS = frozenset({"complete", "succeeded", "parsed", "published"})
# `met.cycle_status` as 000003 creates it (16, in CREATE order); 000027 adds
# `canonical_incomplete` AFTER `canonical_ready`, 000013 adds `cancelled` AFTER
# `failed_publish`.
_CYCLE_CREATE_ORDER = (
    "discovered",
    "downloading",
    "raw_complete",
    "canonical_ready",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
)
_CYCLE_ADDED_AFTER = {"canonical_incomplete": "canonical_ready", "cancelled": "failed_publish"}
_CYCLE_ORDER = (
    "discovered",
    "downloading",
    "raw_complete",
    "canonical_ready",
    "canonical_incomplete",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
    "cancelled",
)
_CYCLE_TERMINAL_UI = frozenset(
    {
        "complete",
        "published",
        "parsed_partial",
        "failed_download",
        "failed_convert",
        "failed_forcing",
        "failed_run",
        "failed_parse",
        "failed_publish",
        "cancelled",
    }
)
_CYCLE_TERMINAL_UPSERT = ("published", "cancelled")
_CYCLE_UPSERT_PRESERVED = (
    "raw_complete",
    "canonical_ready",
    "canonical_incomplete",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "cancelled",
)
_SLURM_RAW = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "COMPLETING",
        "CONFIGURING",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PENDING",
        "PREEMPTED",
        "REQUEUED",
        "REVOKED",
        "RUNNING",
        "SPECIAL_EXIT",
        "SUSPENDED",
        "TIMEOUT",
    }
)
_SLURM_TERMINAL = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "SPECIAL_EXIT",
        "TIMEOUT",
    }
)
_SLURM_SUCCESS = frozenset({"COMPLETED"})
_SLURM_FAILURE = frozenset(
    {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
)
_QC_VERDICTS = frozenset({"passed", "failed", "blocked"})
_READINESS_ITEM = frozenset({"passed", "failed", "blocked", "not_executed", "release_blocked"})

# (vocabulary name, pinned literal): the vocabulary object must equal the literal
# and carry the immutable type of the literal (`frozenset` or ordered `tuple`).
_VOCABULARY: tuple[tuple[str, frozenset[str] | tuple[str, ...]], ...] = (
    ("PIPELINE_JOB_STATUS_ORDER", _PIPELINE_JOB_ORDER),
    ("PIPELINE_JOB_STATUSES", frozenset(_PIPELINE_JOB_ORDER)),
    ("PIPELINE_JOB_INTERNAL_STATUSES", _INTERNAL),
    ("PIPELINE_JOB_TERMINAL", _TERMINAL),
    ("PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION", _TERMINAL_WITHOUT_RESERVATION),
    ("PIPELINE_JOB_TERMINAL_GUARD", _TERMINAL_GUARD),
    ("PIPELINE_JOB_TERMINAL_OVERRIDES", _TERMINAL_OVERRIDES),
    ("PIPELINE_JOB_ACTIVE_MANUAL_RETRY", _ACTIVE_MANUAL_RETRY),
    ("PIPELINE_JOB_SUCCESS", _SUCCESS),
    ("PIPELINE_JOB_ACTIVE", _ACTIVE),
    ("PIPELINE_JOB_PENDING", _PENDING),
    ("PIPELINE_JOB_FAILED_UI", _FAILED_UI),
    ("FAILED_PIPELINE_STATUSES", _FAILED_PIPELINE),
    ("ACTIVE_PIPELINE_STATUSES", _ACTIVE),
    ("RESERVATION_ACTIVE_STATUSES", _RESERVATION_ACTIVE),
    ("HYDRO_RUN_STATUS_ORDER", _HYDRO_RUN_ORDER),
    ("HYDRO_RUN_STATUSES", frozenset(_HYDRO_RUN_ORDER)),
    ("ACTIVE_HYDRO_STATUSES", _ACTIVE_HYDRO),
    ("DURABLE_HYDRO_SUCCESS_STATUSES", _DURABLE_HYDRO_SUCCESS),
    ("HYDRO_RUN_CODE_CLEARING_STATUSES", _CODE_CLEARING),
    ("HYDRO_RUN_TERMINAL_UI", _HYDRO_TERMINAL_UI),
    ("DISPLAY_PRODUCT_READY_STATUSES", _DISPLAY_READY),
    ("QHH_LATEST_READY_RUN_STATUSES", _QHH_LATEST_READY),
    ("PARSE_READY_RUN_STATUSES", _PARSE_READY),
    ("FAILABLE_RUN_STATUSES", _FAILABLE),
    ("FORECAST_CYCLE_SUCCESS_STATUSES", _FORECAST_CYCLE_SUCCESS),
    ("CYCLE_STATUS_ORDER", _CYCLE_ORDER),
    ("CYCLE_STATUSES", frozenset(_CYCLE_ORDER)),
    ("CYCLE_TERMINAL_UI", _CYCLE_TERMINAL_UI),
    ("CYCLE_TERMINAL_UPSERT", _CYCLE_TERMINAL_UPSERT),
    ("CYCLE_UPSERT_PRESERVED", _CYCLE_UPSERT_PRESERVED),
    ("SLURM_RAW_STATES", _SLURM_RAW),
    ("SLURM_TERMINAL_STATES", _SLURM_TERMINAL),
    ("SLURM_TERMINAL_SUCCESS", _SLURM_SUCCESS),
    ("SLURM_TERMINAL_FAILURE", _SLURM_FAILURE),
    ("QC_VERDICTS", _QC_VERDICTS),
    ("READINESS_ITEM_STATUSES", _READINESS_ITEM),
)

# (vocabulary name, site module, site attribute, site type at f7be77f22, pinned
# literal). Sites are untouched by 2.1a except the four `scheduler_state_types`
# rebinds, whose type is unchanged. Rows beyond the first per name are the
# independent copies that 2.3/2.4 rebind.
_CANONICAL_SITES: tuple[tuple[str, ModuleType, str, type, frozenset[str] | tuple[str, ...]], ...] = (
    ("PIPELINE_JOB_TERMINAL", chain_types, "TERMINAL_JOB_STATUSES", set, _TERMINAL),
    ("PIPELINE_JOB_TERMINAL", chain_runtime_utils, "TERMINAL_JOB_STATUSES", set, _TERMINAL),
    ("PIPELINE_JOB_TERMINAL", file_orchestration_journal, "TERMINAL_PIPELINE_STATUSES", set, _TERMINAL),
    (
        "PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION",
        scheduler,
        "TERMINAL_PIPELINE_STATUSES",
        set,
        _TERMINAL_WITHOUT_RESERVATION,
    ),
    (
        "PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION",
        scheduler_candidates,
        "CANDIDATE_CONSTRUCTION_TERMINAL_PIPELINE_STATUSES",
        set,
        _TERMINAL_WITHOUT_RESERVATION,
    ),
    ("PIPELINE_JOB_TERMINAL_GUARD", persistence, "TERMINAL_STATUS_GUARD", set, _TERMINAL_GUARD),
    ("PIPELINE_JOB_TERMINAL_OVERRIDES", persistence, "TERMINAL_STATUS_OVERRIDES", set, _TERMINAL_OVERRIDES),
    ("PIPELINE_JOB_ACTIVE_MANUAL_RETRY", persistence, "ACTIVE_MANUAL_RETRY_STATUSES", tuple, _ACTIVE_MANUAL_RETRY),
    ("PIPELINE_JOB_SUCCESS", scheduler_state_types, "TERMINAL_PIPELINE_SUCCESS_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_SUCCESS", chain, "TERMINAL_PIPELINE_SUCCESS_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_SUCCESS", chain_runtime_utils, "TERMINAL_PIPELINE_SUCCESS_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_SUCCESS", chain_source_cycle, "TERMINAL_PIPELINE_SUCCESS_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_SUCCESS", chain_repository_state, "TERMINAL_PIPELINE_SUCCESS_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_SUCCESS", retry, "TERMINAL_SUCCESS_RETRY_STATUSES", set, _SUCCESS),
    ("PIPELINE_JOB_ACTIVE", pipeline_route, "_ACTIVE_JOB_STATUSES", set, _ACTIVE),
    ("PIPELINE_JOB_PENDING", pipeline_route, "_PENDING_JOB_STATUSES", set, _PENDING),
    ("PIPELINE_JOB_FAILED_UI", pipeline_route, "_FAILED_JOB_STATUSES", set, _FAILED_UI),
    ("FAILED_PIPELINE_STATUSES", scheduler_state_types, "FAILED_PIPELINE_STATUSES", set, _FAILED_PIPELINE),
    ("FAILED_PIPELINE_STATUSES", chain, "FAILED_PIPELINE_STATUSES", set, _FAILED_PIPELINE),
    ("FAILED_PIPELINE_STATUSES", chain_repository_state, "FAILED_PIPELINE_STATUSES", set, _FAILED_PIPELINE),
    ("FAILED_PIPELINE_STATUSES", chain_source_cycle, "FAILED_PIPELINE_STATUSES", set, _FAILED_PIPELINE),
    ("FAILED_PIPELINE_STATUSES", retry, "FAILED_RETRY_STATUSES", set, _FAILED_PIPELINE),
    ("ACTIVE_PIPELINE_STATUSES", scheduler_state_types, "ACTIVE_PIPELINE_STATUSES", set, _ACTIVE),
    ("ACTIVE_PIPELINE_STATUSES", retry, "ACTIVE_RETRY_STATUSES", set, _ACTIVE),
    ("RESERVATION_ACTIVE_STATUSES", persistence, "RESERVATION_ACTIVE_STATUSES", tuple, _RESERVATION_ACTIVE),
    ("ACTIVE_HYDRO_STATUSES", scheduler_state_types, "ACTIVE_HYDRO_STATUSES", set, _ACTIVE_HYDRO),
    (
        "DURABLE_HYDRO_SUCCESS_STATUSES",
        scheduler_state_types,
        "DURABLE_HYDRO_SUCCESS_STATUSES",
        set,
        _DURABLE_HYDRO_SUCCESS,
    ),
    (
        "HYDRO_RUN_CODE_CLEARING_STATUSES",
        scheduler_state_types,
        "HYDRO_RUN_CODE_CLEARING_STATUSES",
        frozenset,
        _CODE_CLEARING,
    ),
    ("HYDRO_RUN_TERMINAL_UI", pipeline_route, "_TERMINAL_HYDRO_STATUSES", set, _HYDRO_TERMINAL_UI),
    ("DISPLAY_PRODUCT_READY_STATUSES", hydro_display_constants, "DISPLAY_PRODUCT_READY_STATUSES", set, _DISPLAY_READY),
    ("QHH_LATEST_READY_RUN_STATUSES", forecast_store, "QHH_LATEST_READY_RUN_STATUSES", tuple, _QHH_LATEST_READY),
    ("PARSE_READY_RUN_STATUSES", parser, "PARSE_READY_RUN_STATUSES", tuple, _PARSE_READY),
    ("FAILABLE_RUN_STATUSES", parser, "FAILABLE_RUN_STATUSES", tuple, _FAILABLE),
    (
        "FORECAST_CYCLE_SUCCESS_STATUSES",
        file_orchestration_journal,
        "_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES",
        set,
        _FORECAST_CYCLE_SUCCESS,
    ),
    ("CYCLE_TERMINAL_UI", pipeline_route, "_TERMINAL_CYCLE_STATUSES", set, _CYCLE_TERMINAL_UI),
    ("CYCLE_TERMINAL_UPSERT", met_store, "_TERMINAL_CYCLE_STATUSES", tuple, _CYCLE_TERMINAL_UPSERT),
    ("CYCLE_UPSERT_PRESERVED", met_store, "_UPSERT_PRESERVED_CYCLE_STATUSES", tuple, _CYCLE_UPSERT_PRESERVED),
    ("SLURM_TERMINAL_STATES", slurm_validation, "TERMINAL_SLURM_STATES", set, _SLURM_TERMINAL),
    ("SLURM_TERMINAL_SUCCESS", run_qhh_continuous, "SLURM_TERMINAL_SUCCESS", set, _SLURM_SUCCESS),
    ("SLURM_TERMINAL_FAILURE", run_qhh_continuous, "SLURM_TERMINAL_FAILURE", set, _SLURM_FAILURE),
    ("READINESS_ITEM_STATUSES", readiness_item_contracts, "STATUS_VALUES", frozenset, _READINESS_ITEM),
)


def _site_id(row: tuple[str, ModuleType, str, type, frozenset[str] | tuple[str, ...]]) -> str:
    return f"{row[0]}<-{row[1].__name__.rsplit('.', 1)[-1]}.{row[2]}"


def _schema_status_enum(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["properties"]["status"]["enum"]


def _assert_public_enum_matches(order: tuple[str, ...], schema_enum: list[str], pending_delta: tuple[str, ...]) -> None:
    """The ordered vocabulary minus the pending public delta equals the schema enum, in order.

    Pure: the real tree and the synthetic drift cases go through the same check,
    and a mismatch names the values one side has and the other lacks.
    """

    expected = tuple(status for status in order if status not in pending_delta)
    missing = sorted(set(expected) - set(schema_enum))
    unexpected = sorted(set(schema_enum) - set(expected))
    assert not missing, f"schema enum is missing {missing}"
    assert not unexpected, f"schema enum has values outside the vocabulary: {unexpected}"
    assert expected == tuple(schema_enum), f"schema enum order {schema_enum} != vocabulary order {list(expected)}"


@pytest.mark.parametrize(("name", "pinned"), _VOCABULARY, ids=[row[0] for row in _VOCABULARY])
def test_vocabulary_set_equals_its_pinned_literal_and_is_immutable(
    name: str, pinned: frozenset[str] | tuple[str, ...]
) -> None:
    value = getattr(vocab, name)
    assert type(value) is type(pinned), f"{name} is {type(value).__name__}, expected {type(pinned).__name__}"
    assert value == pinned
    if isinstance(value, tuple):
        assert len(set(value)) == len(value), f"{name} repeats a member"


@pytest.mark.parametrize("row", _CANONICAL_SITES, ids=[_site_id(row) for row in _CANONICAL_SITES])
def test_canonical_site_keeps_its_members_and_type(
    row: tuple[str, ModuleType, str, type, frozenset[str] | tuple[str, ...]],
) -> None:
    name, module, attribute, site_type, pinned = row
    site = getattr(module, attribute)
    assert type(site) is site_type, f"{module.__name__}.{attribute} is {type(site).__name__}, was {site_type.__name__}"
    if site_type is tuple:
        assert site == pinned
    else:
        assert site == set(pinned)
    vocabulary_value = getattr(vocab, name)
    if isinstance(vocabulary_value, tuple):
        assert vocabulary_value == site
    else:
        assert vocabulary_value == set(site)


def test_every_vocabulary_name_is_pinned_once() -> None:
    pinned_names = [name for name, _ in _VOCABULARY]
    assert len(pinned_names) == len(set(pinned_names))
    public_upper = {
        name
        for name, value in vars(vocab).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, (frozenset, tuple))
    }
    assert public_upper == set(pinned_names)
    assert {row[0] for row in _CANONICAL_SITES} <= public_upper


def test_scheduler_state_types_rebinds_the_vocabulary() -> None:
    """The four 2.1a rebinds: three fresh mutable `set` copies and one shared frozenset."""

    for site_name, vocab_name in (
        ("DURABLE_HYDRO_SUCCESS_STATUSES", "DURABLE_HYDRO_SUCCESS_STATUSES"),
        ("TERMINAL_PIPELINE_SUCCESS_STATUSES", "PIPELINE_JOB_SUCCESS"),
        ("FAILED_PIPELINE_STATUSES", "FAILED_PIPELINE_STATUSES"),
    ):
        site = getattr(scheduler_state_types, site_name)
        assert type(site) is set, site_name
        assert site is not getattr(vocab, vocab_name), site_name
        assert site == getattr(vocab, vocab_name), site_name
    assert scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES is vocab.HYDRO_RUN_CODE_CLEARING_STATUSES


def test_terminal_with_and_without_reservation_lost() -> None:
    assert "reservation_lost" in vocab.PIPELINE_JOB_TERMINAL
    assert "reservation_lost" not in vocab.PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION
    assert vocab.PIPELINE_JOB_TERMINAL - vocab.PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION == {"reservation_lost"}


def test_internal_statuses_complete_the_accepted_submit_master_set() -> None:
    site = accepted_submit_identity.ACCEPTED_SUBMIT_MASTER_STATUSES
    assert type(site) is frozenset
    assert site == _ACCEPTED_SUBMIT_MASTER
    assert site == (vocab.PIPELINE_JOB_STATUSES - {"skipped"}) | vocab.PIPELINE_JOB_INTERNAL_STATUSES
    assert not vocab.PIPELINE_JOB_INTERNAL_STATUSES & vocab.PIPELINE_JOB_STATUSES
    assert not vocab.PIPELINE_JOB_INTERNAL_STATUSES & set(_schema_status_enum(_PIPELINE_JOB_SCHEMA))


def test_pipeline_job_order_matches_schema_and_api_tuple_modulo_pending_delta() -> None:
    assert _PENDING_PUBLIC_DELTA == ("reservation_lost",)
    _assert_public_enum_matches(
        vocab.PIPELINE_JOB_STATUS_ORDER, _schema_status_enum(_PIPELINE_JOB_SCHEMA), _PENDING_PUBLIC_DELTA
    )
    site = pipeline_route.PIPELINE_JOB_STATUS_VALUES
    assert type(site) is tuple
    assert site == _PIPELINE_API_TUPLE
    _assert_public_enum_matches(vocab.PIPELINE_JOB_STATUS_ORDER, list(site), _PENDING_PUBLIC_DELTA)
    # `reservation_lost` sits right after `submission_failed`.
    order = vocab.PIPELINE_JOB_STATUS_ORDER
    assert order.index("reservation_lost") == order.index("submission_failed") + 1


def test_public_enum_check_names_a_value_missing_from_the_schema() -> None:
    """Synthetic drift: a value added to the order but not to the schema reds, naming it.

    The delta is passed explicitly (empty here), so these cases outlive the
    deletion of `_PENDING_PUBLIC_DELTA` in task 2.2.
    """

    schema_enum = ["pending", "running", "succeeded"]
    _assert_public_enum_matches(("pending", "running", "succeeded"), schema_enum, ())

    with pytest.raises(AssertionError, match="missing \\['drift_probe'\\]"):
        _assert_public_enum_matches(("pending", "running", "succeeded", "drift_probe"), schema_enum, ())
    with pytest.raises(AssertionError, match="outside the vocabulary: \\['drift_probe'\\]"):
        _assert_public_enum_matches(("pending", "running", "succeeded"), [*schema_enum, "drift_probe"], ())
    with pytest.raises(AssertionError, match="order"):
        _assert_public_enum_matches(("running", "pending", "succeeded"), schema_enum, ())
    # A delta value is excused only while it is declared pending.
    _assert_public_enum_matches(("pending", "running", "succeeded", "drift_probe"), schema_enum, ("drift_probe",))


def test_hydro_run_order_matches_schema_and_covers_the_folded_db_enum() -> None:
    assert vocab.HYDRO_RUN_STATUS_ORDER == tuple(_schema_status_enum(_RUN_STATUS_SCHEMA))
    folded = _fold_enum_members("hydro", "run_status", create_sentinel="succeeded", add_sentinel="pending")
    assert len(folded) == 12
    assert "frequency_done" in folded
    assert folded - {"frequency_done"} <= vocab.HYDRO_RUN_STATUSES
    assert "frequency_done" not in vocab.HYDRO_RUN_STATUSES


def test_cycle_order_equals_the_folded_db_enum_in_ordinal_order() -> None:
    folded = _fold_enum_members("met", "cycle_status", create_sentinel="discovered", add_sentinel="cancelled")
    assert {"cancelled", "canonical_incomplete"} <= folded
    assert len(folded) == 18
    assert vocab.CYCLE_STATUSES == folded

    order = vocab.CYCLE_STATUS_ORDER
    assert tuple(status for status in order if status not in _CYCLE_ADDED_AFTER) == _CYCLE_CREATE_ORDER
    for added, after in _CYCLE_ADDED_AFTER.items():
        assert order.index(added) == order.index(after) + 1, f"{added} must follow {after}"
    assert vocab.CYCLE_TERMINAL_UI <= vocab.CYCLE_STATUSES
    assert set(vocab.CYCLE_UPSERT_PRESERVED) <= vocab.CYCLE_STATUSES
    assert vocab.CYCLE_UPSERT_PRESERVED[-len(vocab.CYCLE_TERMINAL_UPSERT) :] == vocab.CYCLE_TERMINAL_UPSERT


@pytest.mark.parametrize(
    ("alias", "order"),
    [
        pytest.param(vocab.PipelineJobStatus, vocab.PIPELINE_JOB_STATUS_ORDER, id="PipelineJobStatus"),
        pytest.param(vocab.HydroRunStatus, vocab.HYDRO_RUN_STATUS_ORDER, id="HydroRunStatus"),
        pytest.param(vocab.CycleStatus, vocab.CYCLE_STATUS_ORDER, id="CycleStatus"),
    ],
)
def test_literal_alias_spells_the_ordered_tuple(alias: object, order: tuple[str, ...]) -> None:
    assert typing.get_args(alias) == order


def test_slurm_raw_states_are_the_gateway_state_map_keys() -> None:
    state_map = real_backend.SLURM_STATE_MAP
    assert type(state_map) is dict
    assert len(state_map) == 17
    assert vocab.SLURM_RAW_STATES == set(state_map)
    assert vocab.SLURM_TERMINAL_STATES | vocab.SLURM_TERMINAL_SUCCESS | vocab.SLURM_TERMINAL_FAILURE <= (
        vocab.SLURM_RAW_STATES
    )


@pytest.mark.parametrize(
    ("spelling", "verdict"),
    [
        ("pass", "passed"),
        ("PASS", "passed"),
        ("passed", "passed"),
        ("PASSED", "passed"),
        ("fail", "failed"),
        ("FAIL", "failed"),
        ("failed", "failed"),
        ("FAILED", "failed"),
        ("blocked", "blocked"),
        ("BLOCKED", "blocked"),
    ],
)
def test_normalize_qc_verdict_maps_known_spellings(spelling: str, verdict: str) -> None:
    assert vocab.normalize_qc_verdict(spelling) == verdict
    assert verdict in vocab.QC_VERDICTS


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(1, id="non-str"),
        pytest.param(b"PASS", id="bytes"),
        pytest.param("Pass", id="mixed-case"),
        pytest.param(" PASS", id="padded"),
        pytest.param("not_executed", id="readiness-only"),
    ],
)
def test_normalize_qc_verdict_rejects_everything_else(value: object) -> None:
    with pytest.raises(ValueError):
        vocab.normalize_qc_verdict(value)


def test_status_vocab_imports_only_the_standard_library() -> None:
    tree = ast.parse(_STATUS_VOCAB_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module.split(".", 1)[0])
    assert imported - {"__future__"} <= sys.stdlib_module_names, sorted(imported)


def test_status_vocab_import_loads_no_runtime_modules() -> None:
    """Fresh interpreter: importing the vocabulary pulls in no services/apps/workers/scripts/httpx."""

    probe = (
        "import sys\n"
        "import packages.common.status_vocab\n"
        "roots = ('services', 'apps', 'workers', 'scripts', 'httpx')\n"
        "leaked = sorted(m for m in sys.modules if m.split('.', 1)[0] in roots)\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


# --- Duplicate status literal scan (entropy-burndown task 2.1b) ----------------------
#
# Scan rule. Every `ast.Set` and `ast.Tuple` node anywhere in a module (`ast.walk`;
# `ast.List` is not scanned, and `frozenset({...})` / `frozenset((...))` count once,
# through their inner literal). A literal's members are its `str` constant elements,
# case-sensitive; starred elements and every other non-`str` element are ignored and
# the constants are still compared (so `{*BASE, "succeeded", "failed"}` is scanned as
# `{"failed", "succeeded"}`). The literal is a hit when, for some vocabulary set V,
# it shares at least two members with V and equals, is a subset of, or is a superset
# of V; or when it contains all of `succeeded`, `complete` and `published`. The
# vocabulary is every public `frozenset`/`tuple` of `str` in `status_vocab`.
#
# Scanned files: git-tracked `*.py` under the five roots below, minus any path with
# a `tests` component and minus `packages/common/status_vocab.py` itself. A hit is
# keyed by `(file, sorted members)`, so a line move never stales an entry.

_STATUS_LITERAL_ALLOWLIST = "tests/status_literal_allowlist.json"
_STATUS_LITERAL_SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")
_STATUS_LITERAL_SCAN_EXCLUDED_FILES = frozenset({"packages/common/status_vocab.py"})
_STATUS_LITERAL_REASONS = frozenset({"pending-migration", "distinct-vocabulary", "not-a-status-set"})
_STATUS_LITERAL_ENTRY_KEYS = frozenset({"file", "members", "reason"})
_SUCCESS_TRIPLE = frozenset({"succeeded", "complete", "published"})


def _scanned_vocabulary() -> dict[str, frozenset[str]]:
    return {
        name: frozenset(value)
        for name, value in vars(vocab).items()
        if not name.startswith("_")
        and isinstance(value, (frozenset, tuple))
        and value
        and all(isinstance(member, str) for member in value)
    }


def _status_literal_matches(members: frozenset[str], vocabulary: dict[str, frozenset[str]]) -> list[str]:
    """The vocabulary names (or the success-triple marker) that make ``members`` a hit."""

    reasons = sorted(
        name
        for name, value in vocabulary.items()
        if len(members & value) >= 2 and (members <= value or members >= value)
    )
    if _SUCCESS_TRIPLE <= members:
        reasons.append("{succeeded, complete, published}")
    return reasons


def _literal_str_members(node: ast.Set | ast.Tuple) -> frozenset[str]:
    return frozenset(
        element.value for element in node.elts if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )


def _is_scanned_status_file(path: str) -> bool:
    parts = path.split("/")
    return (
        path.endswith(".py")
        and parts[0] in _STATUS_LITERAL_SCAN_ROOTS
        and "tests" not in parts
        and path not in _STATUS_LITERAL_SCAN_EXCLUDED_FILES
    )


def _tracked_status_scan_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *_STATUS_LITERAL_SCAN_ROOTS],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return sorted(path for path in result.stdout.decode("utf-8").split("\0") if path and _is_scanned_status_file(path))


def scan_status_literals(repo_root: Path, files: list[str]) -> list[tuple[str, int, tuple[str, ...], list[str]]]:
    """``(file, line, sorted members, matched vocabulary names)`` for every hit, in file/line order."""

    vocabulary = _scanned_vocabulary()
    hits: list[tuple[str, int, tuple[str, ...], list[str]]] = []
    for file in files:
        tree = ast.parse((repo_root / file).read_text(encoding="utf-8"), filename=file)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.Tuple)):
                continue
            members = _literal_str_members(node)
            matched = _status_literal_matches(members, vocabulary)
            if matched:
                hits.append((file, node.lineno, tuple(sorted(members)), matched))
    return sorted(hits)


def compare_status_literals(repo_root: Path, files: list[str], allowlist: object) -> list[str]:
    """Every problem of the scanned ``files`` against ``allowlist``; empty means clean.

    Both directions: a hit the allowlist does not name (``file:line``), and an
    allowlist entry that is malformed, duplicated, out of order, names a file that
    is not scanned, or names members no hit in that file has (stale; reported as
    ``tests/status_literal_allowlist.json[<index>]`` plus the file, since a stale
    entry has no source line).
    """

    if not isinstance(allowlist, list):
        return [f"{_STATUS_LITERAL_ALLOWLIST}: top level must be a list, got {type(allowlist).__name__}"]
    hits = scan_status_literals(repo_root, files)
    hit_keys = {(file, members) for file, _, members, _ in hits}
    scanned = set(files)
    problems: list[str] = []
    keys: list[tuple[str, tuple[str, ...]]] = []
    for index, entry in enumerate(allowlist):
        label = f"{_STATUS_LITERAL_ALLOWLIST}[{index}] {json.dumps(entry, sort_keys=True)}"
        if not isinstance(entry, dict) or set(entry) != _STATUS_LITERAL_ENTRY_KEYS:
            got = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
            problems.append(f"{label}: an entry must have exactly the keys ['file', 'members', 'reason'], got {got}")
            continue
        file, members, reason = entry["file"], entry["members"], entry["reason"]
        if not isinstance(file, str) or not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            problems.append(f"{label}: 'file' must be a string and 'members' a list of strings")
            continue
        if reason not in _STATUS_LITERAL_REASONS:
            problems.append(f"{label}: reason must be one of {sorted(_STATUS_LITERAL_REASONS)}")
        if members != sorted(set(members)):
            problems.append(f"{label}: 'members' must be sorted and free of repeats")
        key = (file, tuple(members))
        keys.append(key)
        if file not in scanned:
            problems.append(f"{label}: stale entry -- {file} is not a scanned Python file")
        elif key not in hit_keys:
            problems.append(f"{label}: stale entry -- no status literal in {file} has the members {members}")
    for key, count in sorted(Counter(keys).items()):
        if count > 1:
            problems.append(f"{_STATUS_LITERAL_ALLOWLIST}: duplicate entry file={key[0]} members={list(key[1])}")
    for previous, current in zip(keys, keys[1:], strict=False):
        if current < previous:
            problems.append(
                f"{_STATUS_LITERAL_ALLOWLIST}: entries are not sorted by (file, members): "
                f"{current[0]} {list(current[1])} follows {previous[0]} {list(previous[1])}"
            )
            break
    allowed = set(keys)
    for file, line, members, matched in hits:
        if (file, members) not in allowed:
            problems.append(
                f"{file}:{line}: status literal {list(members)} matches {matched} "
                f"and is not in {_STATUS_LITERAL_ALLOWLIST}"
            )
    return problems


def test_repository_status_literals_match_the_allowlist() -> None:
    allowlist = json.loads((_REPO_ROOT / _STATUS_LITERAL_ALLOWLIST).read_text(encoding="utf-8"))
    problems = compare_status_literals(_REPO_ROOT, _tracked_status_scan_files(_REPO_ROOT), allowlist)
    assert not problems, "\n".join(problems)


def test_status_literal_scan_covers_its_roots_and_skips_tests_and_the_vocabulary() -> None:
    files = _tracked_status_scan_files(_REPO_ROOT)
    assert "services/orchestrator/chain_types.py" in files
    assert "apps/api/routes/pipeline.py" in files
    assert "packages/common/status_vocab.py" not in files
    assert not [path for path in files if "tests" in path.split("/")]
    assert {path.split("/", 1)[0] for path in files} == set(_STATUS_LITERAL_SCAN_ROOTS)


def _write_sources(root: Path, sources: dict[str, str]) -> list[str]:
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(sources)


def _status_entry(file: str, members: list[str], reason: str = "distinct-vocabulary") -> dict[str, object]:
    return {"file": file, "members": members, "reason": reason}


def test_status_literal_scan_names_an_unlisted_copy_by_file_and_line(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {"services/x/mod.py": 'import os\n\nTERMINAL = frozenset({"succeeded", "failed", "cancelled"})\n'},
    )
    problems = compare_status_literals(tmp_path, files, [])
    assert len(problems) == 1, problems
    assert problems[0].startswith("services/x/mod.py:3: status literal ['cancelled', 'failed', 'succeeded'] matches ")
    assert "PIPELINE_JOB_TERMINAL" in problems[0]
    entry = _status_entry("services/x/mod.py", ["cancelled", "failed", "succeeded"])
    assert compare_status_literals(tmp_path, files, [entry]) == []


def test_status_literal_scan_rules(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {
            "services/x/rules.py": (
                'AS_LIST = ["succeeded", "failed", "cancelled"]\n'  # List: not scanned
                'ONCE = frozenset({"pending", "queued"})\n'  # one hit, not two
                'UPPER = ("PASS", "BLOCKED")\n'  # case-sensitive: no vocabulary member
                'MIXED = {"Succeeded", "FAILED", "cancelled"}\n'  # one lowercase member only
                'STARRED = {*BASE, "submitted", "running"}\n'  # starred ignored, constants compared
                'SUCCESS = ("succeeded", "complete", "published", "other")\n'  # success triple
                'SINGLE = {"COMPLETED"}\n'  # one member in common: never a hit
                'UNRELATED = ("alpha", "pending")\n'  # one member in common: not a hit
                'MAPPING = {"succeeded": 1, "failed": 2}\n'  # dict keys: not a Set/Tuple
            )
        },
    )
    assert [(line, members) for _, line, members, _ in scan_status_literals(tmp_path, files)] == [
        (2, ("pending", "queued")),
        (5, ("running", "submitted")),
        (6, ("complete", "other", "published", "succeeded")),
    ]


def test_status_literal_scan_superset_and_subset_both_hit(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {
            "workers/x/sets.py": (
                'SUPER = {"pending", "queued", "unrelated"}\n'
                'SUB = ("parsed", "published")\n'
                'OVERLAP = {"pending", "parsed", "unrelated"}\n'  # neither subset nor superset of any set
            )
        },
    )
    hits = scan_status_literals(tmp_path, files)
    assert [(line, members) for _, line, members, _ in hits] == [
        (1, ("pending", "queued", "unrelated")),
        (2, ("parsed", "published")),
    ]
    assert "PIPELINE_JOB_PENDING" in hits[0][3]
    assert "DISPLAY_PRODUCT_READY_STATUSES" in hits[1][3]


def test_status_literal_allowlist_stale_entries_fail(tmp_path: Path) -> None:
    files = _write_sources(tmp_path, {"apps/x/mod.py": 'S = {"pending", "queued"}\n'})
    problems = compare_status_literals(
        tmp_path,
        files,
        [
            _status_entry("apps/x/gone.py", ["pending", "queued"]),
            _status_entry("apps/x/mod.py", ["pending", "queued"]),
            _status_entry("apps/x/mod.py", ["pending", "queued", "running"]),
        ],
    )
    assert problems == [
        f'{_STATUS_LITERAL_ALLOWLIST}[0] {{"file": "apps/x/gone.py", "members": ["pending", "queued"], '
        '"reason": "distinct-vocabulary"}: stale entry -- apps/x/gone.py is not a scanned Python file',
        f'{_STATUS_LITERAL_ALLOWLIST}[2] {{"file": "apps/x/mod.py", "members": ["pending", "queued", "running"], '
        '"reason": "distinct-vocabulary"}: stale entry -- no status literal in apps/x/mod.py has the members '
        "['pending', 'queued', 'running']",
    ]


@pytest.mark.parametrize(
    ("allowlist", "expected"),
    [
        pytest.param({"file": "x"}, "top level must be a list, got dict", id="not-a-list"),
        pytest.param(
            [{"file": "apps/x/mod.py", "members": ["pending", "queued"], "reason": "distinct-vocabulary", "line": 1}],
            "got ['file', 'line', 'members', 'reason']",
            id="extra-key",
        ),
        pytest.param(
            [{"file": "apps/x/mod.py", "members": ["pending", "queued"]}],
            "an entry must have exactly the keys ['file', 'members', 'reason'], got ['file', 'members']",
            id="missing-key",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", ["pending", "queued"], reason="migrate-later")],
            "reason must be one of ['distinct-vocabulary', 'not-a-status-set', 'pending-migration']",
            id="bad-reason",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", ["queued", "pending"])],
            "'members' must be sorted and free of repeats",
            id="unsorted-members",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", "pending,queued")],  # type: ignore[arg-type]
            "'members' a list of strings",
            id="members-not-a-list",
        ),
        pytest.param(
            [
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
            ],
            "duplicate entry file=apps/x/mod.py members=['pending', 'queued']",
            id="duplicate",
        ),
        pytest.param(
            [
                _status_entry("apps/x/mod.py", ["running", "submitted"]),
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
            ],
            "entries are not sorted by (file, members): apps/x/mod.py ['pending', 'queued'] follows "
            "apps/x/mod.py ['running', 'submitted']",
            id="unsorted-list",
        ),
    ],
)
def test_malformed_status_literal_allowlist_fails(tmp_path: Path, allowlist: object, expected: str) -> None:
    files = _write_sources(tmp_path, {"apps/x/mod.py": 'A = {"pending", "queued"}\nB = {"submitted", "running"}\n'})
    problems = compare_status_literals(tmp_path, files, allowlist)
    assert any(expected in problem for problem in problems), problems


def test_well_formed_status_literal_allowlist_is_clean(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {"apps/x/mod.py": 'A = {"pending", "queued"}\nB = {"submitted", "running"}\nC = ("queued", "pending")\n'},
    )
    allowlist = [
        _status_entry("apps/x/mod.py", ["pending", "queued"], reason="pending-migration"),
        _status_entry("apps/x/mod.py", ["running", "submitted"], reason="not-a-status-set"),
    ]
    assert compare_status_literals(tmp_path, files, allowlist) == []
