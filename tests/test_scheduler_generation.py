"""Tests for the scheduler-side generation-aware cutover consumer (#1081, §8).

These tests cover the 8-value ``transition_decision`` enum end-to-end:

- Admits: ``warm_continue``, ``cold_new_model``, ``cold_declared_cutover``.
- Blocks: ``block_predecessor_pending``, ``block_declaration_missing``,
  ``block_declaration_stale``, ``block_cold_start_out_of_window``,
  ``block_wrong_generation``.

Every block-side test asserts the single mapped typed-reason so the D8.8
1:1 mapping cannot silently drift.  Tests are unit-level against the pure
decision engine + declaration loader so they do not require a full DB-free
scheduler pass or Slurm oracle harness; §8.10 pytest evidence is satisfied by
running the whole test file plus the existing DB-free tests together.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.orchestrator import scheduler_generation as generation
from services.orchestrator import scheduler_generation_gate as gate
from services.orchestrator.scheduler_generation import MAX_CUTOVER_DECLARATION_BYTES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _hex(byte: str) -> str:
    """Return a valid 64-hex string from a short label so tests read cleanly."""
    return (byte * 64)[:64]


NEW_CHECKSUM = _hex("b")
OLD_CHECKSUM = _hex("a")
NEW_GENERATION = generation.derive_generation(NEW_CHECKSUM)
# Stable reference time so declaration ``effective_cycle_utc`` values stay
# inside the publisher's 24h-past / 168h-future tolerance window regardless
# of when the test suite is run (declarations use 2026-07-06 fixture dates).
NOW = _dt("2026-07-06T18:00:00Z")


def _write_declaration(
    tmp_path: Path,
    *,
    model_id: str = "model_a",
    old_checksum: str = OLD_CHECKSUM,
    new_checksum: str = NEW_CHECKSUM,
    effective_cycle_utc: str = "2026-07-06T12:00:00Z",
    transition_mode: str = "replace",
    generation_field: str | None = None,
    filename: str = "cutover.json",
    extra_entries: list[dict[str, Any]] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
        "generated_at": "2026-07-06T00:00:00Z",
        "generation": generation_field or generation.derive_generation(new_checksum),
        "entries": [
            {
                "model_id": model_id,
                "old_checksum": old_checksum,
                "new_checksum": new_checksum,
                "effective_cycle_utc": effective_cycle_utc,
                "transition_mode": transition_mode,
            }
        ]
        + (extra_entries or []),
    }
    path = tmp_path / filename
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test helper: signal builder
# ---------------------------------------------------------------------------


def _signal(
    *,
    exists_any: bool,
    exists_current: bool,
    has_exact_predecessor: bool = False,
    predecessor_cycle_id: str = "gfs_2026070600",
    predecessor_lead_hours: int = 12,
    predecessor_valid_time: str = "2026-07-06T00:00:00Z",
    latest_any_checksum: str | None = None,
    wrong_generation_predecessor_present: bool = False,
    wrong_generation_predecessor_checksum: str = "",
) -> generation._HistorySignal:
    current_summary: dict[str, Any] | None = None
    if exists_current:
        current_summary = {
            "has_exact_predecessor": has_exact_predecessor,
            "predecessor_cycle_id": predecessor_cycle_id,
            "predecessor_valid_time": predecessor_valid_time,
            "predecessor_lead_hours": predecessor_lead_hours,
        }
    any_summary: dict[str, Any] | None = None
    if exists_any:
        any_summary = {
            "state_id": "state_old",
            "model_package_checksum": latest_any_checksum or OLD_CHECKSUM,
            "valid_time": "2026-07-05T12:00:00Z",
        }
    return generation._HistorySignal(
        exists_current_generation=exists_current,
        exists_any_generation=exists_any,
        latest_current_generation_checkpoint=current_summary,
        latest_any_generation_checkpoint=any_summary,
        wrong_generation_predecessor_present=wrong_generation_predecessor_present,
        wrong_generation_predecessor_checksum=wrong_generation_predecessor_checksum,
    )


# ---------------------------------------------------------------------------
# T1: generation-token derivation
# ---------------------------------------------------------------------------


def test_derive_generation_uses_manifest_12hex_convention() -> None:
    result = generation.derive_generation(NEW_CHECKSUM)
    assert result.startswith("manifest-")
    assert len(result) == len("manifest-") + 12
    # Deterministic re-derivation returns identical short form.
    assert generation.derive_generation(NEW_CHECKSUM) == result


def test_derive_generation_of_empty_checksum_is_manifest_empty() -> None:
    assert generation.derive_generation("") == "manifest-empty"
    assert generation.derive_generation(None) == "manifest-empty"


# ---------------------------------------------------------------------------
# T2: declaration loader — happy paths and error envelopes
# ---------------------------------------------------------------------------


def test_load_cutover_declaration_returns_none_for_empty_env() -> None:
    assert generation.load_cutover_declaration(None) is None
    assert generation.load_cutover_declaration("") is None


def test_load_cutover_declaration_parses_valid_file(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path)
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") is None
    entries = payload["entries"]
    assert len(entries) == 1
    assert entries[0]["model_id"] == "model_a"
    assert entries[0]["effective_cycle_utc"] == _dt("2026-07-06T12:00:00Z")
    assert payload["generation"] == NEW_GENERATION


def test_load_cutover_declaration_rejects_relative_path(tmp_path: Path) -> None:
    payload = generation.load_cutover_declaration("cutover.json")
    assert payload == {"_load_error": "declaration_path_not_absolute"}


def test_load_cutover_declaration_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    payload = generation.load_cutover_declaration(str(missing), now=NOW)
    assert payload == {"_load_error": "declaration_file_missing"}


def test_load_cutover_declaration_rejects_wrong_schema(tmp_path: Path) -> None:
    path = tmp_path / "cutover.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.wrong.v1",
                "generated_at": "2026-07-06T00:00:00Z",
                "generation": NEW_GENERATION,
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload == {"_load_error": "declaration_wrong_schema"}


def test_load_cutover_declaration_rejects_invalid_transition_mode(tmp_path: Path) -> None:
    # B1: schema enforces ``transition_mode`` enum → wrong_schema fires before
    # the semantic normalization loop.  The loader still rejects, and D8.8
    # maps every load-error other than ``declaration_file_missing`` to
    # ``block_declaration_stale`` so the operator remediation surface stays
    # consistent regardless of whether jsonschema or the semantic loop caught
    # the failure.
    path = _write_declaration(tmp_path, transition_mode="rebase")
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


def test_load_cutover_declaration_rejects_effective_cycle_off_hour(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T03:00:00Z")
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_entry_effective_cycle_invalid"


def test_load_cutover_declaration_rejects_non_hex_checksum(tmp_path: Path) -> None:
    # B1: schema pattern ``^[0-9a-f]{64}$`` catches this before the semantic
    # loop.  Uppercase hex is intentionally rejected here as well because the
    # publisher pattern is case-sensitive.
    path = _write_declaration(tmp_path, new_checksum="not-a-hex-string")
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


def test_load_cutover_declaration_rejects_duplicate_model_ids(tmp_path: Path) -> None:
    path = _write_declaration(
        tmp_path,
        extra_entries=[
            {
                "model_id": "model_a",
                "old_checksum": _hex("c"),
                "new_checksum": _hex("d"),
                "effective_cycle_utc": "2026-07-06T12:00:00Z",
                "transition_mode": "replace",
            }
        ],
    )
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_entry_model_id_invalid"


# ---------------------------------------------------------------------------
# T3: transition-decision matrix — 8 enum values, 1:1 typed reason
# ---------------------------------------------------------------------------


def test_transition_admits_warm_continue_when_predecessor_exists() -> None:
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=True,
            has_exact_predecessor=True,
            latest_any_checksum=NEW_CHECKSUM,
        ),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.WARM_CONTINUE
    assert evaluation.typed_reason is None
    assert evaluation.selected_predecessor is not None
    assert evaluation.selected_predecessor["cycle_id"] == "gfs_2026070600"
    assert evaluation.selected_predecessor["generation"] == NEW_GENERATION
    assert evaluation.cold_start_reason is None


def test_transition_admits_cold_new_model_when_no_history() -> None:
    evaluation = generation.evaluate_transition_decision(
        model_id="model_new",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=False, exists_current=False),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.COLD_NEW_MODEL
    assert evaluation.typed_reason is None
    assert evaluation.selected_predecessor is None
    assert evaluation.cold_start_reason == "no_prior_history"
    assert evaluation.generation == NEW_GENERATION


def test_transition_admits_cold_declared_cutover_at_effective_cycle(tmp_path: Path) -> None:
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.COLD_DECLARED_CUTOVER
    assert evaluation.typed_reason is None
    assert evaluation.cold_start_reason == "declared_cutover_at_effective_cycle"
    assert evaluation.declaration_evidence["bound_entry"]["model_id"] == "model_a"
    assert evaluation.declaration_evidence["bound_entry"]["transition_mode"] == "replace"


def test_transition_blocks_declaration_missing_for_package_change() -> None:
    """§8.5: an old-generation history + no declaration → block_declaration_missing."""
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_MISSING
    assert evaluation.typed_reason == "registry_cutover_declaration_missing"
    assert evaluation.selected_predecessor is None


def test_transition_blocks_declaration_stale_when_generation_mismatches(tmp_path: Path) -> None:
    """D8.2: declaration.generation must equal derive_generation(entry.new_checksum)."""
    path = _write_declaration(tmp_path, generation_field="manifest-wrong0000000")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    assert evaluation.typed_reason == "registry_cutover_declaration_stale"


def test_transition_blocks_declaration_stale_when_new_checksum_mismatches(
    tmp_path: Path,
) -> None:
    path = _write_declaration(tmp_path, new_checksum=_hex("c"))
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    assert evaluation.typed_reason == "registry_cutover_declaration_stale"


def test_transition_blocks_declaration_stale_on_file_load_error(tmp_path: Path) -> None:
    """A malformed declaration file blocks every relevant candidate."""
    path = tmp_path / "cutover.json"
    path.write_text("not-valid-json", encoding="utf-8")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    assert declaration is not None
    assert declaration.get("_load_error")
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    assert evaluation.typed_reason == "registry_cutover_declaration_stale"


def test_transition_blocks_cold_start_out_of_window_before_effective_cycle(
    tmp_path: Path,
) -> None:
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T12:00:00Z")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T00:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW
    assert evaluation.typed_reason == "registry_cutover_cold_start_out_of_window"


def test_transition_blocks_predecessor_pending_after_effective_cycle_without_new_gen_history(
    tmp_path: Path,
) -> None:
    """A cycle later than effective_cycle_utc must find the exact NEW-gen predecessor."""
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T00:00:00Z")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_PREDECESSOR_PENDING
    assert evaluation.typed_reason == "state_snapshot_index_prior_checkpoint_missing_after_history"
    assert evaluation.selected_predecessor is not None
    assert evaluation.selected_predecessor["generation"] == NEW_GENERATION


def test_transition_blocks_predecessor_pending_within_current_generation() -> None:
    """§8.4 + §8.6: same-generation successor missing exact predecessor blocks retryable."""
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=True,
            has_exact_predecessor=False,
            latest_any_checksum=NEW_CHECKSUM,
        ),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_PREDECESSOR_PENDING
    assert (
        evaluation.typed_reason == "state_snapshot_index_prior_checkpoint_missing_after_history"
    )
    assert evaluation.selected_predecessor is not None
    assert evaluation.selected_predecessor["source_id"] == "gfs"
    assert evaluation.selected_predecessor["lead_hours"] == 12


def test_transition_typed_reason_mapping_is_1_to_1() -> None:
    """D8.8: every block enum value maps to exactly one typed reason."""
    assert set(generation.TRANSITION_DECISION_REASONS.keys()) == generation.TransitionDecision.BLOCK
    values = list(generation.TRANSITION_DECISION_REASONS.values())
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# T4: generation_evidence bounded serialization
# ---------------------------------------------------------------------------


def test_generation_evidence_serialization_is_bounded() -> None:
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=False, exists_current=False),
        declaration=None,
    )
    evidence = generation.generation_evidence(evaluation)
    assert evidence["decision"] == generation.TransitionDecision.COLD_NEW_MODEL
    # Package checksum is redacted to a short prefix; the full checksum stays
    # only in the audit chain (registry manifest), never in bounded evidence.
    assert evidence["package_checksum_prefix"] == NEW_CHECKSUM[:12]
    assert "package_checksum" not in evidence


# ---------------------------------------------------------------------------
# T5: env-override safety (D8.9) — checked at the transition-decision layer.
#
# The scheduler-level regression that ``NHMS_REQUIRE_FORECAST_WARM_START=false``
# never admits a declaration-less cutover / missing predecessor is asserted at
# the module contract: ``evaluate_transition_decision`` has no env input.
# Below we re-confirm that no code path in the module reads the env.
# ---------------------------------------------------------------------------


def test_transition_decision_module_does_not_read_env_flags(monkeypatch: Any) -> None:
    """D8.9: transition decisions are env-independent by construction."""
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=None,
    )
    # Without a declaration and with old-generation history, the decision
    # must be block_declaration_missing regardless of the env flag.
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_MISSING
    assert evaluation.typed_reason == "registry_cutover_declaration_missing"


# ---------------------------------------------------------------------------
# T6: declaration.match_declaration_entry safety
# ---------------------------------------------------------------------------


def test_match_declaration_entry_returns_none_for_load_error() -> None:
    assert (
        generation.match_declaration_entry(
            {"_load_error": "any_error"},
            model_id="model_a",
        )
        is None
    )


def test_match_declaration_entry_returns_matching_row(tmp_path: Path) -> None:
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)), now=NOW)
    entry = generation.match_declaration_entry(declaration, model_id="model_a")
    assert entry is not None
    assert entry["model_id"] == "model_a"


def test_match_declaration_entry_returns_none_for_unknown_model(tmp_path: Path) -> None:
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)), now=NOW)
    assert generation.match_declaration_entry(declaration, model_id="model_b") is None


# ---------------------------------------------------------------------------
# #1433: the shared declaration may now carry `retire` entries.  This consumer
# derives nothing from them, but it MUST NOT be disabled by their presence.
# ---------------------------------------------------------------------------


def _retire_entry(model_id: str = "model_r") -> dict[str, Any]:
    return {
        "model_id": model_id,
        "old_checksum": OLD_CHECKSUM,
        "new_checksum": None,
        "effective_cycle_utc": "2026-07-06T12:00:00Z",
        "transition_mode": "retire",
    }


def test_consumer_transition_modes_match_the_shared_schema_enum() -> None:
    """The consumer's copy of the constant is the schema enum, not a subset:
    a mode the schema admits must not turn the whole shared file into a
    ``_load_error`` here."""
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_registry_package_cutover.schema.json"
        ).read_text(encoding="utf-8")
    )
    schema_enum = set(
        schema["properties"]["entries"]["items"]["properties"]["transition_mode"]["enum"]
    )

    assert set(generation.CUTOVER_TRANSITION_MODES) == schema_enum


def test_declaration_with_a_retire_entry_still_loads_and_matches_replace_entries(
    tmp_path: Path,
) -> None:
    """#1433/I8: a retirement in the file must not cost the replace entry its
    binding — before the tolerant skip, the consumer failed the WHOLE file with
    ``declaration_entry_transition_mode_invalid`` and every candidate fell to
    ``block_declaration_missing`` / ``block_declaration_stale``."""
    path = _write_declaration(tmp_path, extra_entries=[_retire_entry()])

    declaration = generation.load_cutover_declaration(str(path), now=NOW)

    assert declaration is not None
    assert "_load_error" not in declaration
    entry = generation.match_declaration_entry(declaration, model_id="model_a")
    assert entry is not None
    assert entry["transition_mode"] == "replace"
    assert entry["new_checksum"] == NEW_CHECKSUM


def test_retire_entries_never_match_and_never_produce_none_strings(
    tmp_path: Path,
) -> None:
    """The retirement is skipped before checksum normalization, so no candidate
    can bind it and no ``"None"`` string reaches generation derivation."""
    path = _write_declaration(tmp_path, extra_entries=[_retire_entry()])

    declaration = generation.load_cutover_declaration(str(path), now=NOW)

    assert generation.match_declaration_entry(declaration, model_id="model_r") is None
    assert [entry["model_id"] for entry in declaration["entries"]] == ["model_a"]
    assert "None" not in json.dumps(declaration, default=str)


def test_a_retire_entry_does_not_hide_a_duplicate_model_id(tmp_path: Path) -> None:
    """The skip sits AFTER the duplicate-id check, so a file that names one
    model twice is still rejected even when the duplicate is a retirement."""
    path = _write_declaration(
        tmp_path, extra_entries=[_retire_entry(model_id="model_a")]
    )

    declaration = generation.load_cutover_declaration(str(path), now=NOW)

    assert declaration["_load_error"] == "declaration_entry_model_id_invalid"


# ---------------------------------------------------------------------------
# T7: §8.9 scheduler-level regression — NHMS_REQUIRE_FORECAST_WARM_START=false
# must NOT admit a declaration-less cutover / missing predecessor / wrong-
# generation checkpoint.  End-to-end through _strict_warm_start_for_candidate.
# ---------------------------------------------------------------------------


def test_env_override_does_not_admit_declaration_less_cutover(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9: with old-generation history + no declaration, the env cannot bypass."""
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    # Old-generation history triggers the cutover boundary; a valid
    # candidate needs a declaration OR strict warm-start blocking.
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[_old_generation_state_entry(roots)],
    )
    # Model with a valid current-generation package_checksum in its
    # resource_profile so §8 gating fires.
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    # §8.9 CRITICAL: set the env to false — this must NOT loosen the gate.
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 12)),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "declaration-less cutover must not build orchestrator"
        ),
    )
    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    # The env is false but the transition matrix still blocks — D8.9 proven.
    assert blocked[0].reason == "registry_cutover_declaration_missing"
    assert (
        blocked[0].state_evidence["registry_cutover_transition"]["decision"]
        == "block_declaration_missing"
    )


# ---------------------------------------------------------------------------
# T8 (A1): BLOCK_WRONG_GENERATION emission — dead-code fix
# ---------------------------------------------------------------------------


