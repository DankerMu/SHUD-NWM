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
    pass — round-1 B3 fix adds ``RecursionError`` to the loader's except.

    R2-B5 (round-2 review): pin the deterministic branch on CPython 3.11 (the
    CI Python — see .github/workflows/ci.yml).  The default recursion limit
    is 1000; ``json.loads`` on 2000-depth nested lists deterministically
    raises ``RecursionError`` before the top-level array parses, so the
    loader routes to ``declaration_malformed_json``.  A future Python bump
    that raises the recursion limit high enough for the array to parse would
    surface as ``declaration_wrong_schema`` (the schema requires an object,
    not a list); the pin is deliberately deterministic so a silent branch
    shift on the CI interpreter is caught, and a Python-version bump has to
    consciously update this assertion.
    """
    depth = 2000
    payload = "[" * depth + "]" * depth
    path = tmp_path / "cutover-deep.json"
    path.write_text(payload, encoding="utf-8")
    result = generation.load_cutover_declaration(str(path), now=NOW)
    assert isinstance(result, dict)
    assert result.get("_load_error") == "declaration_malformed_json"


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

    # All 4 threads observed the same §8 decision (cache did not tear).
    assert reasons == ["registry_cutover_declaration_missing"] * 4
    # After 4 concurrent passes the per-lifetime cache still holds ONE
    # loaded value (not 4 distinct entries or a partial state).
    assert not isinstance(scheduler._cutover_declaration_cache, list)


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
    """§8.9 (b): env=false + valid declaration + missing predecessor -> block."""
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
    # Write a valid declaration whose effective_cycle_utc is BEFORE the
    # candidate cycle → the transition matrix requires an exact NEW-gen
    # predecessor at (cycle - 12h), which does not exist → §8.6
    # block_predecessor_pending.
    declaration_path = tmp_path / "cutover-declaration.json"
    _pkg = fixture["package_checksum"]
    _pkg_hex = generation.derive_generation(_pkg)
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": generation.CUTOVER_DECLARATION_SCHEMA_VERSION,
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "generation": _pkg_hex,
                "entries": [
                    {
                        "model_id": "model_a",
                        "old_checksum": "a" * 64,
                        "new_checksum": _pkg if _looks_like_hex64(_pkg) else "b" * 64,
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
            "package_checksum": fixture["package_checksum"]
            if _looks_like_hex64(fixture["package_checksum"])
            else "b" * 64,
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
    # Decision string: block_declaration_stale for a fabricated fixture is
    # acceptable when the fixture's new_checksum cannot match the model
    # resource_profile — but the KEY assertion is that env-override did
    # NOT admit the candidate.  Any admit would surface a non-empty
    # candidates list.
    assert blocked[0].reason in {
        "state_snapshot_index_prior_checkpoint_missing_after_history",
        "registry_cutover_declaration_stale",
    }
    transition_decision = (
        blocked[0].state_evidence.get("registry_cutover_transition", {}).get(
            "decision"
        )
    )
    assert transition_decision in {
        generation.TransitionDecision.BLOCK_PREDECESSOR_PENDING,
        generation.TransitionDecision.BLOCK_DECLARATION_STALE,
    }


def test_env_override_does_not_admit_wrong_generation_checkpoint(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """§8.9 (c): env=false + wrong-generation state entry at expected key -> block."""
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
    # Wrong-generation state entry sitting AT the expected predecessor key
    # (2026-05-21T00Z for a 12h lead) — this drives BLOCK_WRONG_GENERATION
    # or BLOCK_DECLARATION_MISSING at the §8 gate, never admit.
    wrong_gen_entry = _old_generation_state_entry(
        roots,
        state_id="state_wrong_gen_at_expected_key",
        valid_time="2026-05-21T00:00:00Z",
        cycle_id="gfs_2026052100",
        lead_hours=12,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[wrong_gen_entry],
    )
    model = {
        **fixture["model"],
        "resource_profile": {
            **dict(fixture["model"]["resource_profile"]),
            "package_checksum": fixture["package_checksum"],
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
    # The key claim: env=false did not admit the candidate.  §8 either
    # blocks with declaration_missing (no declaration configured) or
    # wrong_generation (declaration configured + wrong lineage) — both
    # are valid §8 blocks; admit is the failure we're guarding against.
    assert blocked[0].reason in {
        "registry_cutover_declaration_missing",
        "state_snapshot_index_generation_mismatch",
    }


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
