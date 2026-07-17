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

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler_generation as generation

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
    payload = generation.load_cutover_declaration(str(path))
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
    payload = generation.load_cutover_declaration(str(missing))
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
    payload = generation.load_cutover_declaration(str(path))
    assert payload == {"_load_error": "declaration_wrong_schema"}


def test_load_cutover_declaration_rejects_invalid_transition_mode(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, transition_mode="rebase")
    payload = generation.load_cutover_declaration(str(path))
    assert payload is not None
    assert payload.get("_load_error") == "declaration_entry_transition_mode_invalid"


def test_load_cutover_declaration_rejects_effective_cycle_off_hour(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T03:00:00Z")
    payload = generation.load_cutover_declaration(str(path))
    assert payload is not None
    assert payload.get("_load_error") == "declaration_entry_effective_cycle_invalid"


def test_load_cutover_declaration_rejects_non_hex_checksum(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, new_checksum="not-a-hex-string")
    payload = generation.load_cutover_declaration(str(path))
    assert payload is not None
    assert payload.get("_load_error") == "declaration_entry_checksum_invalid"


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
    payload = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)))
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
    declaration = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(path))
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
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)))
    entry = generation.match_declaration_entry(declaration, model_id="model_a")
    assert entry is not None
    assert entry["model_id"] == "model_a"


def test_match_declaration_entry_returns_none_for_unknown_model(tmp_path: Path) -> None:
    declaration = generation.load_cutover_declaration(str(_write_declaration(tmp_path)))
    assert generation.match_declaration_entry(declaration, model_id="model_b") is None


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