def test_transition_blocks_wrong_generation_at_expected_predecessor_key(
    tmp_path: Path,
) -> None:
    """§8.3 spec Scenario: a wrong-generation checkpoint at the expected
    predecessor key must emit ``block_wrong_generation`` — the enum value
    now has a live return path (round-1 A1 fix)."""
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T00:00:00Z")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=False,
            latest_any_checksum=OLD_CHECKSUM,
            wrong_generation_predecessor_present=True,
            wrong_generation_predecessor_checksum=OLD_CHECKSUM,
        ),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_WRONG_GENERATION
    assert evaluation.typed_reason == "state_snapshot_index_generation_mismatch"
    # Bounded evidence carries the mismatching checksum prefix for audit.
    assert (
        evaluation.declaration_evidence["wrong_generation_predecessor_checksum_prefix"]
        == OLD_CHECKSUM[:12]
    )


def test_transition_blocks_wrong_generation_within_current_generation_history() -> None:
    """(e) branch: current-gen history exists but the exact predecessor key
    holds a wrong-generation entry — block_wrong_generation, not pending."""
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=True,
            has_exact_predecessor=False,
            latest_any_checksum=NEW_CHECKSUM,
            wrong_generation_predecessor_present=True,
            wrong_generation_predecessor_checksum=OLD_CHECKSUM,
        ),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_WRONG_GENERATION
    assert evaluation.typed_reason == "state_snapshot_index_generation_mismatch"


# ---------------------------------------------------------------------------
# T9 (B3): loader NEVER raises on deeply-nested JSON payloads
# ---------------------------------------------------------------------------


def test_load_cutover_declaration_handles_recursion_error_on_deeply_nested_json(
    tmp_path: Path,
) -> None:
    """A deeply-nested-but-under-256KB payload must NOT crash the scheduler
    pass — round-1 B3 fix adds ``RecursionError`` to the loader's except.

    The depth is pinned at a value measured to raise ``RecursionError``
    deterministically on every supported CPython version (``requires-python =
    ">=3.11"``; 3.11 — the CI Python, see .github/workflows/ci.yml — through
    3.14 measured), so the ``declaration_malformed_json`` branch is really
    exercised everywhere instead of only where one interpreter's internal
    limits happen to bite.  The ``setrecursionlimit(1000)`` wrapper stays: on
    3.11 it is the determinism source (the JSON scanner honors the Python-level
    limit, so parsing dies at depth 995), while 3.12+ guard C recursion
    independently of that limit and the threshold is version-dependent —
    measured first-raising depth 9998 on 3.12, 9999 on 3.13, and ~74.4k on 3.14
    (74381 here; 3.14 sizes the guard off actual C-stack headroom, so that one
    drifts with the stack in use).  Depth 20000 therefore PARSES on 3.14 and
    would falsely red as ``declaration_not_object``; depth 100000 was measured
    through this very loader to return ``declaration_malformed_json`` on all of
    3.11/3.12/3.13/3.14.  The payload also stays under
    ``MAX_CUTOVER_DECLARATION_BYTES`` (200000 < 262144, asserted below) so the
    loader reaches the JSON parse rather than the oversize branch.  The
    adjacent non-recursive malformed shape (a top-level list) is pinned by its
    own case below, so neither error code depends on the interpreter version.
    """
    depth = 100000
    payload = "[" * depth + "]" * depth
    path = tmp_path / "cutover-deep.json"
    path.write_text(payload, encoding="utf-8")
    assert path.stat().st_size < MAX_CUTOVER_DECLARATION_BYTES
    previous_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        result = generation.load_cutover_declaration(str(path), now=NOW)
    finally:
        sys.setrecursionlimit(previous_recursion_limit)
    assert isinstance(result, dict)
    assert result.get("_load_error") == "declaration_malformed_json"


def test_load_cutover_declaration_reports_not_object_on_top_level_list(
    tmp_path: Path,
) -> None:
    """A shallow top-level JSON list is well-formed JSON of the wrong shape: the
    loader routes it to ``declaration_not_object``.  This case involves no
    nesting, so the code is pinned independently of any interpreter's recursion
    behavior — it can never become the accidental outcome of the recursion case
    above.
    """
    path = tmp_path / "cutover-list.json"
    path.write_text(json.dumps([{"model_id": "model_a"}]), encoding="utf-8")
    assert path.stat().st_size < MAX_CUTOVER_DECLARATION_BYTES
    result = generation.load_cutover_declaration(str(path), now=NOW)
    assert result == {"_load_error": "declaration_not_object"}


# ---------------------------------------------------------------------------
# T10 (B4): configured-but-missing declaration → block_declaration_missing
# ---------------------------------------------------------------------------


def test_transition_blocks_declaration_missing_when_configured_file_absent(
    tmp_path: Path,
) -> None:
    """Round-1 B4 fix: configured env + file absent maps to
    ``block_declaration_missing`` (typed reason
    ``registry_cutover_declaration_missing``), not stale.  The stale mapping
    is reserved for load errors that come from present-but-invalid content."""
    missing = tmp_path / "not-there.json"
    declaration = generation.load_cutover_declaration(str(missing), now=NOW)
    assert declaration == {"_load_error": "declaration_file_missing"}
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_MISSING
    assert evaluation.typed_reason == "registry_cutover_declaration_missing"


# ---------------------------------------------------------------------------
# T11 (C1): AC7 IFS coverage — parametrize cutover admit, cold-start,
# and wrong-generation-block tests across GFS and IFS source_ids.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_transition_admits_cold_declared_cutover_per_source(
    source_id: str, tmp_path: Path
) -> None:
    declaration = generation.load_cutover_declaration(
        str(_write_declaration(tmp_path)), now=NOW
    )
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id=source_id,
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.COLD_DECLARED_CUTOVER


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_transition_admits_cold_new_model_per_source(source_id: str) -> None:
    evaluation = generation.evaluate_transition_decision(
        model_id="model_new",
        package_checksum=NEW_CHECKSUM,
        source_id=source_id,
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=False, exists_current=False),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.COLD_NEW_MODEL


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_transition_blocks_wrong_generation_per_source(source_id: str) -> None:
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id=source_id,
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=True,
            has_exact_predecessor=False,
            latest_any_checksum=NEW_CHECKSUM,
            wrong_generation_predecessor_present=True,
            wrong_generation_predecessor_checksum=OLD_CHECKSUM,
        ),
        declaration=None,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_WRONG_GENERATION


# ---------------------------------------------------------------------------
# T12 (C2): AC7 13-continuing + 6-new-model spread
# ---------------------------------------------------------------------------


def _build_registry_state(
    continuing_count: int, new_count: int
) -> list[dict[str, Any]]:
    """Return one model spec per row in a 13→19 registry step.

    Continuing rows carry the NEW checksum + existing state history in the
    same generation → warm_continue.  New rows carry the NEW checksum but
    no state history → cold_new_model.  The helper reuses ``NEW_CHECKSUM``
    for both because §8's admit decisions turn on history presence, not on
    per-row checksum diversity.
    """
    return [
        {
            "model_id": f"model_continue_{index:02d}",
            "package_checksum": NEW_CHECKSUM,
            "has_history_current": True,
            "has_exact_predecessor": True,
        }
        for index in range(continuing_count)
    ] + [
        {
            "model_id": f"model_new_{index:02d}",
            "package_checksum": NEW_CHECKSUM,
            "has_history_current": False,
            "has_exact_predecessor": False,
        }
        for index in range(new_count)
    ]


def test_transition_matrix_13_continuing_plus_6_new_models_produces_expected_histogram() -> None:
    """AC7: a registry step from 13 → 19 models must yield 13 warm_continue
    and 6 cold_new_model decisions with no accidental blocks."""
    registry = _build_registry_state(13, 6)
    histogram: dict[str, int] = {}
    for spec in registry:
        history = _signal(
            exists_any=spec["has_history_current"],
            exists_current=spec["has_history_current"],
            has_exact_predecessor=spec["has_exact_predecessor"],
            latest_any_checksum=NEW_CHECKSUM,
        )
        evaluation = generation.evaluate_transition_decision(
            model_id=spec["model_id"],
            package_checksum=spec["package_checksum"],
            source_id="gfs",
            candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
            required_lead_hours=12,
            history=history,
            declaration=None,
        )
        histogram[evaluation.decision] = histogram.get(evaluation.decision, 0) + 1
    assert histogram == {
        generation.TransitionDecision.WARM_CONTINUE: 13,
        generation.TransitionDecision.COLD_NEW_MODEL: 6,
    }


# ---------------------------------------------------------------------------
# T13 (C3, R2-A2 rewrite): retry / restart across a cutover.  Real
# ProductionScheduler + real _cutover_declaration_cache seam.
# ---------------------------------------------------------------------------


def test_transition_decision_idempotent_across_scheduler_restart(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """R2-A2: exercise the real per-lifetime cache seam.

    Instantiate a real ``ProductionScheduler`` twice (fresh cache each time)
    against the same declaration file on disk and assert the emitted §8
    admit shape survives the "restart".  Between passes we EVICT the cache
    by re-instantiating ``ProductionScheduler`` — proving the loader
    reproduces the same decision even when the module-lifetime cache is
    thrown away.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator import scheduler_generation_gate as _generation_gate
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[_old_generation_state_entry(roots)],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }

    decisions: list[str] = []
    for _pass in range(2):
        # Restart: fresh ProductionScheduler → fresh
        # ``_cutover_declaration_cache`` sentinel.
        scheduler = ProductionScheduler(
            ProductionSchedulerConfig(
                now=generated_at, allowed_cycle_hours_utc=(0, 12)
            ),
            registry=FakeRegistry([model]),
            adapters={},
            orchestrator_factory=lambda _source_id: pytest.fail(
                "declaration-less cutover must not build orchestrator"
            ),
        )
        # Cache starts at the UNLOADED sentinel — sanity that the "restart"
        # actually zeroed the per-lifetime state.
        assert (
            scheduler._cutover_declaration_cache
            is _generation_gate.CUTOVER_DECLARATION_UNLOADED
        )
        candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
            models=[scheduler_module._coerce_registered_model(model)],
            cycles=[
                scheduler_module.SchedulerSourceCycle(
                    discovery=CycleDiscovery(
                        cycle_id="gfs_2026052112",
                        source_id="gfs",
                        cycle_time=cycle_time,
                        cycle_hour=12,
                        available=True,
                        status="discovered",
                    ),
                    horizon={},
                )
            ],
        )
        assert candidates == []
        assert len(blocked) == 1
        decisions.append(blocked[0].reason)
        # After the pass the sentinel has been replaced (loader ran) —
        # this proves the cache seam actually fired during the pass.
        assert (
            scheduler._cutover_declaration_cache
            is not _generation_gate.CUTOVER_DECLARATION_UNLOADED
        )
    # Both passes produced the same decision, proving §8 gating idempotent
    # under a cold cache restart.
    assert decisions == ["registry_cutover_declaration_missing"] * 2


# ---------------------------------------------------------------------------
# T14 (C4, R2-A2 rewrite): concurrent scheduler-plan calls survive shared
# per-lifetime caches without torn writes.
# ---------------------------------------------------------------------------


def test_transition_decision_survives_concurrent_evaluation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """R2-A2: fan out 4 threads against the SAME ``ProductionScheduler`` so
    they share the per-lifetime cache; assert every thread saw the same
    §8 decision AND that ``_cutover_declaration_cache`` did not tear
    (single entry after the fan-out)."""
    import threading

    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[_old_generation_state_entry(roots)],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "declaration-less cutover must not build orchestrator"
        ),
    )

    coerced = scheduler_module._coerce_registered_model(model)
    source_cycles = [
        scheduler_module.SchedulerSourceCycle(
            discovery=CycleDiscovery(
                cycle_id="gfs_2026052112",
                source_id="gfs",
                cycle_time=cycle_time,
                cycle_hour=12,
                available=True,
                status="discovered",
            ),
            horizon={},
        )
    ]

    reasons: list[str] = []
    lock = threading.Lock()

    def _run() -> None:
        _cands, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
            models=[coerced],
            cycles=source_cycles,
        )
        with lock:
            reasons.append(blocked[0].reason if blocked else "")

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Cross-thread invariant #1: all 4 threads observed the same §8 decision
    # (cache did not tear; no thread saw a partial mutation).
    assert reasons == ["registry_cutover_declaration_missing"] * 4
    # Cross-thread invariant #2: the per-lifetime declaration cache is a
    # scalar sentinel-based slot (see scheduler_core.py:138 —
    # ``_cutover_declaration_cache: Any = _CUTOVER_DECLARATION_UNLOADED``,
    # then replaced by the loader with either ``None`` (env unset) or the
    # parsed declaration dict).  After the fan-out the sentinel MUST have
    # been replaced — proving the loader actually fired inside at least one
    # thread — AND the final settled value MUST be ``None`` because this
    # fixture leaves ``CUTOVER_DECLARATION`` env unset.  If two threads
    # torn-wrote different terminal values, this assertion fails.
    # R1-A4 invariant: single-value pin, no OR-set. See
    # .workplans/1081/review/review-failure-retro-round3.md.
    from services.orchestrator import scheduler_generation_gate as _generation_gate
    assert (
        scheduler._cutover_declaration_cache
        is not _generation_gate.CUTOVER_DECLARATION_UNLOADED
    )
    assert scheduler._cutover_declaration_cache is None


# ---------------------------------------------------------------------------
# T15 (C5, R2-A3 rewrite): env-override end-to-end coverage for (b)/(c).
#
# Both drive the real ``ProductionScheduler._build_candidates`` seam so
# the env-override protection at ``scheduler_core._strict_warm_start_for_
# candidate`` / ``scheduler_generation_gate.forecast_warm_start_env_enabled``
# is genuinely exercised.  Mirrors the (a) pattern at T7.
# ---------------------------------------------------------------------------


def test_env_override_does_not_admit_missing_predecessor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9 (b): env=false + generation-matched history but no predecessor
    checkpoint → §8 blocks with
    ``state_snapshot_index_prior_checkpoint_missing_after_history``.

    Fixture (#1109 reshape): a valid, in-window declaration (effective
    2026-05-21T00Z, 18h before ``now`` and inside the 24h past tolerance) so
    the gate clears the declaration layer, plus ONE current-generation state
    entry at 2026-05-21T00Z — strictly earlier than the 12Z candidate cycle
    and NOT at the expected predecessor identity key
    (``valid_time`` = candidate cycle 12Z, ``cycle_id`` = gfs_2026052100,
    ``lead_hours`` = 12).  The transition matrix therefore takes the
    current-generation branch and returns ``block_predecessor_pending``; the
    gate falls through to the exact-warm-start check, which reports
    ``state_snapshot_index_exact_checkpoint_missing``, and — because usable
    history DOES exist — settles on the AC5 defect branch this test guards
    (``scheduler_generation_gate.py`` fallback reason).
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    # Current-generation history strictly BEFORE the candidate cycle and away
    # from the expected predecessor key: history_exists is True while the
    # exact predecessor checkpoint stays absent.
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[
            _old_generation_state_entry(
                roots,
                old_package_checksum=candidate_checksum,
                state_id="state_current_gen_prior_history",
                valid_time="2026-05-21T00:00:00Z",
                cycle_id="gfs_2026052012",
                lead_hours=12,
            )
        ],
    )
    # Valid, in-window declaration binding the candidate generation.  Its
    # effective cycle is 00Z, NOT the candidate's 12Z, so a mis-seeded index
    # can never admit via cold_declared_cutover — it would fail loudly.
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    # The successor blocks; env=false did NOT bypass §8.
    assert candidates == []
    assert len(blocked) == 1
    # R1-A4 invariant: single-value pin, no OR-set. See
    # .workplans/1081/review/review-failure-retro-round3.md.
    # #1109: the promised state-lineage branch — generation-matched history
    # exists but the expected predecessor checkpoint does not.
    assert (
        blocked[0].reason
        == "state_snapshot_index_prior_checkpoint_missing_after_history"
    )
    state_evidence = blocked[0].state_evidence
    transition_decision = (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
    )
    assert (
        transition_decision
        == generation.TransitionDecision.BLOCK_PREDECESSOR_PENDING
    )
    # #1152: this is the self-healing population — the emitted §8.6
    # predecessor's OWN exact warm-start state exists (latest usable state sits
    # exactly at ``required_prior_cycle_time`` = T − lead_hours = 00Z), so the
    # single-level backfill closes the gap.  No operator action is named and no
    # runbook pointer is attached.
    assert state_evidence["state_history"]["history_exists"] is True
    assert (
        state_evidence["state_history"]["latest_usable_state"]["valid_time"]
        == state_evidence["required_prior_cycle_time"]
    )
    assert state_evidence["self_heal_expected"] is True
    assert state_evidence["operator_action_required"] is False
    assert "operator_action" not in state_evidence
    assert "runbook" not in state_evidence
    # Round-2: the signal is the predecessor's OWN full warm-start probe, not
    # a valid_time comparison — here it verifies clean and reports ready.
    assert state_evidence["self_heal_probe"] == {"ready": True, "reason": None}
    # Non-goal guard: the failure block is untouched by the additive signal.
    assert state_evidence["failure"]["retryable"] is True
    assert state_evidence["failure"]["permanent"] is False


def test_multi_cycle_gap_flags_operator_action_despite_earlier_history(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """#1152: ≥2-cycle gap → operator action even though history_exists=True.

    Fixture = the T15(b) self-heal anchor above with ONE state-index change:
    the single current-generation entry moves back one more cycle, to
    ``valid_time`` 2026-05-20T12Z (= T − 2·lead_hours for the 12Z candidate).
    The strictly-earlier probe still reports ``history_exists=True``, but the
    latest usable state is NOT the emitted §8.6 predecessor's own warm-start
    state (``required_prior_cycle_time`` = 2026-05-21T00Z).  §8.6 steps back a
    single level per pass, so the emitted predecessor blocks on exactly this
    shape again and the gap is a fixpoint: it must NOT be labeled self-healing.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    # Current-generation history TWO cycles before the candidate: the
    # generation-scoped matrix signal still sees current-generation history
    # (→ block_predecessor_pending) and the strictly-earlier probe still
    # reports history_exists=True, but the predecessor's own state is absent.
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[
            _old_generation_state_entry(
                roots,
                old_package_checksum=candidate_checksum,
                state_id="state_current_gen_two_cycle_gap",
                valid_time="2026-05-20T12:00:00Z",
                cycle_id="gfs_2026052000",
                lead_hours=12,
            )
        ],
    )
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    assert (
        blocked[0].reason
        == "state_snapshot_index_prior_checkpoint_missing_after_history"
    )
    state_evidence = blocked[0].state_evidence
    assert (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
        == "block_predecessor_pending"
    )
    # Round-2: the discriminator is NOT history_exists — it is True here — and
    # NOT a valid_time comparison either; it is the emitted predecessor's OWN
    # full warm-start probe (``strict_warm_start_evidence(
    # valid_time=required_prior_cycle_time, …).ready``).  The valid_time values
    # asserted below merely document this fixture's geometry (latest usable
    # state one cycle short of the predecessor slot); they are not the rule.
    assert state_evidence["state_history"]["history_exists"] is True
    assert state_evidence["required_prior_cycle_time"] == "2026-05-21T00:00:00Z"
    assert (
        state_evidence["state_history"]["latest_usable_state"]["valid_time"]
        == "2026-05-20T12:00:00Z"
    )
    assert state_evidence["self_heal_expected"] is False
    assert state_evidence["operator_action_required"] is True
    assert state_evidence["operator_action"] == "backfill_predecessor_state"
    assert (
        state_evidence["runbook"]
        == "docs/runbooks/scheduler-dbfree-typed-reasons.md"
    )
    # Round-2: the predecessor's own probe finds nothing at 00Z at all.
    assert state_evidence["self_heal_probe"] == {
        "ready": False,
        "reason": "state_snapshot_index_exact_checkpoint_missing",
    }
    # Non-goal guard: the failure block is untouched by the additive signal.
    assert state_evidence["failure"]["retryable"] is True
    assert state_evidence["failure"]["permanent"] is False


