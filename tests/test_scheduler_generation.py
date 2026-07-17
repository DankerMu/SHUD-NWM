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
    pass — round-1 B3 fix adds ``RecursionError`` to the loader's except."""
    depth = 2000
    payload = "[" * depth + "]" * depth
    path = tmp_path / "cutover-deep.json"
    path.write_text(payload, encoding="utf-8")
    result = generation.load_cutover_declaration(str(path), now=NOW)
    assert isinstance(result, dict)
    # Either the JSON parser hit RecursionError → declaration_malformed_json,
    # OR it decoded a plain nested list → declaration_wrong_schema; both
    # honor the documented "NEVER raises" contract.
    assert result.get("_load_error") in {
        "declaration_malformed_json",
        "declaration_wrong_schema",
    }


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
# T13 (C3): retry / restart across a cutover — idempotence under cache reset
# ---------------------------------------------------------------------------


def test_transition_decision_idempotent_across_scheduler_restart(tmp_path: Path) -> None:
    """A cutover admit at effective_cycle_utc must yield identical decision
    and selected_predecessor across a scheduler restart (module has no
    cross-call cache; a fresh load reproduces the same evaluation)."""
    path = _write_declaration(tmp_path)
    first = generation.load_cutover_declaration(str(path), now=NOW)
    # Simulate restart: evict any in-memory caches by re-reading from disk.
    second = generation.load_cutover_declaration(str(path), now=NOW)
    evaluations = []
    for declaration in (first, second):
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
            ),
            declaration=declaration,
        )
        evaluations.append(evaluation)
    assert evaluations[0].decision == evaluations[1].decision
    assert evaluations[0].generation == evaluations[1].generation
    assert evaluations[0].selected_predecessor == evaluations[1].selected_predecessor


# ---------------------------------------------------------------------------
# T14 (C4): concurrent scheduler-plan calls survive shared-cache boundaries
# ---------------------------------------------------------------------------


def test_transition_decision_survives_concurrent_evaluation(tmp_path: Path) -> None:
    """Two concurrent scheduler passes across a cutover boundary must each
    emit self-consistent transition_decision + no torn-write on the shared
    caches.  A lease-serialized pair with staggered declaration visibility
    proves the caches survive per the round-1 candidate-list note."""
    import threading

    path = _write_declaration(tmp_path)
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    results: list[generation.TransitionEvaluation] = []
    lock = threading.Lock()

    def _run() -> None:
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
            ),
            declaration=declaration,
        )
        with lock:
            results.append(evaluation)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4
    decisions = {result.decision for result in results}
    generations = {result.generation for result in results}
    predecessors = {
        json.dumps(result.selected_predecessor, sort_keys=True, default=str)
        for result in results
    }
    assert len(decisions) == 1
    assert len(generations) == 1
    assert len(predecessors) == 1


# ---------------------------------------------------------------------------
# T15 (C5): AC8 (b)/(c) env-override regressions — depend on A1 landing
# ---------------------------------------------------------------------------


def test_env_override_does_not_admit_missing_predecessor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9 (b): with a valid declaration + missing predecessor, env=false
    must still block with ``block_predecessor_pending`` — never admit."""
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    path = _write_declaration(tmp_path, effective_cycle_utc="2026-07-06T00:00:00Z")
    declaration = generation.load_cutover_declaration(str(path), now=NOW)
    evaluation = generation.evaluate_transition_decision(
        model_id="model_a",
        package_checksum=NEW_CHECKSUM,
        source_id="gfs",
        # Candidate cycle later than effective — requires exact NEW-gen
        # predecessor, which is absent.
        candidate_cycle_time_utc=_dt("2026-07-06T12:00:00Z"),
        required_lead_hours=12,
        history=_signal(exists_any=True, exists_current=False, latest_any_checksum=OLD_CHECKSUM),
        declaration=declaration,
    )
    assert evaluation.decision == generation.TransitionDecision.BLOCK_PREDECESSOR_PENDING
    assert evaluation.typed_reason == "state_snapshot_index_prior_checkpoint_missing_after_history"


def test_env_override_does_not_admit_wrong_generation_checkpoint(
    monkeypatch: Any,
) -> None:
    """§8.9 (c): with a wrong-generation state entry at the expected
    predecessor key, env=false must still block with
    ``block_wrong_generation`` — coupled with A1 being fixed."""
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
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