def test_wrong_generation_state_at_predecessor_slot_flags_operator_action(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """#1152 round-2: OLD-generation checkpoint at T − lead → operator action.

    Fixture = the T15(b) self-heal anchor with the entry at the predecessor
    slot (2026-05-21T00Z) swapped to an OLD-generation checksum, plus a
    current-generation entry one cycle further back (2026-05-20T12Z) so the
    transition matrix still reaches ``block_predecessor_pending``.

    ``usable_state_history_evidence`` is generation-blind (it filters on
    ``usable_flag`` only, ``state_manager.py`` :1297-1304), so the history
    probe's ``latest_usable_state.valid_time`` DOES equal
    ``required_prior_cycle_time`` here — a valid_time-only predicate would
    label this self-healing.  It is not: the emitted §8.6 predecessor's own
    gate verifies lineage and blocks permanently on the generation mismatch.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[
            # OLD-generation checkpoint sitting exactly at the predecessor
            # slot (T − lead_hours = 2026-05-21T00Z).
            _old_generation_state_entry(
                roots,
                old_package_checksum="a" * 64,
                state_id="state_old_gen_at_predecessor_slot",
                valid_time="2026-05-21T00:00:00Z",
                cycle_id="gfs_2026052012",
                lead_hours=12,
            ),
            # Current-generation history further back so the matrix takes the
            # current-generation branch (block_predecessor_pending).
            _old_generation_state_entry(
                roots,
                old_package_checksum=candidate_checksum,
                state_id="state_current_gen_two_cycle_gap",
                valid_time="2026-05-20T12:00:00Z",
                cycle_id="gfs_2026052000",
                lead_hours=12,
            ),
        ],
    )
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    # Non-goal guard: the typed reason is unchanged by the round-2 predicate.
    assert (
        blocked[0].reason
        == "state_snapshot_index_prior_checkpoint_missing_after_history"
    )
    state_evidence = blocked[0].state_evidence
    assert (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
        == "block_predecessor_pending"
    )
    # The generation-blind trap this test exists for: the history probe DOES
    # report a usable state exactly at required_prior_cycle_time.
    assert state_evidence["required_prior_cycle_time"] == "2026-05-21T00:00:00Z"
    assert (
        state_evidence["state_history"]["latest_usable_state"]["valid_time"]
        == "2026-05-21T00:00:00Z"
    )
    # …and the signal still says "operator", because the provider probe runs
    # the predecessor's OWN lineage verification.
    assert state_evidence["self_heal_expected"] is False
    assert state_evidence["operator_action_required"] is True
    assert state_evidence["operator_action"] == "backfill_predecessor_state"
    assert (
        state_evidence["runbook"]
        == "docs/runbooks/scheduler-dbfree-typed-reasons.md"
    )
    assert state_evidence["self_heal_probe"]["ready"] is False
    assert (
        state_evidence["self_heal_probe"]["reason"]
        == "state_snapshot_index_model_package_checksum_mismatch"
    )
    # Non-goal guard: the failure block is untouched by the additive signal.
    assert state_evidence["failure"]["retryable"] is True
    assert state_evidence["failure"]["permanent"] is False


def test_missing_state_object_at_predecessor_slot_flags_operator_action(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """#1152 round-2: current-gen entry at T − lead whose object is GONE.

    Fixture = the T15(b) self-heal anchor verbatim, then the predecessor
    slot's state object is deleted from the object store after the index was
    published.  ``usable_state_history_evidence`` never opens the object
    (``state_manager.py`` :1297-1317), so the history probe still reports the
    entry as the latest usable state; the emitted §8.6 predecessor's own gate
    opens it and blocks on ``state_snapshot_index_object_missing``
    (``state_manager.py`` :2546-2551).
    """
    from packages.common.object_store import LocalObjectStore
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    predecessor_entry = _old_generation_state_entry(
        roots,
        old_package_checksum=candidate_checksum,
        state_id="state_current_gen_prior_history",
        valid_time="2026-05-21T00:00:00Z",
        cycle_id="gfs_2026052012",
        lead_hours=12,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[predecessor_entry],
    )
    # Delete the state object AFTER publishing so the index still advertises
    # a usable current-generation entry at the predecessor slot.
    LocalObjectStore(roots["object_store_root"], "s3://nhms").resolve_path(
        predecessor_entry["state_uri"]
    ).unlink()
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    assert (
        blocked[0].reason
        == "state_snapshot_index_prior_checkpoint_missing_after_history"
    )
    state_evidence = blocked[0].state_evidence
    assert (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
        == "block_predecessor_pending"
    )
    # Object-blind history probe: still the latest usable state at T − lead.
    assert state_evidence["required_prior_cycle_time"] == "2026-05-21T00:00:00Z"
    assert (
        state_evidence["state_history"]["latest_usable_state"]["valid_time"]
        == "2026-05-21T00:00:00Z"
    )
    assert state_evidence["self_heal_expected"] is False
    assert state_evidence["operator_action_required"] is True
    assert state_evidence["operator_action"] == "backfill_predecessor_state"
    assert (
        state_evidence["runbook"]
        == "docs/runbooks/scheduler-dbfree-typed-reasons.md"
    )
    assert state_evidence["self_heal_probe"]["ready"] is False
    assert (
        state_evidence["self_heal_probe"]["reason"]
        == "state_snapshot_index_object_missing"
    )
    assert state_evidence["failure"]["retryable"] is True
    assert state_evidence["failure"]["permanent"] is False


def test_future_only_history_admits_cold_new_model_with_env_override(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """#1775 D5 at the ``_build_candidates`` seam: a run's own output is not history.

    Fixture = T15(b) with ONE state-index change: the single
    current-generation entry sits at ``valid_time`` 2026-05-22T00Z, strictly
    LATER than the 12Z candidate cycle (``cycle_id`` = gfs_2026052112,
    ``lead_hours`` = 12) — i.e. an entry only this candidate's OWN run (or a
    later cycle) could have produced.

    Before D5 this test asserted the opposite outcome.  ``state_manager.py``
    ``generation_scoped_history_signal`` was valid_time-agnostic, so that
    future entry counted as current-generation history → branch (e) →
    ``block_predecessor_pending`` → the candidate blocked with
    ``state_snapshot_index_prior_checkpoint_missing_after_history``, forever,
    demanding a predecessor cycle that had never existed.  That is precisely
    the wedge #1775 fixes: with history-existence scoped to
    ``valid_time <= cutoff`` the entry no longer counts, the model reads as
    what it is — one with no prior history — and the packaged-IC bootstrap
    branch (``cold_new_model``) stays open.

    The #1150 fail-open guard this fixture used to carry (the gate's
    warm_continue-only passthrough) is NOT lost: under D5 the split-predicate
    geometry it needed is unreachable through ``_build_candidates``, so the pin
    now lives at the gate seam in
    ``test_predecessor_pending_without_earlier_history_still_blocks_at_gate``.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    # Current-generation history strictly AFTER the candidate cycle: the
    # strictly-earlier probe sees nothing while the generation-scoped signal
    # still counts this entry.  NOT at valid_time == candidate cycle — that
    # shape settles on ``state_snapshot_index_cycle_id_mismatch`` instead
    # (lead_hours still matches; the expected cycle_id gfs_2026052100 does
    # not) and never reaches the branch under test.
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[
            _old_generation_state_entry(
                roots,
                old_package_checksum=candidate_checksum,
                state_id="state_current_gen_later_history",
                valid_time="2026-05-22T00:00:00Z",
                cycle_id="gfs_2026052112",
                lead_hours=12,
            )
        ],
    )
    # Valid, in-window declaration — identical to T15(b) so the only variable
    # is the state-index geometry.
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    # D5: the future-dated entry is not history, so nothing blocks and the
    # bootstrap admit fires.  No predecessor is demanded and no operator
    # backfill signal is emitted — there is nothing to back-fill.
    assert blocked == []
    assert len(candidates) == 1
    state_evidence = candidates[0].state_evidence
    assert state_evidence["mode"] == "db_free_cold_new_model"
    assert state_evidence["cold_start_reason"] == "no_prior_history"
    assert (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
        == "cold_new_model"
    )


def test_future_only_history_admits_cold_new_model_under_strict_warm_start(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """#1775 D5: the same future-only fixture under
    ``NHMS_REQUIRE_FORECAST_WARM_START=true``.

    Strict warm start does not forbid a cold bootstrap — it forbids running a
    model FORWARD without the checkpoint its own history implies.  A model with
    no usable entry at or before its cutoff has no such implication, so the
    env=true leg reaches the same ``cold_new_model`` admit as the env=false
    sibling.  Pinned as a pair so the relaxation cannot be read as env-scoped.

    Before D5 this asserted a block with
    ``state_snapshot_index_exact_checkpoint_missing``: the future entry made
    the generation-scoped signal report history, which routed the strict leg
    into demanding an exact predecessor at a cycle that never ran.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[
            _old_generation_state_entry(
                roots,
                old_package_checksum=candidate_checksum,
                state_id="state_current_gen_later_history",
                valid_time="2026-05-22T00:00:00Z",
                cycle_id="gfs_2026052112",
                lead_hours=12,
            )
        ],
    )
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "predecessor-pending cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert blocked == []
    assert len(candidates) == 1
    state_evidence = candidates[0].state_evidence
    assert state_evidence["mode"] == "db_free_cold_new_model"
    assert state_evidence["cold_start_reason"] == "no_prior_history"
    assert (
        state_evidence.get("registry_cutover_transition", {}).get("decision")
        == "cold_new_model"
    )
    # #1152 absence pin, retained: the cold admit is not an operator-signal
    # site either, so none of the backfill signal fields may appear here.
    assert "operator_action_required" not in state_evidence
    assert "self_heal_expected" not in state_evidence
    assert "self_heal_probe" not in state_evidence
    assert "operator_action" not in state_evidence


def test_env_override_does_not_admit_stale_declaration(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9: env=false + a stale declaration → ``block_declaration_stale``.

    Restores the stale-propagation integration pin that the #1109 T15(b)
    reshape removed: before the reshape, T15(b)'s fixture settled on
    ``registry_cutover_declaration_stale``, and it was this repo's only
    ``_build_candidates``-level assertion proving that
    ``TransitionDecision.BLOCK_DECLARATION_STALE`` is a member of
    ``scheduler_generation_gate._DECLARATION_LEVEL_BLOCKS``.  Reusing the old
    geometry (old-generation history + env override off) with the D8.2
    generation-field mismatch as the explicit stale trigger, this test keeps
    that integration seam covered while T15(b) pins the state-lineage branch.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    # Old-generation-only history puts the candidate on the cutover boundary
    # (§8.4 branch (d)), where the declaration must bind identity.
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[_old_generation_state_entry(roots)],
    )
    # Schema-valid, in-window declaration whose ``generation`` field does NOT
    # equal ``derive_generation(entry.new_checksum)`` — the D8.2
    # ``generation_field_mismatch`` stale trigger.  ``new_checksum`` still
    # binds the candidate so the earlier ``new_checksum_mismatch`` branch
    # cannot claim the decision.
    stale_generation = "manifest-000000000000"
    assert stale_generation != generation.derive_generation(candidate_checksum)
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": stale_generation,
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    # §8.9 CRITICAL: the env is false — it must NOT loosen the gate.
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "stale-declaration cutover must not build orchestrator"
        ),
    )
    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    # R1-A4 invariant: single-value pin, no OR-set. See
    # .workplans/1081/review/review-failure-retro-round3.md.
    # This is the assertion that dies if BLOCK_DECLARATION_STALE is dropped
    # from ``scheduler_generation_gate._DECLARATION_LEVEL_BLOCKS``.
    assert blocked[0].reason == "registry_cutover_declaration_stale"
    transition_decision = (
        blocked[0].state_evidence.get("registry_cutover_transition", {}).get(
            "decision"
        )
    )
    assert (
        transition_decision
        == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    )


def test_env_override_does_not_admit_wrong_generation_checkpoint(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9 (c): env=false + a wrong-generation checkpoint at the expected
    predecessor key → §8 blocks with
    ``state_snapshot_index_generation_mismatch``.

    Fixture (#1109 reshape): a valid, in-window declaration plus TWO state
    entries — an OLD-generation one AT the expected predecessor identity key
    (``valid_time`` = candidate cycle 2026-05-21T12Z, ``cycle_id`` =
    gfs_2026052100, ``lead_hours`` = 12, per
    ``packages/common/state_manager.py`` key geometry) and a
    current-generation one elsewhere (00Z) so
    ``exists_current_generation`` is True.  The transition matrix therefore
    takes the current-generation branch, where the wrong-generation
    predecessor guard fires ``block_wrong_generation`` — the core §8
    invariant that the env override must never loosen.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _old_generation_state_entry,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(
        monkeypatch, tmp_path / "db-free-local-root"
    )
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    candidate_checksum = (
        fixture["package_checksum"]
        if _looks_like_hex64(fixture["package_checksum"])
        else "b" * 64
    )
    # Wrong-generation state entry sitting AT the expected predecessor key:
    # valid_time == candidate cycle (12Z), cycle_id == cycle - 12h,
    # lead_hours == 12.
    wrong_gen_entry = _old_generation_state_entry(
        roots,
        state_id="state_wrong_gen_at_expected_key",
        valid_time="2026-05-21T12:00:00Z",
        cycle_id="gfs_2026052100",
        lead_hours=12,
    )
    # Current-generation history elsewhere so the transition matrix reaches
    # the current-generation branch instead of the cutover-boundary one.
    current_gen_entry = _old_generation_state_entry(
        roots,
        old_package_checksum=candidate_checksum,
        state_id="state_current_gen_history",
        valid_time="2026-05-21T00:00:00Z",
        cycle_id="gfs_2026052012",
        lead_hours=12,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=candidate_checksum,
        generated_at=generated_at,
        entries=[wrong_gen_entry, current_gen_entry],
    )
    # Valid, in-window declaration (effective 00Z, not the candidate's 12Z so
    # a mis-seeded index cannot admit via cold_declared_cutover).
    declaration_path = tmp_path / "cutover-declaration.json"
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": generation.derive_generation(candidate_checksum),
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": candidate_checksum,
                        "effective_cycle_utc": "2026-05-21T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        generation.CUTOVER_DECLARATION_ENV, str(declaration_path)
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": candidate_checksum,
        },
    }
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at, allowed_cycle_hours_utc=(0, 12)
        ),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "wrong-generation cutover must not build orchestrator"
        ),
    )
    candidates, blocked, _skipped, _dup, _slurm = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    assert candidates == []
    assert len(blocked) == 1
    # R1-A4 invariant: single-value pin, no OR-set. See
    # .workplans/1081/review/review-failure-retro-round3.md.
    # #1109: the promised state-lineage branch — a wrong-generation entry at
    # the expected predecessor key must block, env override notwithstanding.
    assert blocked[0].reason == "state_snapshot_index_generation_mismatch"
    transition = blocked[0].state_evidence.get("registry_cutover_transition", {})
    assert (
        transition.get("decision")
        == generation.TransitionDecision.BLOCK_WRONG_GENERATION
    )
    # Pin the current-generation branch (not the cutover-boundary one) so a
    # fixture drift back to the declaration-bound path is visible.
    assert (
        transition.get("declaration", {}).get("window_direction")
        == "current_generation_history"
    )


def _looks_like_hex64(value: str) -> bool:
    """Return True when ``value`` is a 64-char lowercase hex string.

    Used by the T15 fixture to decide whether the model's real package
    checksum can be reused verbatim in a schema-valid declaration entry.
    Test fixtures often use non-hex tokens (e.g. ``package-model-a``);
    in that case the test declaration falls back to a stable stub hex so
    the schema validator accepts the payload.
    """
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


# ---------------------------------------------------------------------------
# T16 (R2-A4): schema-validator rejection matrix — window boundaries,
# oversize entries, case-sensitive checksums, generation pattern violations.
# ---------------------------------------------------------------------------


def test_load_declaration_rejects_past_effective_cycle_beyond_tolerance(
    tmp_path: Path,
) -> None:
    """R2-A4: effective_cycle 25h in the past → out-of-window rejection."""
    # NOW = 2026-07-06T18Z; 25h before is 2026-07-05T17Z, but effective cycle
    # must land on 00/12 — pick 2026-07-05T00Z (42h before) so it fails the
    # 24h past tolerance.  Use a NOW that's after the window closes.
    reference_now = _dt("2026-07-06T18:00:00Z")
    past_effective = "2026-07-05T00:00:00Z"  # ~42h before → out of window
    path = _write_declaration(tmp_path, effective_cycle_utc=past_effective)
    payload = generation.load_cutover_declaration(str(path), now=reference_now)
    assert payload is not None
    assert (
        payload.get("_load_error")
        == "declaration_entry_effective_cycle_out_of_window"
    )


def test_load_declaration_rejects_future_effective_cycle_beyond_tolerance(
    tmp_path: Path,
) -> None:
    """R2-A4: effective_cycle 169h in the future → out-of-window rejection."""
    reference_now = _dt("2026-07-06T00:00:00Z")
    future_effective = "2026-07-13T12:00:00Z"  # 180h forward → out of window
    path = _write_declaration(tmp_path, effective_cycle_utc=future_effective)
    payload = generation.load_cutover_declaration(str(path), now=reference_now)
    assert payload is not None
    assert (
        payload.get("_load_error")
        == "declaration_entry_effective_cycle_out_of_window"
    )


def test_load_declaration_rejects_oversize_entries(tmp_path: Path) -> None:
    """R2-A4: declaration with 257 entries → schema maxItems=256 rejection."""
    entries = [
        {
            "model_id": f"model_{i:04d}",
            "old_checksum": OLD_CHECKSUM,
            "new_checksum": NEW_CHECKSUM,
            "effective_cycle_utc": "2026-07-06T12:00:00Z",
            "transition_mode": "replace",
        }
        for i in range(257)
    ]
    path = tmp_path / "cutover-oversize.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": "2026-07-06T00:00:00Z",
                "generation": NEW_GENERATION,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


def test_load_declaration_rejects_uppercase_hex_checksum(tmp_path: Path) -> None:
    """R2-A4: schema pattern ``^[0-9a-f]{64}$`` is case-sensitive — an
    all-uppercase 64-hex string must be rejected."""
    path = _write_declaration(tmp_path, new_checksum="A" * 64)
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


def test_load_declaration_rejects_generation_pattern_violation(tmp_path: Path) -> None:
    """R2-A4: ``generation`` pattern ``^[A-Za-z0-9_.:-]+$`` — a value with
    an illegal character (`!`) must be rejected as wrong_schema."""
    path = _write_declaration(tmp_path, generation_field="bad!token")
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


def test_load_declaration_rejects_malformed_generated_at_via_format_checker(
    tmp_path: Path,
) -> None:
    """R2-B6: with the FormatChecker now attached, an unparseable
    ``generated_at`` must fail the validator (previously symbolic-only)."""
    path = tmp_path / "cutover-bad-generated-at.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": "not-a-date",
                "generation": NEW_GENERATION,
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": OLD_CHECKSUM,
                        "new_checksum": NEW_CHECKSUM,
                        "effective_cycle_utc": "2026-07-06T12:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    payload = generation.load_cutover_declaration(str(path), now=NOW)
    assert payload is not None
    assert payload.get("_load_error") == "declaration_wrong_schema"


# ---------------------------------------------------------------------------
# T17 (R2-A5): STALE-branch coverage + D8.9 preflight fallthrough +
# candidate_pipeline_already_complete fail-CLOSED probe.
# ---------------------------------------------------------------------------


def test_transition_blocks_old_checksum_mismatch_as_stale(tmp_path: Path) -> None:
    """R2-A5: declaration binds NEW checksum + generation but the OLD
    checkpoint's checksum does not match declaration.old_checksum →
    BLOCK_DECLARATION_STALE with stale_reason=old_checksum_mismatch."""
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T00:00:00Z")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T00:00:00Z"),
        required_lead_hours=12,
        # latest_any_checksum diverges from declaration's old_checksum → stale.
        history=_signal(
            exists_any=True,
            exists_current=False,
            latest_any_checksum=_hex("c"),  # not OLD_CHECKSUM
        ),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    assert (
        evaluation.declaration_evidence.get("stale_reason") == "old_checksum_mismatch"
    )


# ---------------------------------------------------------------------------
# #1775 D5 regression: branch (d)'s old-checksum comparison reads
# ``latest_any_generation_checkpoint``, which D5 now scopes to
# ``valid_time <= cutoff``.  The tests above drive that comparison from a
# hand-built ``_HistorySignal``, so they are blind to the scoping.  These two
# drive it from a REAL state index through the real
# ``generation_scoped_history_signal``, which is the only way the scope shows up.
# ---------------------------------------------------------------------------


def _history_signal_from_index(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    *,
    cutoff: str,
    current_package_checksum: str,
    expected_predecessor_cycle_id: str,
    required_lead_hours: int = 12,
) -> tuple[generation._HistorySignal, dict[str, Any]]:
    """Real index -> real signal -> ``_HistorySignal``, wired as the gate wires it.

    Mirrors ``scheduler_generation_gate.py:385-400`` field for field; the point
    of the exercise is that the evidence comes from
    ``FileStateSnapshotIndexRepository`` rather than from ``_signal()``.
    """
    from tests.test_state_manager_generation_history import (
        FileStateSnapshotIndexRepository,
        _publish_entries,
    )

    index_path = _publish_entries(tmp_path, entries, generated_at="2026-07-06T18:00:00Z")
    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T18:00:00Z"),
    )
    evidence = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt(cutoff),
        current_package_checksum=current_package_checksum,
        expected_predecessor_cycle_id=expected_predecessor_cycle_id,
        expected_predecessor_lead_hours=required_lead_hours,
    )
    assert evidence["ready"] is True
    signal = generation._HistorySignal(
        exists_current_generation=bool(evidence.get("history_exists_current_generation")),
        exists_any_generation=bool(evidence.get("history_exists_any_generation")),
        latest_current_generation_checkpoint=evidence.get(
            "latest_current_generation_checkpoint"
        ),
        latest_any_generation_checkpoint=evidence.get("latest_any_generation_checkpoint"),
        wrong_generation_predecessor_present=bool(
            evidence.get("wrong_generation_predecessor_present")
        ),
        wrong_generation_predecessor_checksum=str(
            evidence.get("wrong_generation_predecessor_checksum") or ""
        ),
    )
    return signal, evidence


@pytest.mark.parametrize(
    ("post_cutoff_checksum", "post_cutoff_id"),
    [
        # A THIRD generation published after the cutover cycle (the model was
        # repackaged again later).  Pre-D5 this row was the ``latest_any``
        # sample, so branch (d) compared the declaration's ``old_checksum``
        # against a checksum from the FUTURE -> BLOCK_DECLARATION_STALE /
        # ``old_checksum_mismatch``: a later cycle's output answering a question
        # about an earlier candidate, the same circular evidence D5 removes.
        pytest.param(_hex("c"), "state_third_generation_later", id="third_generation_after_cutoff"),
        # The ordinary shape: the cutover ran and later cycles wrote
        # NEW-generation checkpoints.  Pre-D5 those counted as
        # current-generation history and pushed the candidate out of branch (d)
        # into branch (e) -> block_predecessor_pending.
        pytest.param(NEW_CHECKSUM, "state_new_generation_later", id="new_generation_after_cutoff"),
    ],
)
def test_declaration_binds_when_only_post_cutoff_history_would_contradict_it(
    tmp_path: Path,
    post_cutoff_checksum: str,
    post_cutoff_id: str,
) -> None:
    """#1775 D5 at branch (d): the old-checksum comparison is as-of candidate time.

    Geometry: a cutover-declared model at the cutover cycle itself
    (``candidate_cycle_time_utc == effective_cycle_utc``), with the state index
    carrying BOTH an OLD-generation entry at ``valid_time`` before the cutoff
    AND a later entry after it.  Only the at-or-before entry may drive
    ``latest_any_generation_checkpoint``; the declaration therefore binds and
    the candidate cold-starts on the declared cutover, instead of being blocked
    by an entry that did not exist when the candidate's cycle came due.

    Both rows are pinned because the two post-cutoff checksums fail
    DIFFERENTLY without D5's scope — see the parametrization comments.
    """
    from tests.test_state_manager_generation_history import _entry

    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True, exist_ok=True)
    old_generation_entry = _entry(
        state_id="state_old_generation_before_cutoff",
        valid_time="2026-07-06T00:00:00Z",
        cycle_id="gfs_2026070512",
        lead_hours=12,
        checksum_seed=b"old1",
        package_checksum=OLD_CHECKSUM,
        object_root=object_root,
    )
    post_cutoff_entry = _entry(
        state_id=post_cutoff_id,
        valid_time="2026-07-07T00:00:00Z",
        cycle_id="gfs_2026070612",
        lead_hours=12,
        checksum_seed=b"post1",
        package_checksum=post_cutoff_checksum,
        object_root=object_root,
    )

    signal, evidence = _history_signal_from_index(
        tmp_path,
        [old_generation_entry, post_cutoff_entry],
        cutoff="2026-07-06T12:00:00Z",
        current_package_checksum=NEW_CHECKSUM,
        expected_predecessor_cycle_id="gfs_2026070600",
    )

    # The value that drives branch (d)'s ``old_checksum`` comparison is the
    # at-or-before-cutoff entry, never the later one.  This assertion alone is
    # M4-sensitive; the decision below pins the consequence.
    assert signal.exists_any_generation is True
    assert signal.exists_current_generation is False
    assert evidence["latest_any_generation_checkpoint"]["state_id"] == (
        "state_old_generation_before_cutoff"
    )
    assert evidence["latest_any_generation_checkpoint"]["model_package_checksum"] == OLD_CHECKSUM

    declaration = generation.load_cutover_declaration(
        str(_write_declaration(tmp_path, effective_cycle_utc="2026-07-06T12:00:00Z")),
        now=NOW,
    )
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=signal,
        declaration=declaration,
    )

    assert evaluation.decision == generation.TransitionDecision.COLD_DECLARED_CUTOVER
    assert evaluation.cold_start_reason == "declared_cutover_at_effective_cycle"
    assert evaluation.declaration_evidence.get("stale_reason") is None


def test_transition_blocks_missing_candidate_checksum_with_declaration_as_stale(
    tmp_path: Path,
) -> None:
    """R2-A5: candidate's package_checksum is missing but a declaration is
    configured → BLOCK_DECLARATION_STALE with hint candidate_package_checksum_missing."""
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=None,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_DECLARATION_STALE
    assert (
        evaluation.declaration_evidence.get("block_hint")
        == "candidate_package_checksum_missing"
    )


def test_candidate_pipeline_already_complete_fails_closed_on_read_errors() -> None:
    """R2-A5: the D8.9 preflight probe must fail-CLOSED on filesystem /
    permission / OS-family errors so §8 gating still runs — a False return
    guarantees the compat-mode terminal-skip short-circuits."""
    from services.orchestrator import scheduler_generation_gate as _generation_gate

    class _Repo:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def has_completed_pipeline(self, **kwargs: Any) -> bool:
            raise self.error

    class _Scheduler:
        def __init__(self, error: Exception) -> None:
            self.active_repository = _Repo(error)

    class _Candidate:
        source_id = "gfs"
        cycle_time_utc = _dt("2026-07-06T12:00:00Z")
        model_id = "model_a"

    for error_cls in (FileNotFoundError, PermissionError, OSError):
        scheduler = _Scheduler(error_cls("boom"))
        assert (
            _generation_gate.candidate_pipeline_already_complete(
                scheduler,  # type: ignore[arg-type]
                _Candidate(),  # type: ignore[arg-type]
            )
            is False
        )


def test_d89_preflight_returns_none_preserves_pre_section8_evidence_shape(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """R2-A5: when the state-index history signal is not ready (evaluate
    returns None), the caller's evidence path uses the LEGACY strict warm
    start shape rather than the §8 shape.  The legacy branch must not
    carry a ``registry_cutover_transition`` field at the top level."""
    from services.orchestrator import scheduler_generation_gate as _generation_gate

    class _NotReadyProvider:
        def strict_warm_start_evidence(self, **kwargs: Any) -> dict[str, Any]:
            # Legacy shape — no ``registry_cutover_transition``.
            return {
                "ready": False,
                "status": "blocked",
                "reason": "state_snapshot_index_exact_checkpoint_missing",
            }

        def generation_scoped_history_signal(self, **kwargs: Any) -> dict[str, Any]:
            return {"ready": False}

        def usable_state_history_evidence(self, **kwargs: Any) -> dict[str, Any]:
            return {"ready": False}

    provider = _NotReadyProvider()

    class _Config:
        db_free_required = True
        now = _dt("2026-07-06T18:00:00Z")

    class _Scheduler:
        def __init__(self) -> None:
            self.active_repository = None
            self.config = _Config()

        def _db_free_state_index_provider(self) -> Any:
            return provider

        def _db_free_strict_warm_start_required_for(self, _candidate: Any) -> bool:
            return True

        def _required_warm_start_lead_hours(
            self, _candidate: Any, _cycle: Any
        ) -> int:
            return 12

    class _Candidate:
        candidate_id = "cand_gfs_2026070612_model_a"
        source_id = "gfs"
        cycle_id = "gfs_2026070612"
        cycle_time_utc = _dt("2026-07-06T12:00:00Z")
        model_id = "model_a"
        model_package_uri = "s3://nhms/models/model_a/package/"
        resource_profile: dict[str, Any] = {}

    class _Cycle:
        pass

    # No declaration configured (env unset) — signal not ready →
    # legacy_strict_warm_start_evidence path.
    monkeypatch.delenv(generation.CUTOVER_DECLARATION_ENV, raising=False)
    scheduler = _Scheduler()
    scheduler._cutover_declaration_cache = _generation_gate.CUTOVER_DECLARATION_UNLOADED
    evidence = _generation_gate.strict_warm_start_evidence(
        scheduler,  # type: ignore[arg-type]
        _Candidate(),  # type: ignore[arg-type]
        _Cycle(),  # type: ignore[arg-type]
    )
    assert evidence is not None
    # Pre-§8 legacy shape: no ``registry_cutover_transition`` at the top
    # level — the caller downstream still sees the legacy evidence contract.
    assert "registry_cutover_transition" not in evidence
    # R3-T-4 / F5 (tests-evidence): also assert the pre-§8 fields ARE
    # present with expected values so this test proves BOTH the absence of
    # §8-only fields AND the preservation of the legacy provider payload.
    # ``legacy_strict_warm_start_evidence`` at scheduler_generation_gate.py
    # returns the provider evidence verbatim when
    # ``_db_free_strict_warm_start_required_for`` is True (line 223-224).
    # The _NotReadyProvider above returns exactly three keys — assert them.
    assert evidence["ready"] is False
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "state_snapshot_index_exact_checkpoint_missing"


# ---------------------------------------------------------------------------
# §8.7 / #1107 Wiring A: journal predecessor identity quarantine at the
# ``_build_candidates`` seam.
# ---------------------------------------------------------------------------


def _wrong_suffix_init_state_id() -> str:
    """Recorded id sharing T's base key but naming a 12h-off predecessor."""
    from packages.common.state_manager import state_snapshot_id
    from workers.data_adapters.base import cycle_id_for

    valid_time = _dt("2026-05-21T12:00:00Z")
    return state_snapshot_id(
        "model_a",
        valid_time,
        source_id="gfs",
        cycle_id=cycle_id_for("gfs", _dt("2026-05-21T00:00:00Z")),
        lead_hours=12,
    )


def _expected_init_state_id() -> str:
    """Expected token for T with the 6h cadence lead (independent composition)."""
    from packages.common.state_manager import state_snapshot_id
    from workers.data_adapters.base import cycle_id_for

    valid_time = _dt("2026-05-21T12:00:00Z")
    return state_snapshot_id(
        "model_a",
        valid_time,
        source_id="gfs",
        cycle_id=cycle_id_for("gfs", _dt("2026-05-21T06:00:00Z")),
        lead_hours=6,
    )


def _state_index_entry(
    roots: Any,
    *,
    valid_time: datetime,
    producer_cycle_time: datetime,
    package_checksum: str,
    model_id: str = "model_a",
) -> dict[str, Any]:
    """Compose one state-index entry the same way the DB-free fixtures do."""
    from packages.common.object_store import LocalObjectStore, sha256_bytes
    from workers.data_adapters.base import cycle_id_for, format_cycle_time

    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    content = f"state-fixture-{format_cycle_time(valid_time)}\n".encode()
    state_uri = store.write_bytes_atomic(
        f"states/gfs/{model_id}/{format_cycle_time(valid_time)}/state.cfg.ic",
        content,
    )
    producer_cycle_id = cycle_id_for("gfs", producer_cycle_time)
    lead_hours = int(round((valid_time - producer_cycle_time).total_seconds() / 3600.0))
    return {
        "state_id": (
            f"state_gfs_{model_id}_{format_cycle_time(valid_time)}"
            f"_{producer_cycle_id}_f{lead_hours:03d}"
        ),
        "model_id": model_id,
        "run_id": f"analysis_{producer_cycle_id}_{model_id}",
        "source_id": "gfs",
        "valid_time": valid_time.isoformat().replace("+00:00", "Z"),
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "cycle_id": producer_cycle_id,
        "lead_hours": lead_hours,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": package_checksum,
    }


def _running_forecast_job(cycle_time: datetime) -> dict[str, Any]:
    """A pipeline job that is running but carries no real Slurm binding.

    No ``slurm_job_id``/``array_task_id`` keeps ``_state_active_jobs`` empty,
    so the decision is ``active_duplicate_pipeline`` rather than the
    ``active_slurm_job`` skip that the else-leg excludes outright.
    """
    from workers.data_adapters.base import cycle_id_for, format_cycle_time

    stamp = format_cycle_time(cycle_time)
    return {
        "job_id": f"job_cycle_gfs_{stamp}_forecast",
        "idempotency_key": f"cycle_gfs_{stamp}:forecast",
        "run_id": f"fcst_gfs_{stamp}_model_a",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "stage": "forecast",
        "status": "running",
        "created_at": "2026-05-21T12:00:00Z",
        "submitted_at": "2026-05-21T12:01:00Z",
    }


def _terminal_state_save_qc_job(cycle_time: datetime) -> dict[str, Any]:
    """A candidate-scoped terminal pipeline success for cycle T."""
    from workers.data_adapters.base import cycle_id_for, format_cycle_time

    stamp = format_cycle_time(cycle_time)
    return {
        "job_id": f"job_cycle_gfs_{stamp}_state_save_qc",
        "idempotency_key": f"cycle_gfs_{stamp}:state_save_qc",
        "run_id": f"fcst_gfs_{stamp}_model_a",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "state_save_qc",
        "model_id": "model_a",
        "stage": "state_save_qc",
        "status": "succeeded",
        "created_at": "2026-05-21T12:00:00Z",
        "finished_at": "2026-05-21T12:05:00Z",
    }


#: ``_run_wiring_a_build_candidates`` default: the run manifest mirrors whatever
#: the journal recorded.  Pass an explicit value to drive them apart (E6).
_MANIFEST_MIRRORS_JOURNAL = object()


def _run_wiring_a_build_candidates(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    recorded_init_state_id: str | None,
    hydro_status: str | None = "complete",
    jobs: Sequence[Mapping[str, Any]] | None = None,
    journal_identity_field: str = "init_state_id",
    manifest_init_state_id: Any = _MANIFEST_MIRRORS_JOURNAL,
    repository_factory: Any | None = None,
) -> tuple[list[Any], list[Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Drive ``_build_candidates`` over one journal-recognised completed cycle T.

    ``NHMS_REQUIRE_FORECAST_WARM_START=false`` plus a journal-completed
    pipeline puts the D8.9 compat regime in force, so the strict warm start is
    nulled and the terminal-skip else-leg carrying the §8.7 identity filter is
    the live gate.  The successor checkpoint at T+6h and the run manifest are
    seeded so the pre-existing
    ``strict_warm_start_successor_checkpoint_missing`` /
    ``terminal_run_manifest_missing`` legs cannot fire first.

    Returns ``_build_candidates``' full 5-tuple.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_file_orchestration_journal import _latest_view, _write_json
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery, format_cycle_time

    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[
            _state_index_entry(
                roots,
                valid_time=cycle_time,
                producer_cycle_time=_pdt("2026-05-21T06:00:00Z"),
                package_checksum=fixture["package_checksum"],
            ),
            # Successor checkpoint at T+6h so the pre-existing
            # ``strict_warm_start_successor_checkpoint_missing`` leg cannot
            # fire before the else-leg under test.
            _state_index_entry(
                roots,
                valid_time=generated_at,
                producer_cycle_time=cycle_time,
                package_checksum=fixture["package_checksum"],
            ),
        ],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
        },
    }
    # D8.9 compat regime: env=false + journal-completed -> strict warm start
    # is nulled, so the terminal-skip else-leg is the live gate.
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")

    cycle_segment = format_cycle_time(cycle_time)
    run_id = f"fcst_gfs_{cycle_segment}_model_a"
    journal_root = Path(paths["NHMS_SCHEDULER_JOURNAL_ROOT"])
    latest = _latest_view(
        cycle_time=cycle_time,
        hydro_status=hydro_status,
        jobs=[dict(job) for job in (jobs or [])],
    )
    if hydro_status is not None and recorded_init_state_id is not None:
        latest["hydro_run"][journal_identity_field] = recorded_init_state_id
    _write_json(journal_root / "latest" / "gfs" / cycle_segment / "model_a.json", latest)
    # The run manifest keeps the pre-existing ``terminal_run_manifest_missing``
    # leg from firing first, so the else-leg under test is reached.
    manifest_state_id = (
        recorded_init_state_id
        if manifest_init_state_id is _MANIFEST_MIRRORS_JOURNAL
        else manifest_init_state_id
    )
    run_manifest = Path(roots["object_store_root"]) / "runs" / run_id / "input" / "manifest.json"
    run_manifest.parent.mkdir(parents=True, exist_ok=True)
    run_manifest.write_text(
        json.dumps({"initial_state": {"quality": "fresh", "state_id": manifest_state_id}}),
        encoding="utf-8",
    )

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 6, 12, 18)),
        registry=FakeRegistry([model]),
        adapters={},
        active_repository=(
            repository_factory(journal_root)
            if repository_factory is not None
            else scheduler_module.FileOrchestrationJournalRepository(journal_root)
        ),
        orchestrator_factory=lambda _source_id: pytest.fail(
            "this seam must not build an orchestrator"
        ),
    )
    return scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id=f"gfs_{cycle_segment}",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )


def test_build_candidates_quarantines_stale_journal_predecessor_identity(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.7 Wiring A: a completed journal entry with a same-base-key /
    wrong-suffix ``init_state_id`` must NOT be skipped as a terminal
    duplicate; the skip decision is REPLACED by a typed retry carrying both
    tokens in evidence (env=false, D8.9 preflight nulls strict warm start)."""
    recorded_init_state_id = _wrong_suffix_init_state_id()

    candidates, blocked, skipped, duplicate_exclusions, slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=recorded_init_state_id,
    )

    # Not skipped as a terminal duplicate — that is the whole point.
    assert skipped == []
    assert duplicate_exclusions == []
    assert slurm_sync == []
    # Action pin: the quarantine must ADMIT the candidate, not block it.
    assert blocked == []
    assert len(candidates) == 1
    state_evidence = candidates[0].state_evidence
    assert state_evidence["reason"] == "journal_predecessor_identity_mismatch"
    assert state_evidence["decision"] == "retry_journal_predecessor_identity_mismatch"
    identity = state_evidence["journal_predecessor_identity"]
    assert identity["recorded_init_state_id"] == recorded_init_state_id
    assert identity["expected_init_state_id"] == _expected_init_state_id()
    assert identity["required_lead_hours"] == 6
    assert identity["quarantined_skip_reason"] == "terminal_hydro_success"


@pytest.mark.parametrize(
    "leg",
    ["matching", "suffix_less_legacy", "earlier_valid_time_fallback"],
)
def test_build_candidates_keeps_terminal_skip_for_no_judgement_journal_identity(
    monkeypatch: Any,
    tmp_path: Path,
    leg: str,
) -> None:
    """Control + no-judgement pins for Wiring A: the matching token and every
    non-mismatch shape keep the pre-#1107 terminal skip exactly as before."""
    if leg == "matching":
        recorded_init_state_id = _expected_init_state_id()
    elif leg == "suffix_less_legacy":
        # Suffix-less legacy id equal to T's expected base prefix.
        recorded_init_state_id = "state_gfs_model_a_2026052112"
        assert _expected_init_state_id().startswith(f"{recorded_init_state_id}_")
    else:
        # DIFFERENT base key: an earlier-valid_time fallback warm start, the
        # legal selection under NHMS_REQUIRE_FORECAST_WARM_START=false.
        recorded_init_state_id = _write_side_init_state_id(
            source_id="gfs",
            model_id="model_a",
            valid_time=_dt("2026-05-21T06:00:00Z"),
            lead_hours=6,
        )
        assert not recorded_init_state_id.startswith("state_gfs_model_a_2026052112")

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=recorded_init_state_id,
    )

    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "terminal_hydro_success"


def test_build_candidates_never_quarantines_active_duplicate_pipeline_skip(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Whitelist pin: an ACTIVE skip is never quarantined, even when the
    recorded id is a positive same-base-key mismatch — resubmitting over a
    running pipeline is exactly what the whitelist exists to prevent."""
    from tests.test_production_scheduler import _dt as _pdt

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=_wrong_suffix_init_state_id(),
        jobs=[_running_forecast_job(_pdt("2026-05-21T12:00:00Z"))],
    )

    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "active_duplicate_pipeline"


def test_build_candidates_declines_judgement_on_superseded_hydro_placeholder(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """The judged identity must be the COMPLETED run's row: a
    ``created`` placeholder superseded by a terminal pipeline job also carries
    an ``init_state_id``, but it does not describe the completing run, so a
    stale value there declines judgement instead of quarantining."""
    from tests.test_production_scheduler import _dt as _pdt

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=_wrong_suffix_init_state_id(),
        hydro_status="created",
        jobs=[_terminal_state_save_qc_job(_pdt("2026-05-21T12:00:00Z"))],
    )

    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "terminal_pipeline_success"


def _wiring_a_journal_identity(cycle_time: datetime, *, model_id: str = "model_a") -> str | None:
    """Wiring B's verdict on the journal ``_run_wiring_a_build_candidates`` just wrote."""
    import os

    from services.orchestrator import scheduler as scheduler_module

    repository = scheduler_module.FileOrchestrationJournalRepository(
        Path(os.environ["NHMS_SCHEDULER_JOURNAL_ROOT"])
    )
    return repository.completed_pipeline_init_state_id(
        source_id="gfs", cycle_time=cycle_time, model_id=model_id
    )


def test_build_candidates_declines_judgement_on_run_manifest_backfilled_identity(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """E6 (#1157 D1): journal has no id, the run manifest has a stale one.

    ``chain_repository_state`` backfills the manifest's ``state_id`` onto the
    candidate-state ``hydro_run`` row, so reading that row made Wiring A judge
    an identity the JOURNAL never recorded — while the discovery-side accessor
    declined.  Both wirings must now agree on no judgement.
    """
    from tests.test_production_scheduler import _dt as _pdt

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=None,
        manifest_init_state_id=_wrong_suffix_init_state_id(),
    )

    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "terminal_hydro_success"
    # Wiring B agrees: the journal recorded nothing to judge.
    assert _wiring_a_journal_identity(_pdt("2026-05-21T12:00:00Z")) is None


def test_build_candidates_declines_judgement_on_bare_state_id_alias(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """E6 (#1157 D1): a wrong-suffix id recorded ONLY under the bare ``state_id``.

    The shared alias table ``_INIT_STATE_FIELD_ALIASES`` still resolves that
    key for strict warm-start comparison (unchanged by this issue), but the
    journal accessor's alias set is ``init_state_id``/``initial_state_id``
    only — so switching Wiring A onto the accessor makes both wirings decline.
    """
    from tests.test_production_scheduler import _dt as _pdt

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=_wrong_suffix_init_state_id(),
        journal_identity_field="state_id",
        manifest_init_state_id=None,
    )

    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "terminal_hydro_success"
    assert _wiring_a_journal_identity(_pdt("2026-05-21T12:00:00Z")) is None


def test_shared_init_state_alias_table_is_unchanged() -> None:
    """"Must preserve": alias convergence came from swapping the ACCESSOR.

    Narrowing the shared table instead would silently change strict warm-start
    matching for legacy bare-``state_id`` rows, which is out of scope here.
    """
    from services.orchestrator.scheduler_init_state_match import _INIT_STATE_FIELD_ALIASES

    assert _INIT_STATE_FIELD_ALIASES["state_id"] == (
        "init_state_id",
        "initial_state_id",
        "state_id",
    )


# ---------------------------------------------------------------------------
# §8.7 breaker (#1157): the candidate-side demotion from retry to blocked.
# ---------------------------------------------------------------------------


def _cohort_master_recording(
    cycle_time: datetime,
    init_state_id: str,
    *,
    job_suffix: str = "",
    quarantine_rerun_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """A terminal-success forecast cohort master that recorded ``init_state_id``.

    ``quarantine_rerun_model_ids=None`` omits the provenance field entirely —
    the shape of an unrelated whitelisted replacement, and of every journal
    written before #1157.
    """
    from workers.data_adapters.base import cycle_id_for, format_cycle_time

    run_id = f"cycle_gfs_{format_cycle_time(cycle_time)}"
    row = {
        "job_id": f"job_{run_id}_forecast{job_suffix}",
        "run_id": run_id,
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "candidate_id": run_id,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": "succeeded",
        "model_id": None,
        "init_state_identities": [
            {"array_task_id": 0, "model_id": "model_a", "init_state_id": init_state_id}
        ],
    }
    if quarantine_rerun_model_ids is not None:
        row["journal_predecessor_quarantine_rerun_model_ids"] = list(quarantine_rerun_model_ids)
    return row


def test_build_candidates_breaker_demotes_quarantine_after_a_stamped_rerun(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """E3 (R1): a provenance-stamped rerun that re-recorded the token fail-stops.

    The demoted decision must land the candidate in ``blocked`` — not in the
    submission set — carrying both tokens, the occurrence count and a
    manual-retry-required policy, so an operator can see why the loop stopped.
    """
    from tests.test_production_scheduler import _dt as _pdt

    cycle_time = _pdt("2026-05-21T12:00:00Z")
    recorded_init_state_id = _wrong_suffix_init_state_id()

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=recorded_init_state_id,
        jobs=[
            # The original defect run: no provenance, and none needed — the
            # current positive mismatch is its witness.
            _cohort_master_recording(cycle_time, recorded_init_state_id),
            # The quarantine rerun that came back with the same stale lineage.
            _cohort_master_recording(
                cycle_time,
                recorded_init_state_id,
                job_suffix="_retry_1",
                quarantine_rerun_model_ids=["model_a"],
            ),
        ],
    )

    assert skipped == []
    assert candidates == []
    assert len(blocked) == 1
    assert blocked[0].reason == "journal_predecessor_identity_quarantine_breaker_engaged"
    evidence = blocked[0].state_evidence
    assert evidence["decision"] == "blocked_journal_predecessor_identity_quarantine"
    assert evidence["reason"] == "journal_predecessor_identity_quarantine_breaker_engaged"
    identity = evidence["journal_predecessor_identity"]
    assert identity["recorded_init_state_id"] == recorded_init_state_id
    assert identity["expected_init_state_id"] == _expected_init_state_id()
    assert identity["required_lead_hours"] == 6
    assert identity["quarantined_skip_reason"] == "terminal_hydro_success"
    assert identity["occurrences"] == 1
    assert evidence["retry_policy"] == {
        "automatic_retry_allowed": False,
        "manual_retry_required": True,
        "occurrences": 1,
        "occurrence_threshold": 1,
    }


@pytest.mark.parametrize(
    "leg",
    ["no_masters", "unstamped_replacements", "legacy_rows", "other_models_stamp"],
)
def test_build_candidates_keeps_quarantine_retry_without_stamped_rerun(
    monkeypatch: Any,
    tmp_path: Path,
    leg: str,
) -> None:
    """E3 (R1) pre-arming pins: only a PROVEN failed rerun may fail-stop.

    ``unstamped_replacements`` is the Class-B defect itself: an unrelated
    whitelisted resubmit (missing run manifest, or a missing-forecast-output
    recompute after a Slurm failure) mints a second same-token master.  Under
    a bare "two masters" count that pre-armed the breaker, so the very FIRST
    quarantine judgement fail-stopped and the convergence layer never ran.
    ``legacy_rows`` is the same shape as every journal written before #1157.
    """
    from tests.test_production_scheduler import _dt as _pdt

    cycle_time = _pdt("2026-05-21T12:00:00Z")
    recorded_init_state_id = _wrong_suffix_init_state_id()
    if leg == "no_masters":
        jobs: list[dict[str, Any]] = []
    elif leg == "other_models_stamp":
        jobs = [
            _cohort_master_recording(
                cycle_time,
                recorded_init_state_id,
                quarantine_rerun_model_ids=["model_b"],
            )
        ]
    else:
        # Two same-token masters, neither stamped for this model.
        jobs = [
            _cohort_master_recording(cycle_time, recorded_init_state_id),
            _cohort_master_recording(
                cycle_time, recorded_init_state_id, job_suffix="_retry_1"
            ),
        ]

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=recorded_init_state_id,
        jobs=jobs,
    )

    assert skipped == []
    assert blocked == []
    assert len(candidates) == 1
    assert candidates[0].state_evidence["decision"] == (
        "retry_journal_predecessor_identity_mismatch"
    )


def test_build_candidates_keeps_quarantine_retry_without_occurrence_accessor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """E3 fail-toward-liveness: no occurrence accessor -> breaker disengaged.

    A repository that cannot answer "how many times" must never be read as
    "zero times is fine to fail-stop on".  One more rerun costs a cycle; a
    wrong fail-stop costs operator time on a cycle that was still converging.
    """
    from services.orchestrator import scheduler as scheduler_module
    from tests.test_production_scheduler import _dt as _pdt

    cycle_time = _pdt("2026-05-21T12:00:00Z")
    recorded_init_state_id = _wrong_suffix_init_state_id()

    class NoOccurrenceAccessorRepository(scheduler_module.FileOrchestrationJournalRepository):
        completed_pipeline_init_state_id_occurrences = None

    candidates, blocked, skipped, _duplicate_exclusions, _slurm_sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=recorded_init_state_id,
        jobs=[
            _cohort_master_recording(
                cycle_time,
                recorded_init_state_id,
                quarantine_rerun_model_ids=["model_a"],
            )
        ],
        repository_factory=NoOccurrenceAccessorRepository,
    )

    assert skipped == []
    assert blocked == []
    assert len(candidates) == 1
    assert candidates[0].state_evidence["decision"] == (
        "retry_journal_predecessor_identity_mismatch"
    )


def test_build_candidates_quarantines_terminal_completed_cycle_skip(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """E7: the ``terminal_completed_cycle`` skip reason reaches the filter.

    It is the only completed-type skip reachable under
    ``NHMS_REQUIRE_FORECAST_WARM_START=true`` that never had an identity gate,
    so its shape is exercised directly at the decision seam: same evidence
    contract as the ``terminal_hydro_success`` shape, only the quarantined
    reason differs.
    """
    from types import SimpleNamespace

    from services.orchestrator import scheduler_candidates as scheduler_candidates_module
    from services.orchestrator.scheduler_state import CandidateStateDecision
    from tests.test_production_scheduler import _dt as _pdt

    cycle_time = _pdt("2026-05-21T12:00:00Z")
    recorded_init_state_id = _wrong_suffix_init_state_id()
    candidate = SimpleNamespace(
        source_id="gfs",
        model_id="model_a",
        cycle_time_utc=cycle_time,
    )
    repository = SimpleNamespace(
        completed_pipeline_init_state_id=(
            lambda *, source_id, cycle_time, model_id: recorded_init_state_id
        ),
        # No stamped quarantine rerun has completed yet, so the breaker is
        # disengaged and this shape must still produce the retry.
        completed_pipeline_init_state_id_occurrences=(
            lambda *, source_id, cycle_time, model_id, init_state_id: 0
        ),
    )
    context = SimpleNamespace(
        active_repository=repository,
        required_lead_hours_for_candidate=lambda _candidate, _cycle: 6,
    )

    decision = scheduler_candidates_module._journal_predecessor_identity_quarantine(
        context,
        candidate,
        SimpleNamespace(),
        CandidateStateDecision(
            "skip",
            "terminal_completed_cycle",
            {"decision": "skip_terminal", "terminal_source": "forecast_cycle"},
        ),
    )

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "journal_predecessor_identity_mismatch"
    assert decision.evidence["decision"] == "retry_journal_predecessor_identity_mismatch"
    identity = decision.evidence["journal_predecessor_identity"]
    assert identity["recorded_init_state_id"] == recorded_init_state_id
    assert identity["expected_init_state_id"] == _expected_init_state_id()
    assert identity["required_lead_hours"] == 6
    assert identity["quarantined_skip_reason"] == "terminal_completed_cycle"


def test_journal_identity_quarantine_breaker_helper_is_total() -> None:
    """The injected-accessor breaker predicate never raises and never over-counts."""
    from types import SimpleNamespace

    from tests.test_production_scheduler import _dt as _pdt

    count = generation.journal_identity_quarantine_occurrence_count
    engaged = generation.journal_identity_quarantine_breaker_engaged
    kwargs = {
        "source_id": "gfs",
        "cycle_time": _pdt("2026-05-21T12:00:00Z"),
        "model_id": "model_a",
        "recorded_init_state_id": _wrong_suffix_init_state_id(),
    }

    def repository_returning(value: Any) -> Any:
        return SimpleNamespace(
            completed_pipeline_init_state_id_occurrences=(
                lambda *, source_id, cycle_time, model_id, init_state_id: value
            )
        )

    def raising(*, source_id: str, cycle_time: Any, model_id: str, init_state_id: str) -> int:
        raise RuntimeError("journal read exploded")

    assert count(None, **kwargs) == 0
    assert count(SimpleNamespace(), **kwargs) == 0
    assert count(
        SimpleNamespace(completed_pipeline_init_state_id_occurrences=raising), **kwargs
    ) == 0
    assert count(repository_returning("not-a-number"), **kwargs) == 0
    assert count(repository_returning(-3), **kwargs) == 0
    assert count(repository_returning(2), **kwargs) == 2

    # R1: the counted rows are provenance-stamped reruns, so ONE proven failed
    # convergence attempt is the fail-stop trigger.
    assert engaged(0) is False
    assert engaged(1) is True
    assert engaged(7) is True
    assert engaged(None) is False
    assert engaged("two") is False


# ---------------------------------------------------------------------------
# §8.7 / #1107 helper: three-valued, total contract.
# ---------------------------------------------------------------------------


def _write_side_init_state_id(
    *,
    source_id: str,
    model_id: str,
    valid_time: datetime,
    lead_hours: int,
) -> str:
    """Compose an id exactly like the write side (``packages.common.state_cli``).

    ``state_cli._state_cycle_id`` -> ``cycle_id_for(run.source_id, cycle_time)``
    and ``state_manager.save_state_snapshot`` -> ``state_snapshot_id(...,
    source_id=run.source_id, ...)``.  Reproducing that call shape here is the
    independent oracle for the scheduler-side expectation.
    """
    from packages.common.state_manager import state_snapshot_id
    from workers.data_adapters.base import cycle_id_for

    return state_snapshot_id(
        model_id,
        valid_time,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, valid_time - timedelta(hours=lead_hours)),
        lead_hours=lead_hours,
    )


def test_journal_init_state_lineage_helper_is_three_valued_and_total() -> None:
    valid_time = _dt("2026-05-21T12:00:00Z")
    judge = generation.journal_init_state_lineage_matches_expected
    kwargs: dict[str, Any] = {
        "source_id": "gfs",
        "model_id": "model_a",
        "candidate_valid_time": valid_time,
        "required_lead_hours": 6,
    }
    expected = _write_side_init_state_id(
        source_id="gfs", model_id="model_a", valid_time=valid_time, lead_hours=6
    )
    assert expected == "state_gfs_model_a_2026052112_gfs_2026052106_f006"

    # True: exact expected token.
    assert judge(expected, **kwargs) is True
    # False: same base key, different lineage suffix (wrong predecessor cycle).
    assert judge("state_gfs_model_a_2026052112_gfs_2026052100_f012", **kwargs) is False
    # None: missing / empty / suffix-less legacy / different base key.
    assert judge(None, **kwargs) is None
    assert judge("", **kwargs) is None
    assert judge("   ", **kwargs) is None
    assert judge("state_gfs_model_a_2026052112", **kwargs) is None
    assert judge("state_gfs_model_a_2026052106_gfs_2026052100_f006", **kwargs) is None
    assert judge("state_gfs_model_b_2026052112_gfs_2026052100_f012", **kwargs) is None
    assert judge("not-a-state-token", **kwargs) is None
    # Total: unusable inputs never raise.
    assert judge(expected, **{**kwargs, "source_id": "unknown-source"}) is None
    assert judge(expected, **{**kwargs, "model_id": "../escape"}) is None
    assert judge(expected, **{**kwargs, "required_lead_hours": "twelve"}) is None
    assert judge(expected, **{**kwargs, "candidate_valid_time": "2026-05-21T12:00:00Z"}) is None


def test_journal_init_state_lineage_helper_matches_write_side_source_casing() -> None:
    """Implementation-phase casing check.

    ``state_snapshot_id`` embeds ``source_id`` VERBATIM while ``cycle_id_for``
    lowercases it through ``normalize_source_id``.  The helper must reproduce
    that exact asymmetry, and a casing skew between the recorded id and the
    candidate's ``source_id`` must degrade to NO JUDGEMENT — never to a false
    quarantine.
    """
    valid_time = _dt("2026-05-21T12:00:00Z")
    judge = generation.journal_init_state_lineage_matches_expected

    era5_recorded = _write_side_init_state_id(
        source_id="ERA5", model_id="model_a", valid_time=valid_time, lead_hours=6
    )
    # Verbatim source part, lowercased cycle_id part — pinned so a future
    # normalization change breaks loudly instead of silently quarantining.
    assert era5_recorded == "state_ERA5_model_a_2026052112_era5_2026052106_f006"
    assert (
        judge(
            era5_recorded,
            source_id="ERA5",
            model_id="model_a",
            candidate_valid_time=valid_time,
            required_lead_hours=6,
        )
        is True
    )
    # Same source, different case on the candidate side -> different base key
    # -> no judgement (never a positive mismatch).
    assert (
        judge(
            era5_recorded,
            source_id="era5",
            model_id="model_a",
            candidate_valid_time=valid_time,
            required_lead_hours=6,
        )
        is None
    )


# ---------------------------------------------------------------------------
# T14 (#1164): first-cycle packaged-IC decision table (design D1/D2).
#
# The pure evaluator gains ONE optional qualification-signal parameter.  Its
# default (``None``) is what keeps every pre-#1164 fixture — including the
# 13/6 histogram above — on the legacy ``cold_new_model`` path with zero
# rebaselining, so each row below states which signal it injects.
# ---------------------------------------------------------------------------


PACKAGE_IC_SHA256 = _hex("d")
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _package_manifest(
    *,
    ic_sha256: str | None = PACKAGE_IC_SHA256,
    ic_size_bytes: int | None = 131072,
    ic_relative_path: str = "alias-a.cfg.ic",
    include_ic: bool = True,
    included_files: list[dict[str, Any]] | None = None,
    shud_input_name: str | None = None,
    extra_ic_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a Basins package manifest shaped like ``basins_package.py`` writes."""
    files: list[dict[str, Any]] = [
        {
            "relative_path": "alias-a.sp.mesh",
            "role": "shud_input",
            "size_bytes": 4096,
            "sha256": _hex("e"),
        }
    ]
    # ``basins_package.py`` sorts ``included_files`` by ``(role, relative_path)``,
    # so ``calibration`` (``CALIB/…``) entries land BEFORE the canonical
    # ``runtime_input`` IC — the ordering that made a first-match classifier read
    # the wrong entry.
    files.extend(extra_ic_entries or [])
    if include_ic:
        entry: dict[str, Any] = {"relative_path": ic_relative_path, "role": "shud_input"}
        if ic_size_bytes is not None:
            entry["size_bytes"] = ic_size_bytes
        if ic_sha256 is not None:
            entry["sha256"] = ic_sha256
        files.append(entry)
    manifest: dict[str, Any] = {
        "schema_version": "nhms.basins_package_manifest.v1",
        "model_id": "model_new",
        "version": "vbasins-test",
        "package_checksum": NEW_CHECKSUM,
        "included_files": included_files if included_files is not None else files,
    }
    if shud_input_name is not None:
        manifest["shud_input_name"] = shud_input_name
    return manifest


def _calib_ic_entry(*, sha256: str = _hex("c"), size_bytes: int = 4096) -> dict[str, Any]:
    """A stray calibration-directory ``*.cfg.ic`` entry (never the canonical IC)."""
    return {
        "relative_path": "CALIB/alias-a.cfg.ic",
        "role": "calibration",
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _first_cycle_evaluation(signal: Any) -> Any:
    return generation.evaluate_transition_decision(
        model_id="model_new",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=False, exists_current=False),
        declaration=None,
        packaged_initial_condition=signal,
    )


def test_packaged_ic_manifest_with_nonempty_cfg_ic_qualifies() -> None:
    signal = generation.classify_packaged_initial_condition(_package_manifest())
    assert signal.status == generation.PACKAGED_IC_QUALIFIED
    assert signal.ic_sha256 == PACKAGE_IC_SHA256
    assert signal.ic_relative_path == "alias-a.cfg.ic"
    assert signal.ic_size_bytes == 131072


def test_packaged_ic_manifest_classification_rejects_empty_digest_and_zero_size() -> None:
    # Empty-file digest -> unqualified even though the entry exists.
    empty_digest = generation.classify_packaged_initial_condition(
        _package_manifest(ic_sha256=EMPTY_FILE_SHA256)
    )
    assert empty_digest.status == generation.PACKAGED_IC_UNQUALIFIED
    # Zero size_bytes -> unqualified even with a non-empty-looking digest.
    zero_size = generation.classify_packaged_initial_condition(_package_manifest(ic_size_bytes=0))
    assert zero_size.status == generation.PACKAGED_IC_UNQUALIFIED
    # No ``*.cfg.ic`` entry at all -> unqualified.
    missing_entry = generation.classify_packaged_initial_condition(_package_manifest(include_ic=False))
    assert missing_entry.status == generation.PACKAGED_IC_UNQUALIFIED
    # Not a manifest object at all -> unreadable, never "no IC".
    assert (
        generation.classify_packaged_initial_condition(None).status
        == generation.PACKAGED_IC_UNREADABLE
    )
    assert (
        generation.classify_packaged_initial_condition(["not", "an", "object"]).status
        == generation.PACKAGED_IC_UNREADABLE
    )


def test_packaged_ic_classification_blocks_ambiguous_inventories() -> None:
    """A stray ``CALIB/*.cfg.ic`` must never qualify a package by sort order.

    Gate/runtime symmetry: the runtime searches the staged tree recursively and
    refuses anything but exactly one candidate, so a package whose inventory
    lists two ``*.cfg.ic`` entries is blocked at planning time instead of being
    submitted into a run doomed to ``PACKAGED_IC_CONSUMPTION_FAILED``.  Neither
    the stray digest nor a stray 0-byte placeholder may decide the verdict.
    """
    non_empty_calib = generation.classify_packaged_initial_condition(
        _package_manifest(extra_ic_entries=[_calib_ic_entry()])
    )
    assert non_empty_calib.status == generation.PACKAGED_IC_UNQUALIFIED
    assert non_empty_calib.detail == "packaged_initial_condition_ambiguous"
    # The wrong entry's digest must not leak into the signal the runtime verifies.
    assert non_empty_calib.ic_sha256 == ""

    zero_byte_calib = generation.classify_packaged_initial_condition(
        _package_manifest(extra_ic_entries=[_calib_ic_entry(sha256=EMPTY_FILE_SHA256, size_bytes=0)])
    )
    assert zero_byte_calib.status == generation.PACKAGED_IC_UNQUALIFIED
    assert zero_byte_calib.detail == "packaged_initial_condition_ambiguous"


def test_packaged_ic_classification_requires_the_canonical_entry() -> None:
    """Only the canonical top-level ``<shud_input_name>.cfg.ic`` can qualify."""
    calib_only = generation.classify_packaged_initial_condition(
        _package_manifest(include_ic=False, extra_ic_entries=[_calib_ic_entry()])
    )
    assert calib_only.status == generation.PACKAGED_IC_UNQUALIFIED
    assert calib_only.detail == "packaged_initial_condition_not_canonical"
    assert calib_only.ic_sha256 == ""

    # When the manifest names the SHUD input directory the basename must match it.
    named_mismatch = generation.classify_packaged_initial_condition(
        _package_manifest(shud_input_name="alias-a", ic_relative_path="other.cfg.ic")
    )
    assert named_mismatch.status == generation.PACKAGED_IC_UNQUALIFIED
    assert named_mismatch.detail == "packaged_initial_condition_not_canonical"

    named_match = generation.classify_packaged_initial_condition(
        _package_manifest(shud_input_name="alias-a")
    )
    assert named_match.status == generation.PACKAGED_IC_QUALIFIED
    assert named_match.ic_sha256 == PACKAGE_IC_SHA256
    assert named_match.ic_relative_path == "alias-a.cfg.ic"


# ---------------------------------------------------------------------------
# T14b (#1164 round 2): tier-(b) qualification for the INVENTORY-LESS
# direct-grid variant manifest — the shape 36/36 production registry rows carry.
#
# ``provision_direct_grid_scheduler_registry.py`` publishes
# ``resource_profile.manifest_uri = f"{variant_uri}manifest.json"`` and
# ``model_package_uri = variant_uri`` (WITH a trailing '/'), while the variant
# manifest written by ``workers/mapping_builder`` has exactly ONE top-level key:
# ``direct_grid_forcing``.  There is no ``included_files`` and no
# ``shud_input_name`` in that manifest, so qualification must fall back to the
# canonical object probe or every production first cycle blocks.
# ---------------------------------------------------------------------------


DG_PACKAGE_KEY = "models/direct_grid_variants/basins_dth_ls_shud/dg-gfs-9f1c2b3d4e5a/package"
DG_PACKAGE_URI = f"s3://nhms/{DG_PACKAGE_KEY}/"
DG_MANIFEST_KEY = f"{DG_PACKAGE_KEY}/manifest.json"
DG_SHUD_INPUT_NAME = "dth_ls"
DG_CANONICAL_IC_URI = f"{DG_PACKAGE_URI}{DG_SHUD_INPUT_NAME}.cfg.ic"
DG_IC_CONTENT = b"2\t1\t29626560.000000\n1\t0.1\t0.2\t0.3\t0.4\n"


def _direct_grid_variant_manifest() -> dict[str, Any]:
    """Return a manifest shaped like ``DirectGridManifest.to_resource_profile_dict``."""
    return {
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "binding_uri": f"{DG_PACKAGE_URI}direct_grid_binding.json",
            "binding_checksum": _hex("b"),
            "model_input_package_id": "dg-input-9f1c2b3d4e5a",
            "sp_att_path": f"{DG_SHUD_INPUT_NAME}.sp.att",
            "sp_att_checksum": _hex("a"),
            "applicable_source_ids": ["gfs"],
            "grid_id": "gfs_0p25",
            "grid_signature": _hex("f"),
            "coordinate_reference_system": "EPSG:4326",
            "z_policy": {"mode": "surface"},
            "station_bindings": [],
        }
    }


def _dg_resource_profile(
    *,
    model_package_uri: str | None = DG_PACKAGE_URI,
    shud_input_name: str | None = DG_SHUD_INPUT_NAME,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "package_checksum": NEW_CHECKSUM,
        "manifest_uri": f"s3://nhms/{DG_MANIFEST_KEY}",
        "lineage": "direct_grid_variant_registration",
        "forcing_mapping_mode": "direct_grid",
    }
    if model_package_uri is not None:
        profile["model_package_uri"] = model_package_uri
    if shud_input_name is not None:
        profile["shud_input_name"] = shud_input_name
    return profile


class _RecordingProbe:
    """Deterministic tier-(b) probe stub that records the uri it was handed."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.uris: list[str] = []

    def __call__(self, object_uri: str) -> Any:
        self.uris.append(object_uri)
        return self.result


def test_variant_manifest_qualifies_from_the_canonical_object_probe() -> None:
    """Tier (b): inventory-less manifest + a present non-empty canonical object."""
    probe = _RecordingProbe(
        generation.PackagedIcObjectProbe(
            exists=True, size_bytes=len(DG_IC_CONTENT), sha256=PACKAGE_IC_SHA256
        )
    )
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(),
        resource_profile=_dg_resource_profile(),
        canonical_object_probe=probe,
    )

    assert signal.status == generation.PACKAGED_IC_QUALIFIED
    assert signal.ic_sha256 == PACKAGE_IC_SHA256
    assert signal.ic_size_bytes == len(DG_IC_CONTENT)
    assert signal.ic_relative_path == f"{DG_SHUD_INPUT_NAME}.cfg.ic"
    assert signal.qualification_source == generation.PACKAGED_IC_SOURCE_OBJECT_PROBE
    # Exactly ONE object is probed, at the canonical uri derived from the row.
    assert probe.uris == [DG_CANONICAL_IC_URI]
    assert signal.evidence()["qualification_source"] == "object_probe"


def test_canonical_object_uri_joining_tolerates_a_missing_trailing_slash() -> None:
    """Production rows end with '/', but the join must not depend on it."""
    assert (
        generation.canonical_packaged_ic_object_uri(
            model_package_uri=DG_PACKAGE_URI, shud_input_name=DG_SHUD_INPUT_NAME
        )
        == DG_CANONICAL_IC_URI
    )
    assert (
        generation.canonical_packaged_ic_object_uri(
            model_package_uri=DG_PACKAGE_URI.rstrip("/"), shud_input_name=DG_SHUD_INPUT_NAME
        )
        == DG_CANONICAL_IC_URI
    )
    # Unusable rows produce no uri at all (never a guessed sibling key).
    assert generation.canonical_packaged_ic_object_uri(model_package_uri="", shud_input_name="x") is None
    assert (
        generation.canonical_packaged_ic_object_uri(
            model_package_uri=DG_PACKAGE_URI, shud_input_name=None
        )
        is None
    )
    assert (
        generation.canonical_packaged_ic_object_uri(
            model_package_uri=DG_PACKAGE_URI, shud_input_name="../escape"
        )
        is None
    )


@pytest.mark.parametrize(
    ("probe_kwargs", "expected_status", "expected_detail"),
    [
        (
            {"exists": False},
            generation.PACKAGED_IC_UNQUALIFIED,
            "packaged_initial_condition_object_missing",
        ),
        (
            {"exists": True, "size_bytes": 0, "sha256": EMPTY_FILE_SHA256},
            generation.PACKAGED_IC_UNQUALIFIED,
            "packaged_initial_condition_object_empty",
        ),
        (
            {"exists": True, "unreadable_detail": "probe_failed"},
            generation.PACKAGED_IC_UNREADABLE,
            "probe_failed",
        ),
    ],
    ids=["object_missing", "object_empty", "probe_read_failure"],
)
def test_variant_manifest_probe_outcomes_are_distinguished(
    probe_kwargs: dict[str, Any], expected_status: str, expected_detail: str
) -> None:
    """A failed probe is UNREADABLE; a completed probe that found nothing is not."""
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(),
        resource_profile=_dg_resource_profile(),
        canonical_object_probe=_RecordingProbe(generation.PackagedIcObjectProbe(**probe_kwargs)),
    )

    assert signal.status == expected_status
    assert signal.detail == expected_detail
    assert signal.ic_sha256 == ""
    assert signal.qualification_source == generation.PACKAGED_IC_SOURCE_OBJECT_PROBE


@pytest.mark.parametrize(
    "profile",
    [
        _dg_resource_profile(shud_input_name=None),
        _dg_resource_profile(model_package_uri=None),
        _dg_resource_profile(shud_input_name=""),
    ],
    ids=["no_shud_input_name", "no_model_package_uri", "blank_shud_input_name"],
)
def test_variant_manifest_without_registry_fields_is_unqualified_without_probing(
    profile: dict[str, Any],
) -> None:
    """A row that cannot locate its IC is unqualified with its own reason."""
    probe = _RecordingProbe(
        generation.PackagedIcObjectProbe(exists=True, size_bytes=4096, sha256=PACKAGE_IC_SHA256)
    )
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(), resource_profile=profile, canonical_object_probe=probe
    )

    assert signal.status == generation.PACKAGED_IC_UNQUALIFIED
    assert signal.detail == "packaged_initial_condition_registry_fields_absent"
    assert probe.uris == []


def test_probed_malformed_header_is_unqualified_not_unreadable() -> None:
    """#1197: a READABLE object with a bad header is a content verdict.

    The malformed lh_gl delivery digested cleanly and was non-empty -- every check
    tier (b) had. Only the header shape separates it from a usable IC, and the
    verdict must land in the UNQUALIFIED domain: routing it to UNREADABLE would
    make it 'undetermined' and let the audit report it as a clean cold start.
    """
    probe = _RecordingProbe(
        generation.PackagedIcObjectProbe(
            exists=True,
            size_bytes=len(DG_IC_CONTENT),
            sha256=PACKAGE_IC_SHA256,
            header_shape_invalid_reason="IC header carries 2 numeric token(s); expected 3 or 4",
        )
    )
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(),
        resource_profile=_dg_resource_profile(),
        canonical_object_probe=probe,
    )

    assert signal.status == generation.PACKAGED_IC_UNQUALIFIED
    assert signal.detail == generation.PACKAGED_IC_HEADER_SHAPE_INVALID_DETAIL
    assert signal.detail == "packaged_initial_condition_header_shape_invalid"
    assert signal.qualification_source == generation.PACKAGED_IC_SOURCE_OBJECT_PROBE
    # The digest evidence survives the downgrade so the operator can identify the object.
    assert signal.ic_sha256 == PACKAGE_IC_SHA256
    assert probe.uris == [DG_CANONICAL_IC_URI]


def test_unreadable_probe_never_reports_the_header_shape_token() -> None:
    """AC-4 discriminator, read from the other side."""
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(),
        resource_profile=_dg_resource_profile(),
        canonical_object_probe=_RecordingProbe(
            generation.PackagedIcObjectProbe(exists=True, unreadable_detail="probe_failed")
        ),
    )

    assert signal.status == generation.PACKAGED_IC_UNREADABLE
    assert signal.detail == "probe_failed"
    assert signal.detail != generation.PACKAGED_IC_HEADER_SHAPE_INVALID_DETAIL


def test_well_formed_probed_header_keeps_qualifying() -> None:
    """A probe that reports no shape problem must not change the pre-change verdict."""
    signal = generation.classify_packaged_initial_condition(
        _direct_grid_variant_manifest(),
        resource_profile=_dg_resource_profile(),
        canonical_object_probe=_RecordingProbe(
            generation.PackagedIcObjectProbe(
                exists=True,
                size_bytes=len(DG_IC_CONTENT),
                sha256=PACKAGE_IC_SHA256,
                header_shape_invalid_reason="",
            )
        ),
    )

    assert signal.status == generation.PACKAGED_IC_QUALIFIED
    assert signal.qualification_source == generation.PACKAGED_IC_SOURCE_OBJECT_PROBE


def test_gate_probe_fills_the_header_shape_verdict_from_real_bytes() -> None:
    """The production gate's own probe implementation, over the incident bytes."""
    malformed = gate._canonical_packaged_ic_probe(
        _StubIcReader(b"23106\t6\n1\t0.1\n"), DG_CANONICAL_IC_URI
    )
    assert malformed.unreadable_detail == ""
    assert "2 numeric token(s)" in malformed.header_shape_invalid_reason

    well_formed = gate._canonical_packaged_ic_probe(
        _StubIcReader(b"23106\t6\t29714400.000000\n1\t0.1\n"), DG_CANONICAL_IC_URI
    )
    assert well_formed.header_shape_invalid_reason == ""
    assert well_formed.sha256


class _StubIcReader:
    """Minimal object reader: the gate probe only calls exists/read_bytes_limited."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def exists(self, object_uri: str) -> bool:
        return True

    def read_bytes_limited(self, object_uri: str, *, max_bytes: int) -> bytes:
        return self.content[:max_bytes]


def test_inventory_tier_never_probes_and_absent_probe_keeps_the_legacy_reason() -> None:
    """Tier (a) wins when an inventory exists; without a probe tier (b) cannot run."""
    probe = _RecordingProbe(generation.PackagedIcObjectProbe(exists=False))
    inventory = generation.classify_packaged_initial_condition(
        _package_manifest(), resource_profile=_dg_resource_profile(), canonical_object_probe=probe
    )
    assert inventory.status == generation.PACKAGED_IC_QUALIFIED
    assert inventory.qualification_source == generation.PACKAGED_IC_SOURCE_INVENTORY
    assert probe.uris == []

    no_probe = generation.classify_packaged_initial_condition(_direct_grid_variant_manifest())
    assert no_probe.status == generation.PACKAGED_IC_UNQUALIFIED
    assert no_probe.detail == "package_manifest_included_files_absent"


def test_first_cycle_variant_probe_signal_admits_packaged_ic_bootstrap() -> None:
    """判定表 row 1 via tier (b): the probed digest reaches the decision."""
    evaluation = _first_cycle_evaluation(
        generation.classify_packaged_initial_condition(
            _direct_grid_variant_manifest(),
            resource_profile=_dg_resource_profile(),
            canonical_object_probe=_RecordingProbe(
                generation.PackagedIcObjectProbe(
                    exists=True, size_bytes=131072, sha256=PACKAGE_IC_SHA256
                )
            ),
        )
    )

    assert evaluation.decision == generation.TransitionDecision.PACKAGED_IC_BOOTSTRAP
    assert evaluation.packaged_ic_checksum == PACKAGE_IC_SHA256
    evidence = generation.generation_evidence(evaluation)
    assert evidence["packaged_initial_condition"]["qualification_source"] == "object_probe"


def test_first_cycle_qualified_signal_admits_packaged_ic_bootstrap() -> None:
    """判定表 row 1: QUALIFIED -> PACKAGED_IC_BOOTSTRAP carrying the IC digest."""
    evaluation = _first_cycle_evaluation(
        generation.classify_packaged_initial_condition(_package_manifest())
    )
    assert evaluation.decision == generation.TransitionDecision.PACKAGED_IC_BOOTSTRAP
    assert evaluation.decision in generation.TransitionDecision.ADMIT
    assert evaluation.typed_reason is None
    assert evaluation.packaged_ic_checksum == PACKAGE_IC_SHA256
    assert evaluation.cold_start_reason is None
    evidence = generation.generation_evidence(evaluation)
    assert evidence["decision"] == "packaged_ic_bootstrap"
    assert evidence["packaged_initial_condition"]["status"] == generation.PACKAGED_IC_QUALIFIED
    assert evidence["packaged_initial_condition"]["ic_sha256"] == PACKAGE_IC_SHA256


@pytest.mark.parametrize(
    "signal_factory",
    [
        lambda: generation.classify_packaged_initial_condition(_package_manifest(include_ic=False)),
        lambda: generation.classify_packaged_initial_condition(
            _package_manifest(ic_sha256=EMPTY_FILE_SHA256)
        ),
        lambda: generation.classify_packaged_initial_condition(_package_manifest(ic_size_bytes=0)),
        lambda: generation.classify_packaged_initial_condition(
            _package_manifest(extra_ic_entries=[_calib_ic_entry()])
        ),
        lambda: generation.classify_packaged_initial_condition(
            _package_manifest(include_ic=False, extra_ic_entries=[_calib_ic_entry()])
        ),
    ],
    ids=["missing_entry", "empty_digest", "zero_size", "ambiguous_entries", "non_canonical_entry"],
)
def test_first_cycle_unqualified_signal_blocks_with_typed_reason(signal_factory: Any) -> None:
    """判定表 row 3: UNQUALIFIED -> fail-closed block, never a silent cold start."""
    evaluation = _first_cycle_evaluation(signal_factory())
    assert (
        evaluation.decision
        == generation.TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED
    )
    assert evaluation.decision in generation.TransitionDecision.BLOCK
    assert evaluation.typed_reason == "first_cycle_initial_state_undecided"
    assert evaluation.cold_start_reason is None
    assert evaluation.packaged_ic_checksum is None


def test_first_cycle_unreadable_manifest_blocks_rather_than_reading_as_no_ic() -> None:
    """判定表 row 4: UNREADABLE -> the same fail-closed block (never UNQUALIFIED)."""
    evaluation = _first_cycle_evaluation(
        generation.PackagedIcSignal(
            status=generation.PACKAGED_IC_UNREADABLE,
            detail="package_manifest_malformed_json",
        )
    )
    assert (
        evaluation.decision
        == generation.TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED
    )
    assert evaluation.typed_reason == "first_cycle_initial_state_undecided"


def test_first_cycle_absent_signal_keeps_legacy_labeled_cold_start() -> None:
    """判定表 row 5 (carve-out): no signal -> byte-identical legacy behavior."""
    evaluation = _first_cycle_evaluation(None)
    assert evaluation.decision == generation.TransitionDecision.COLD_NEW_MODEL
    assert evaluation.cold_start_reason == "no_prior_history"
    assert evaluation.typed_reason is None
    assert evaluation.packaged_ic_checksum is None
    # The parameter is OPTIONAL: omitting it entirely is the same carve-out.
    omitted = generation.evaluate_transition_decision(
        model_id="model_new",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=False, exists_current=False),
        declaration=None,
    )
    assert omitted.decision == generation.TransitionDecision.COLD_NEW_MODEL


def test_qualified_signal_never_overrides_an_existing_history_decision() -> None:
    """F9 guard: the signal is only consulted on the empty-history branch."""
    qualified = generation.classify_packaged_initial_condition(_package_manifest())
    warm = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(
            exists_any=True,
            exists_current=True,
            has_exact_predecessor=True,
            latest_any_checksum=NEW_CHECKSUM,
        ),
        declaration=None,
        packaged_initial_condition=qualified,
    )
    assert warm.decision == generation.TransitionDecision.WARM_CONTINUE
    cutover_boundary = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=None,
        packaged_initial_condition=qualified,
    )
    assert cutover_boundary.decision == generation.TransitionDecision.BLOCK_DECLARATION_MISSING


# ---------------------------------------------------------------------------
# T15 (#1164): gate-level qualification IO + the two named bypass carve-outs.
#
# These drive ``scheduler_generation_gate.strict_warm_start_evidence`` through a
# minimal scheduler stub so the manifest read, the new evidence mode, and the
# bypass carve-outs are exercised without a full DB-free scheduler pass.
# ---------------------------------------------------------------------------


class _StubStateIndex:
    def __init__(
        self,
        *,
        signal_ready: bool = True,
        exists_any: bool = False,
        exists_current: bool = False,
        latest_current: dict[str, Any] | None = None,
    ) -> None:
        self.signal_ready = signal_ready
        self.exists_any = exists_any
        self.exists_current = exists_current
        self.latest_current = latest_current
        self.history_signal_calls = 0

    def generation_scoped_history_signal(self, **_kwargs: Any) -> dict[str, Any]:
        self.history_signal_calls += 1
        return {
            "ready": self.signal_ready,
            "history_exists_current_generation": self.exists_current,
            "history_exists_any_generation": self.exists_any,
            "latest_current_generation_checkpoint": self.latest_current,
            "latest_any_generation_checkpoint": None,
            "wrong_generation_predecessor_present": False,
            "wrong_generation_predecessor_checksum": "",
        }

    def strict_warm_start_evidence(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "blocked",
            "ready": False,
            "reason": "state_snapshot_index_exact_checkpoint_missing",
            "mode": "db_free_legacy_probe",
        }

    def usable_state_history_evidence(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ready": True, "history_exists": False}


class _StubScheduler:
    def __init__(
        self,
        *,
        index: _StubStateIndex,
        object_store_root: Path,
        strict_warm_start_required: bool = True,
    ) -> None:
        from services.orchestrator.scheduler_generation_gate import CUTOVER_DECLARATION_UNLOADED

        self._index = index
        self._strict_warm_start_required = strict_warm_start_required
        self.config = SimpleNamespace(
            now=NOW,
            object_store_root=object_store_root,
            workspace_root=object_store_root,
        )
        self._cutover_declaration_cache = CUTOVER_DECLARATION_UNLOADED

    def _required_warm_start_lead_hours(self, _candidate: Any, _cycle: Any) -> int:
        return 12

    def _db_free_state_index_provider(self) -> _StubStateIndex:
        return self._index

    def _db_free_strict_warm_start_required_for(self, _candidate: Any) -> bool:
        return self._strict_warm_start_required


def _stub_candidate(*, resource_profile: dict[str, Any]) -> Any:
    return SimpleNamespace(
        model_id="model_new",
        source_id="gfs",
        cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        resource_profile=resource_profile,
        model_package_uri="s3://nhms/models/model_new/package/",
        run_id="fcst_gfs_2026070612_model_new",
    )


def _write_package_manifest(object_root: Path, payload: Any, *, key: str = "models/model_new/manifest.json") -> str:
    from packages.common.object_store import LocalObjectStore

    store = LocalObjectStore(object_root, "s3://nhms")
    content = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode("utf-8")
    store.write_bytes_atomic(key, content)
    return f"s3://nhms/{key}"


def test_gate_emits_packaged_ic_bootstrap_evidence_for_qualified_first_cycle(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    manifest_uri = _write_package_manifest(object_root, _package_manifest())
    index = _StubStateIndex(exists_any=False, exists_current=False)
    scheduler = _StubScheduler(index=index, object_store_root=object_root)
    candidate = _stub_candidate(
        resource_profile={"package_checksum": NEW_CHECKSUM, "manifest_uri": manifest_uri}
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["mode"] == "db_free_packaged_ic_bootstrap"
    assert evidence["ready"] is True
    assert evidence["status"] == "ready"
    assert evidence["packaged_ic_checksum"] == PACKAGE_IC_SHA256
    assert evidence["registry_cutover_transition"]["decision"] == "packaged_ic_bootstrap"


@pytest.mark.parametrize(
    "payload",
    [_package_manifest(include_ic=False), _package_manifest(ic_size_bytes=0)],
    ids=["missing_entry", "zero_size"],
)
def test_gate_blocks_first_cycle_when_package_ic_is_unqualified(tmp_path: Path, payload: Any) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    manifest_uri = _write_package_manifest(object_root, payload)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(
        resource_profile={"package_checksum": NEW_CHECKSUM, "manifest_uri": manifest_uri}
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["ready"] is False
    assert evidence["reason"] == "first_cycle_initial_state_undecided"
    assert evidence["registry_cutover_transition"]["decision"] == (
        "block_first_cycle_initial_state_undecided"
    )


@pytest.mark.parametrize(
    "payload",
    [b"{not-json", b""],
    ids=["malformed_json", "empty_object"],
)
def test_gate_blocks_first_cycle_when_package_manifest_is_unreadable(
    tmp_path: Path, payload: bytes
) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    manifest_uri = _write_package_manifest(object_root, payload)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(
        resource_profile={"package_checksum": NEW_CHECKSUM, "manifest_uri": manifest_uri}
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "first_cycle_initial_state_undecided"


def test_gate_blocks_first_cycle_when_referenced_package_manifest_is_absent(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(
        resource_profile={
            "package_checksum": NEW_CHECKSUM,
            "manifest_uri": "s3://nhms/models/model_new/manifest.json",
        }
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "first_cycle_initial_state_undecided"


def _write_dg_variant_package(object_root: Path, *, ic_content: bytes | None = DG_IC_CONTENT) -> None:
    """Publish a production-shaped direct-grid variant package under ``object_root``."""
    from packages.common.object_store import LocalObjectStore

    store = LocalObjectStore(object_root, "s3://nhms")
    store.write_bytes_atomic(
        DG_MANIFEST_KEY, json.dumps(_direct_grid_variant_manifest(), sort_keys=True).encode("utf-8")
    )
    store.write_bytes_atomic(f"{DG_PACKAGE_KEY}/{DG_SHUD_INPUT_NAME}.sp.mesh", b"mesh\n")
    if ic_content is not None:
        store.write_bytes_atomic(f"{DG_PACKAGE_KEY}/{DG_SHUD_INPUT_NAME}.cfg.ic", ic_content)


def test_gate_qualifies_inventory_less_variant_package_via_the_object_probe(tmp_path: Path) -> None:
    """The production 36/36 shape: no ``included_files``, IC decided by the probe."""
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    _write_dg_variant_package(object_root)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(resource_profile=_dg_resource_profile())

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["mode"] == "db_free_packaged_ic_bootstrap"
    assert evidence["ready"] is True
    assert evidence["packaged_ic_checksum"] == hashlib.sha256(DG_IC_CONTENT).hexdigest()
    packaged = evidence["registry_cutover_transition"]["packaged_initial_condition"]
    assert packaged["qualification_source"] == "object_probe"
    assert packaged["ic_relative_path"] == f"{DG_SHUD_INPUT_NAME}.cfg.ic"


@pytest.mark.parametrize(
    "ic_content", [None, b""], ids=["canonical_object_missing", "canonical_object_empty"]
)
def test_gate_blocks_variant_package_without_a_usable_canonical_ic_object(
    tmp_path: Path, ic_content: bytes | None
) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    _write_dg_variant_package(object_root, ic_content=ic_content)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(resource_profile=_dg_resource_profile())

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "first_cycle_initial_state_undecided"
    packaged = evidence["registry_cutover_transition"]["packaged_initial_condition"]
    assert packaged["status"] == "unqualified"
    assert packaged["qualification_source"] == "object_probe"


def test_gate_blocks_variant_package_whose_row_names_no_shud_input(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    _write_dg_variant_package(object_root)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(resource_profile=_dg_resource_profile(shud_input_name=None))

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["registry_cutover_transition"]["packaged_initial_condition"]["detail"] == (
        "packaged_initial_condition_registry_fields_absent"
    )


def test_gate_reports_an_unreadable_canonical_ic_object_as_unreadable(tmp_path: Path) -> None:
    """A probe that cannot complete blocks as UNREADABLE, never as "no IC"."""
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    _write_dg_variant_package(object_root, ic_content=None)
    # A DIRECTORY at the canonical IC key: it stats (exists) but cannot be read.
    (object_root / DG_PACKAGE_KEY / f"{DG_SHUD_INPUT_NAME}.cfg.ic").mkdir(parents=True)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(resource_profile=_dg_resource_profile())

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["status"] == "blocked"
    packaged = evidence["registry_cutover_transition"]["packaged_initial_condition"]
    assert packaged["status"] == "unreadable"
    assert packaged["detail"] == "packaged_initial_condition_object_probe_failed"


def test_gate_keeps_cold_new_model_when_registry_has_no_package_manifest_reference(
    tmp_path: Path,
) -> None:
    """carve-out: registered model without a published manifest reference."""
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    scheduler = _StubScheduler(index=_StubStateIndex(), object_store_root=object_root)
    candidate = _stub_candidate(resource_profile={"package_checksum": NEW_CHECKSUM})

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["mode"] == "db_free_cold_new_model"
    assert evidence["cold_start_reason"] == "no_prior_history"
    assert evidence["registry_cutover_transition"]["decision"] == "cold_new_model"


def test_gate_bypass_without_package_checksum_and_declaration_stays_legacy(tmp_path: Path) -> None:
    """Named bypass #1 (``scheduler_generation_gate.py:322-328``).

    A QUALIFIED package manifest sits on disk, but the candidate carries no
    registry ``package_checksum`` and no declaration is configured — the legacy
    path must answer, with no packaged signal and no §8 transition evidence.
    """
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    manifest_uri = _write_package_manifest(object_root, _package_manifest())
    index = _StubStateIndex()
    scheduler = _StubScheduler(index=index, object_store_root=object_root)
    candidate = _stub_candidate(resource_profile={"manifest_uri": manifest_uri})

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["mode"] != "db_free_packaged_ic_bootstrap"
    assert "registry_cutover_transition" not in evidence
    assert index.history_signal_calls == 0


def test_gate_bypass_with_unavailable_state_index_stays_legacy(tmp_path: Path) -> None:
    """Named bypass #2 (``scheduler_generation_gate.py:336-346``).

    The history signal is not ready, so §8 (and the #1164 first-cycle decision
    that rides it) must defer to the legacy path even though a QUALIFIED
    package manifest is readable.
    """
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    manifest_uri = _write_package_manifest(object_root, _package_manifest())
    index = _StubStateIndex(signal_ready=False)
    scheduler = _StubScheduler(index=index, object_store_root=object_root)
    candidate = _stub_candidate(
        resource_profile={"package_checksum": NEW_CHECKSUM, "manifest_uri": manifest_uri}
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert index.history_signal_calls == 1
    assert evidence["mode"] != "db_free_packaged_ic_bootstrap"
    assert "registry_cutover_transition" not in evidence


def test_gate_does_not_read_package_manifest_when_history_exists(tmp_path: Path) -> None:
    """No new object IO on the warm path: qualification is a first-cycle probe."""
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    index = _StubStateIndex(exists_any=True, exists_current=True)
    scheduler = _StubScheduler(index=index, object_store_root=object_root)
    # ``manifest_uri`` points at an object that does NOT exist: if the gate read
    # it on the existing-history path this would fail closed and block.
    candidate = _stub_candidate(
        resource_profile={
            "package_checksum": NEW_CHECKSUM,
            "manifest_uri": "s3://nhms/models/model_new/manifest.json",
        }
    )

    evidence = gate.strict_warm_start_evidence(scheduler, candidate, cycle=None)

    assert evidence is not None
    assert evidence["reason"] != "first_cycle_initial_state_undecided"


def test_predecessor_pending_without_earlier_history_still_blocks_at_gate(
    tmp_path: Path,
) -> None:
    """#1150 fail-open guard, relocated to the gate seam by #1775 D5.

    The guard: ``scheduler_generation_gate`` may return ``None`` (the
    "no warm gate" cold-seed passthrough that ``scheduler_candidates`` reads as
    "admit") ONLY for ``warm_continue``.  Every other decision that reaches the
    strictly-earlier history probe — ``block_predecessor_pending`` above all —
    must fall through to blocked evidence.  Pre-#1150 the passthrough ignored
    the decision and the blocked candidate was ADMITTED with empty evidence.

    This used to be pinned end-to-end through ``_build_candidates`` with a
    state-index entry dated LATER than the candidate cycle, which was the only
    geometry that made the two history predicates disagree (the matrix signal
    counted any usable current-generation entry; this probe counts strictly
    earlier ones).  #1775 D5 scopes the matrix signal to ``valid_time <=
    cutoff``, so that geometry no longer produces current-generation history at
    all, and the disagreement survives only at ``valid_time == cutoff`` — where
    the strict probe reports a cycle-id/lead mismatch and returns before this
    branch.  The gate code is unchanged, so the pin moves down to the seam that
    still reaches it, driven by the same stubs the T15 gate tests use.
    """
    from services.orchestrator import scheduler_generation_gate as gate

    object_root = tmp_path / "object-store"
    object_root.mkdir(parents=True, exist_ok=True)
    candidate = _stub_candidate(
        resource_profile={
            "package_checksum": NEW_CHECKSUM,
            "manifest_uri": "s3://nhms/models/model_new/manifest.json",
        }
    )
    # Current-generation history exists but names no exact predecessor →
    # branch (e) → ``block_predecessor_pending``; the strict probe answers
    # ``exact_checkpoint_missing`` and the strictly-earlier history probe
    # answers ``history_exists=False`` (both stub defaults).
    pending = _StubScheduler(
        index=_StubStateIndex(exists_any=True, exists_current=True, latest_current=None),
        object_store_root=object_root,
        strict_warm_start_required=False,
    )

    evidence = gate.strict_warm_start_evidence(pending, candidate, cycle=None)

    assert evidence is not None, "block_predecessor_pending must never take the cold-seed passthrough"
    assert evidence["ready"] is False
    assert evidence["reason"] == "state_snapshot_index_prior_checkpoint_missing_after_history"
    assert (
        evidence["registry_cutover_transition"]["decision"] == "block_predecessor_pending"
    )

    # Positive half of the same predicate: warm_continue — which has no block
    # to overturn — still passes through on the identical probe answers.
    warm = _StubScheduler(
        index=_StubStateIndex(
            exists_any=True,
            exists_current=True,
            latest_current={
                "has_exact_predecessor": True,
                "predecessor_valid_time": "2026-07-06T00:00:00Z",
                "predecessor_cycle_id": "gfs_2026070600",
                "predecessor_lead_hours": 12,
            },
        ),
        object_store_root=object_root,
        strict_warm_start_required=False,
    )

    assert gate.strict_warm_start_evidence(warm, candidate, cycle=None) is None


# ---------------------------------------------------------------------------
# T16 (#1164): end-to-end through the real candidate builder.
#
# The gate-stub tests above pin the decision surface; these two pin the WIRING —
# that a first-cycle candidate actually reaches ``blocked`` with the typed reason
# (fail-closed) and that an admitted packaged bootstrap carries the digest the
# cohort carrier needs.  Without these, a correct gate could still be bypassed
# by the candidate builder.
# ---------------------------------------------------------------------------


def _db_free_first_cycle_pass(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    package_manifest: Any,
    manifest_key: str = "models/model_a/package_manifest.json",
) -> tuple[list[Any], list[Any]]:
    """Run one real DB-free candidate pass for an empty-history model_a.

    Returns ``(candidates, blocked)``.  The registry row's
    ``resource_profile.manifest_uri`` points at ``package_manifest`` so the
    #1164 qualification read is exercised through the production seam.
    """
    from packages.common.object_store import LocalObjectStore
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        _gfs_default_forecast_hours,
        _set_db_free_scheduler_env,
        _write_db_free_file_provider_fixtures,
    )
    from tests.test_production_scheduler import (
        _dt as _pdt,
    )
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    cycle_time = _pdt("2026-05-21T12:00:00Z")
    generated_at = _pdt("2026-05-21T18:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
    )
    # Empty state index (published by the fixture helper) == first cycle.
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    content = (
        package_manifest
        if isinstance(package_manifest, bytes)
        else json.dumps(package_manifest, sort_keys=True).encode("utf-8")
    )
    store.write_bytes_atomic(manifest_key, content)
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
            "manifest_uri": f"s3://nhms/{manifest_key}",
        },
    }
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(now=generated_at, allowed_cycle_hours_utc=(0, 12)),
        registry=FakeRegistry([model]),
        adapters={},
        orchestrator_factory=lambda _source_id: pytest.fail(
            "first-cycle decision must not build an orchestrator in this pass"
        ),
    )
    candidates, blocked, _skipped, _duplicates, _slurm_sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model)],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052112",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    cycle_hour=12,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    return candidates, blocked


def test_first_cycle_unqualified_package_blocks_the_real_candidate_pass(
    monkeypatch: Any, tmp_path: Path
) -> None:
    candidates, blocked = _db_free_first_cycle_pass(
        monkeypatch,
        tmp_path,
        package_manifest=_package_manifest(ic_size_bytes=0),
    )

    assert candidates == []
    assert len(blocked) == 1
    assert blocked[0].reason == "first_cycle_initial_state_undecided"
    assert (
        blocked[0].state_evidence["registry_cutover_transition"]["decision"]
        == "block_first_cycle_initial_state_undecided"
    )


def test_first_cycle_qualified_package_admits_candidate_carrying_the_ic_digest(
    monkeypatch: Any, tmp_path: Path
) -> None:
    candidates, blocked = _db_free_first_cycle_pass(
        monkeypatch,
        tmp_path,
        package_manifest=_package_manifest(),
    )

    assert blocked == []
    assert len(candidates) == 1
    # An admitted candidate carries the gate evidence as its top-level
    # ``state_evidence`` (the nested ``strict_warm_start`` layer is a retry/
    # blocked shape).  ``chain_forecast_cycle._state_evidence_layers`` reads both.
    evidence = candidates[0].state_evidence
    assert evidence["mode"] == "db_free_packaged_ic_bootstrap"
    assert evidence["ready"] is True
    assert evidence["packaged_ic_checksum"] == PACKAGE_IC_SHA256
    assert evidence["cold_start_reason"] is None


# ---------------------------------------------------------------------------
# #1196: the NHMS_REQUIRE_FORECAST_WARM_START compat toggle is three-valued.
#
# ``forecast_warm_start_env_enabled`` used to fold every
# ``OrchestratorConfig.from_env()`` failure into ``False`` with zero logging,
# which is the value that ENABLES the D8.9 terminal-skip shortcut at
# ``scheduler_core._strict_warm_start_for_candidate``.  "The check could not
# be completed" must stay distinguishable from "the check answered no".
# ---------------------------------------------------------------------------

#: Logger the gate warns on — asserted by name so a module move cannot leave
#: the operator-facing warning silently unrouted.
_GATE_LOGGER = "services.orchestrator.scheduler_generation_gate"

#: Token the unreadable-env warning carries.
_UNREADABLE_TOKEN = "SCHEDULER_WARM_START_ENV_UNREADABLE"


def test_warm_start_env_toggle_is_three_valued(monkeypatch: Any) -> None:
    """#1196 verdict 1: true -> True, false -> False, unset -> False,
    unreadable -> None.  The unreadable state has its own value; it never
    borrows the "explicitly disabled" one."""
    monkeypatch.delenv("FORECAST_HORIZON_HOURS", raising=False)

    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    assert gate.forecast_warm_start_env_enabled(SimpleNamespace()) is True

    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    assert gate.forecast_warm_start_env_enabled(SimpleNamespace()) is False

    # Unset parses to the ``_env_flag(..., default=False)`` documented default
    # (chain_config.py:154 + :168-171).  ``delenv`` keeps an inherited process
    # env out of the verdict.
    monkeypatch.delenv("NHMS_REQUIRE_FORECAST_WARM_START", raising=False)
    assert gate.forecast_warm_start_env_enabled(SimpleNamespace()) is False

    # An UNRELATED broken variable makes the whole orchestrator config
    # unreadable (``int("abc")`` at chain_config.py:150).
    monkeypatch.setenv("FORECAST_HORIZON_HOURS", "abc")
    assert gate.forecast_warm_start_env_enabled(SimpleNamespace()) is None


@pytest.mark.parametrize(
    ("broken_env", "broken_value", "expected_cause_text"),
    [
        # Unrelated variable: ``int()`` names the offending VALUE.
        (
            "FORECAST_HORIZON_HOURS",
            "abc",
            "invalid literal for int() with base 10: 'abc'",
        ),
        # The toggle itself: ``_env_flag`` names the offending VARIABLE
        # (chain_config.py:176).
        (
            "NHMS_REQUIRE_FORECAST_WARM_START",
            "maybe",
            "NHMS_REQUIRE_FORECAST_WARM_START must be a boolean value.",
        ),
    ],
)
def test_unreadable_warm_start_env_warns_once_per_scheduler_with_root_cause(
    monkeypatch: Any,
    caplog: Any,
    broken_env: str,
    broken_value: str,
    expected_cause_text: str,
) -> None:
    """#1196 verdict 3: the unreadable verdict carries an attributable
    WARNING — token + ``repr(exc)`` — and repeats at most once per scheduler
    instance so a ``run_continuous`` pass cannot spam identical lines."""
    monkeypatch.delenv("FORECAST_HORIZON_HOURS", raising=False)
    monkeypatch.delenv("NHMS_REQUIRE_FORECAST_WARM_START", raising=False)
    monkeypatch.setenv(broken_env, broken_value)
    scheduler = SimpleNamespace()

    with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
        assert gate.forecast_warm_start_env_enabled(scheduler) is None
        assert gate.forecast_warm_start_env_enabled(scheduler) is None
        # A DIFFERENT scheduler instance gets its own warning budget.
        other = SimpleNamespace()
        assert gate.forecast_warm_start_env_enabled(other) is None

    records = [
        record
        for record in caplog.records
        if record.name == _GATE_LOGGER and _UNREADABLE_TOKEN in record.getMessage()
    ]
    assert [record.levelno for record in records] == [logging.WARNING, logging.WARNING]
    for record in records:
        # ``repr(exc)`` — the operator reads the root cause straight from the log.
        assert expected_cause_text in record.getMessage()


def test_readable_warm_start_env_logs_no_unreadable_warning(
    monkeypatch: Any, caplog: Any
) -> None:
    """#1196 must-preserve: a readable env emits zero new logging."""
    monkeypatch.delenv("FORECAST_HORIZON_HOURS", raising=False)
    scheduler = SimpleNamespace()

    with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
        for value, expected in (("true", True), ("false", False)):
            monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", value)
            assert gate.forecast_warm_start_env_enabled(scheduler) is expected

    assert [
        record.getMessage()
        for record in caplog.records
        if _UNREADABLE_TOKEN in record.getMessage()
    ] == []


# ---------------------------------------------------------------------------
# #1735 (`lineage-scoped-cycle-completion`) 5.3, generation surface: a model
# with no clone lineage must keep its history-existence signal and its
# first-cycle / cold-start admission byte-for-byte.  The change touches
# completion scope and cohort membership only — ``generation_scoped_history_
# signal`` is explicitly NOT touched.
# ---------------------------------------------------------------------------


def test_model_without_clone_lineage_keeps_the_first_cycle_cold_start_branch(
    tmp_path: Path,
) -> None:
    from services.orchestrator import scheduler_lineage
    from tests.lineage_state_index_fixtures import index_entry as _lineage_entry
    from tests.lineage_state_index_fixtures import index_repository as _lineage_repository

    object_root = tmp_path / "lineage" / "objects"
    repo = _lineage_repository(
        tmp_path / "lineage",
        [
            # Someone else's recalibration sits in the very same index.
            _lineage_entry(
                object_root=object_root,
                model_id="model_other_prime",
                valid_time="2026-07-06T00:00:00Z",
                cloned_from_model_id="model_other",
            )
        ],
        generated_at="2026-07-06T00:00:00Z",
        now="2026-07-06T12:00:00Z",
    )

    # No lineage for this model — resolution yields None, never an error.
    assert (
        scheduler_lineage.resolve_lineage_cutover(repo, model_id="model_new", source_id="gfs")
        is None
    )

    signal_evidence = repo.generation_scoped_history_signal(
        model_id="model_new",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
    )
    assert signal_evidence["ready"] is True
    assert signal_evidence["history_exists_any_generation"] is False
    assert signal_evidence["history_exists_current_generation"] is False

    evaluation = generation.evaluate_transition_decision(
        model_id="model_new",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=generation._HistorySignal(
            exists_current_generation=bool(
                signal_evidence.get("history_exists_current_generation")
            ),
            exists_any_generation=bool(signal_evidence.get("history_exists_any_generation")),
        ),
        declaration=None,
    )

    assert evaluation.decision == generation.TransitionDecision.COLD_NEW_MODEL
    assert evaluation.cold_start_reason == "no_prior_history"
    assert evaluation.typed_reason is None
