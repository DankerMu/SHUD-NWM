"""Status-preserving scheduler evidence tiers after the candidate-list summary (#3170).

Pinned here:

* tier 4b -- the candidate-list summary is retried COMPACT before the
  fail-closed fallback, so a pass whose indent-2 summary overflows but whose
  compact summary fits keeps its TRUE status (artifact, ``SchedulerPassResult``
  and CLI) and is read by readiness, ``list-operator-actions`` and the node-22
  stall probe exactly like the same pass written in full;
* ``limit.lane_bytes`` -- the fallback names the overflowing top-level lanes,
  added only after the fallback product is fitted, only while ``limit`` still
  carries ``pre_limit_status`` and only in the serialization the product
  already used;
* byte identity -- tiers 1-4a and the fallback product (minus ``lane_bytes``)
  are byte-identical to the ``origin/master`` writer.  The golden bytes in
  ``tests/fixtures/scheduler_evidence_golden_3170.json`` were captured from the
  ``origin/master`` (4b89961c2) writer with the builders below, never by
  importing old code.

Every ``services.*`` / ``tests.*`` / ``scripts.*`` import is function-local, as
in ``tests/test_scheduler_evidence_decidability.py``: the file must not join the
frozen top-level importer set of ``tests.test_production_scheduler``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PASS_ID = "scheduler_2026052112_cccccccccccc"
_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "scheduler_evidence_golden_3170.json"
_FIXED_ARTIFACT_PATH = Path("/golden/evidence") / f"{_PASS_ID}.json"
_N_MODELS_PER_SOURCE = 66
_SOURCES = ("gfs", "IFS")

# Bounds captured together with the golden bytes (each one puts its case on the
# named tier of the ``origin/master`` writer; ``test_golden_cases_reach_their_tier``
# re-proves the tier from the output's own markers).
_GOLDEN_BOUNDS: dict[str, int] = {
    "within_limit": 5_000_000,
    # exactly the admission projection's indent-2 size
    "admission_only": 73_720,
    # exactly the candidate-list summary's indent-2 size
    "summary_indent_fit": 45_615,
    # one byte under the summary's compact size; the product fits indent-2
    "fallback_indent": 216_400,
    # one byte under the product's indent-2 size: written compact
    "fallback_compact": 7_385,
    # one byte over the product's indent-2 size: no room for lane_bytes
    "fallback_no_switch": 7_387,
    # inside the band where only the terminal limit compaction fits
    # (tests/test_production_scheduler.py::test_terminal_limit_compaction_keeps_the_evidence_size_limit_reason_marker)
    "terminal_floor": 1_470,
}


# ---------------------------------------------------------------------------
# Deterministic payload builders (shared with the golden capture)
# ---------------------------------------------------------------------------


def _candidate(model_id: str) -> Any:
    from services.orchestrator import scheduler as scheduler_module
    from tests.test_production_scheduler import _dt
    from workers.data_adapters.base import CycleDiscovery

    return scheduler_module._candidate_for(
        discovery=CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        model=scheduler_module.RegisteredSchedulerModel(
            model_id=model_id,
            basin_id=f"basin_{model_id}",
            basin_version_id=f"basin_{model_id}_v1",
            river_network_version_id=f"basin_{model_id}_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri=f"s3://nhms/models/{model_id}/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )


def _model_run_rows(count: int) -> list[dict[str, Any]]:
    """Writer-produced ``model_run_evidence`` rows (no ``error_code``)."""

    from services.orchestrator.scheduler_candidate_execution_evidence import _candidate_model_run_review_evidence

    rows = []
    for index in range(count):
        row = _candidate_model_run_review_evidence(
            _candidate(f"model_{index:03d}"),
            output_uri=None,
            outcome=None,
            status="submitted",
            stage_statuses=[],
        )
        rows.append({**row, "status": "submitted", "submitted": True, "execution_attempted": True})
    return json.loads(json.dumps(rows))


def golden_payload(case: str) -> dict[str, Any]:
    """The input payload of one golden case; deterministic, no tmp path."""

    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    if case == "terminal_floor":
        return _incident_scheduler_evidence_payload(_PASS_ID)
    payload = _incident_scheduler_evidence_payload(_PASS_ID, status="submitted")
    payload["artifact_path"] = str(_FIXED_ARTIFACT_PATH)
    if case == "within_limit":
        return payload
    if case == "admission_only":
        payload["retention"] = {
            "status": "completed",
            "counts": {"planned": 0, "deleted": 0, "skipped": 400, "failed": 0},
            "skipped": [
                {"key": f"raw/gfs/{index:010d}", "reason": "within_retention_window", "root": "/object-store"}
                for index in range(400)
            ],
        }
        return payload
    if case == "summary_indent_fit":
        payload["candidates"][0]["state_evidence"]["canonical_readiness"] = {
            "missing_leads": [f"lead_{index:04d}" for index in range(2_000)]
        }
        return payload
    if case in {"fallback_indent", "fallback_compact", "fallback_no_switch"}:
        payload["model_run_evidence"] = _model_run_rows(40)
        return payload
    raise KeyError(case)


def _payload_writer() -> Any:
    """``scheduler_evidence_payload``; its ``scheduler_evidence`` half must load first (circular)."""

    # Order matters: ``scheduler_evidence`` first.
    from services.orchestrator import scheduler_evidence, scheduler_evidence_payload  # noqa: F401

    return scheduler_evidence_payload


def _context(max_evidence_bytes: int) -> Any:
    from services.orchestrator import scheduler_evidence

    return SimpleNamespace(
        max_evidence_bytes=max_evidence_bytes,
        bounded_evidence_payload=scheduler_evidence.bounded_evidence_payload,
    )


def write_golden_case(case: str, max_evidence_bytes: int) -> tuple[dict[str, Any], str]:
    """Run one case through the writer's tier ladder with a fixed artifact path."""

    scheduler_evidence_payload = _payload_writer()

    return scheduler_evidence_payload._serialized_evidence_within_limit(
        _context(max_evidence_bytes),
        json.loads(json.dumps(golden_payload(case))),
        artifact_path=_FIXED_ARTIFACT_PATH,
    )


def _golden() -> dict[str, dict[str, Any]]:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


def _is_indent(serialized: str) -> bool:
    return serialized.startswith("{\n")


def _without_lane_bytes(written: dict[str, Any], serialized: str) -> str:
    """Re-serialize ``written`` minus ``limit.lane_bytes`` in the format it was written in."""

    scheduler_evidence_payload = _payload_writer()

    stripped = json.loads(json.dumps(written))
    stripped.get("limit", {}).pop("lane_bytes", None)
    return scheduler_evidence_payload._serialize_evidence_json(stripped, compact=not _is_indent(serialized))


def _tier_four_payload(payload: dict[str, Any], max_evidence_bytes: int) -> dict[str, Any]:
    """The candidate-list summary of ``payload``, built with the PRE-#3170 helpers only."""

    scheduler_evidence_payload = _payload_writer()

    without_circuit = {key: value for key, value in payload.items() if key != "no_progress_circuit"}
    admission = scheduler_evidence_payload._compact_admissible_pass_payload(without_circuit)
    summary = scheduler_evidence_payload._non_blocking_summary_payload(
        admission if admission is not None else without_circuit,
        max_evidence_bytes=max_evidence_bytes,
    )
    assert summary is not None
    return summary


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Byte identity against the origin/master writer (golden)
# ---------------------------------------------------------------------------


def test_golden_bounds_match_the_capture() -> None:
    golden = _golden()
    assert {case: record["max_evidence_bytes"] for case, record in golden.items()} == _GOLDEN_BOUNDS


def test_golden_cases_reach_their_tier() -> None:
    """Each captured case is on the tier its name says, read from the output's own markers."""

    golden = _golden()
    tiers = {}
    for case, record in golden.items():
        persisted = json.loads(record["serialized"])
        compaction = persisted.get("evidence_compaction") or {}
        tiers[case] = (
            persisted["status"],
            compaction.get("mode"),
            "reason" in compaction,
            "limit" in persisted,
            _is_indent(record["serialized"]),
        )
    assert tiers == {
        "within_limit": ("submitted", None, False, False, True),
        "admission_only": ("submitted", None, True, False, True),
        "summary_indent_fit": ("submitted", "non_blocking_summary", True, False, True),
        "fallback_indent": ("resource_limit_blocked", None, False, True, True),
        "fallback_compact": ("resource_limit_blocked", None, False, True, False),
        "fallback_no_switch": ("resource_limit_blocked", None, False, True, True),
        "terminal_floor": ("resource_limit_blocked", None, False, True, False),
    }
    assert json.loads(golden["terminal_floor"]["serialized"])["limit"] == {"reason": "evidence_size_limit_exceeded"}


@pytest.mark.parametrize("case", ["within_limit", "admission_only", "summary_indent_fit"])
def test_tiers_one_to_four_a_are_byte_identical_to_origin_master(case: str) -> None:
    record = _golden()[case]

    _written, serialized = write_golden_case(case, record["max_evidence_bytes"])

    assert serialized == record["serialized"]


@pytest.mark.parametrize(
    "case",
    ["fallback_indent", "fallback_compact", "fallback_no_switch", "terminal_floor"],
)
def test_the_fallback_minus_lane_bytes_is_byte_identical_to_origin_master(case: str) -> None:
    record = _golden()[case]

    written, serialized = write_golden_case(case, record["max_evidence_bytes"])

    assert _is_indent(serialized) == _is_indent(record["serialized"])
    assert _without_lane_bytes(written, serialized) == record["serialized"]
    assert _utf8_len(serialized) <= record["max_evidence_bytes"]


# ---------------------------------------------------------------------------
# limit.lane_bytes (task 1.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["fallback_indent", "fallback_compact"])
def test_the_fallback_names_the_largest_lanes_of_the_tier_four_payload(case: str) -> None:
    record = _golden()[case]
    bound = record["max_evidence_bytes"]

    written, serialized = write_golden_case(case, bound)

    # Independent measure: every top-level value of the candidate-list summary
    # serialized ALONE with indent-2, UTF-8 bytes, top 10 by bytes then key.
    summary = _tier_four_payload(json.loads(json.dumps(golden_payload(case))), bound)
    expected = sorted(
        (
            {"key": key, "bytes": len(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))}
            for key, value in summary.items()
        ),
        key=lambda row: (-row["bytes"], row["key"]),
    )[:10]
    assert written["status"] == "resource_limit_blocked"
    assert written["limit"]["pre_limit_status"] == "submitted"
    assert written["limit"]["lane_bytes"] == expected
    assert expected[0]["key"] == "model_run_evidence"
    assert json.loads(serialized)["limit"]["lane_bytes"] == expected
    assert _utf8_len(serialized) <= bound


def test_lane_bytes_never_switches_an_indent_product_to_compact() -> None:
    """A product that fits indent-2 but not indent-2 WITH ``lane_bytes`` is written without it."""

    scheduler_evidence_payload = _payload_writer()

    record = _golden()["fallback_no_switch"]
    bound = record["max_evidence_bytes"]

    written, serialized = write_golden_case("fallback_no_switch", bound)

    assert _is_indent(serialized)
    assert "lane_bytes" not in written["limit"]
    assert serialized == record["serialized"]
    # The premise, measured independently of the writer's helper: with the
    # attribution the product overflows indent-2 and WOULD fit compact, so
    # omitting it is the rule, not a size accident.
    summary = _tier_four_payload(json.loads(json.dumps(golden_payload("fallback_no_switch"))), bound)
    rows = sorted(
        (
            {"key": key, "bytes": len(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))}
            for key, value in summary.items()
        ),
        key=lambda row: (-row["bytes"], row["key"]),
    )[:10]
    attributed = {**written, "limit": {**written["limit"], "lane_bytes": rows}}
    assert _utf8_len(scheduler_evidence_payload._serialize_evidence_json(attributed)) > bound
    assert _utf8_len(scheduler_evidence_payload._serialize_evidence_json(attributed, compact=True)) <= bound


def test_without_candidate_lists_the_lane_source_is_the_payload_handed_to_tier_four() -> None:
    """No candidate list means no tier-4 payload: ``lane_bytes`` measures the tier-4 input."""

    bound = 50_000
    payload = golden_payload("fallback_indent")
    for field_name in ("candidates", "blocked_candidates", "skipped_candidates"):
        payload.pop(field_name)
    scheduler_evidence_payload = _payload_writer()
    assert scheduler_evidence_payload._non_blocking_summary_payload(payload, max_evidence_bytes=bound) is None
    assert scheduler_evidence_payload._compact_admissible_pass_payload(payload) is None

    written, serialized = scheduler_evidence_payload._serialized_evidence_within_limit(
        _context(bound),
        json.loads(json.dumps(payload)),
        artifact_path=_FIXED_ARTIFACT_PATH,
    )

    expected = sorted(
        (
            {"key": key, "bytes": len(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))}
            for key, value in payload.items()
        ),
        key=lambda row: (-row["bytes"], row["key"]),
    )[:10]
    assert written["status"] == "resource_limit_blocked"
    assert written["limit"]["lane_bytes"] == expected
    assert expected[0]["key"] == "model_run_evidence"
    assert _utf8_len(serialized) <= bound


def test_a_re_bounded_fallback_product_carries_the_original_attribution() -> None:
    """The writer replaces the caller's mapping in place, so a second write can start from a product."""

    scheduler_evidence_payload = _payload_writer()
    bound = _GOLDEN_BOUNDS["fallback_indent"]
    first, _serialized = write_golden_case("fallback_indent", bound)
    original = first["limit"]["lane_bytes"]
    # The caller adds bulk to the replaced mapping and writes it again; twice the
    # original rows, so even the compact candidate-list summary overflows and the
    # fallback runs a second time.
    again_source = {**json.loads(json.dumps(first)), "model_run_evidence": _model_run_rows(80)}

    again, serialized = scheduler_evidence_payload._serialized_evidence_within_limit(
        _context(bound),
        again_source,
        artifact_path=_FIXED_ARTIFACT_PATH,
    )

    assert "evidence_compaction" not in again
    assert again["status"] == "resource_limit_blocked"
    assert again["limit"]["pre_limit_status"] == "resource_limit_blocked"
    assert again["limit"]["lane_bytes"] == original
    # Re-measured, it would have named the re-bounded payload's own lanes.
    assert scheduler_evidence_payload._lane_bytes(_tier_four_payload(again_source, bound)) != original
    assert _utf8_len(serialized) <= bound


def test_the_lane_bytes_measure_is_each_value_serialized_alone() -> None:
    """Pinned: indent-2, sort_keys, ``ensure_ascii`` default, UTF-8 length, value on its own."""

    scheduler_evidence_payload = _payload_writer()

    payload = {
        "a_text": "xy",  # '"xy"'
        "b_mapping": {"z": 1, "y": [1, 2]},  # 39 B: 2+9+7+6+5+9+1 by line
        "c_non_ascii": "é",  # '"\\u00e9"'
        **{f"k{index:02d}": index for index in range(10)},  # '0'..'9'
    }

    rows = scheduler_evidence_payload._lane_bytes(payload)

    assert rows == [
        {"key": "b_mapping", "bytes": 39},
        {"key": "c_non_ascii", "bytes": 8},
        {"key": "a_text", "bytes": 4},
        {"key": "k00", "bytes": 1},
        {"key": "k01", "bytes": 1},
        {"key": "k02", "bytes": 1},
        {"key": "k03", "bytes": 1},
        {"key": "k04", "bytes": 1},
        {"key": "k05", "bytes": 1},
        {"key": "k06", "bytes": 1},
    ]
    # Its nested contribution would be larger (deeper indentation): the value is
    # measured on its own, not inside the document.
    nested = json.dumps({"b_mapping": payload["b_mapping"]}, indent=2, sort_keys=True)
    assert len(nested) - len('{\n  "b_mapping": \n}') > 39


def test_the_terminal_floor_written_through_write_evidence_keeps_a_reason_only_limit(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _config, _dt, _scheduler_evidence_test_context

    config = _config(tmp_path, now=_dt("2026-05-21T12:00:00Z"))
    Path(config.evidence_dir).mkdir(parents=True)
    # ``write_evidence`` adds ``artifact_path`` (a required bounded field, kept
    # verbatim); the floor is compact, so the golden band moves by exactly the
    # compact bytes of that one member.
    artifact_path = scheduler_evidence.artifact_path_evidence(config, Path(config.evidence_dir) / f"{_PASS_ID}.json")
    bound = _GOLDEN_BOUNDS["terminal_floor"] + _utf8_len(f'"artifact_path":{json.dumps(artifact_path)},')

    written_path = scheduler_evidence.write_evidence(
        _scheduler_evidence_test_context(config, max_evidence_bytes=bound),
        _PASS_ID,
        golden_payload("terminal_floor"),
    )
    serialized = Path(written_path or "").read_text(encoding="utf-8")
    persisted = json.loads(serialized)

    assert _utf8_len(serialized) <= bound
    assert persisted["status"] == "resource_limit_blocked"
    assert persisted["limit"] == {"reason": "evidence_size_limit_exceeded"}


# ---------------------------------------------------------------------------
# Tier 4b: compact retry of the candidate-list summary (task 1.2)
# ---------------------------------------------------------------------------


def test_a_summary_that_overflows_indent_but_fits_compact_is_written_compact_with_the_true_status() -> None:
    scheduler_evidence_payload = _payload_writer()

    payload = golden_payload("summary_indent_fit")
    summary = _tier_four_payload(json.loads(json.dumps(payload)), 99_999)
    indent_bytes = _utf8_len(scheduler_evidence_payload._serialize_evidence_json(summary))
    compact_bytes = _utf8_len(scheduler_evidence_payload._serialize_evidence_json(summary, compact=True))
    bound = (indent_bytes + compact_bytes) // 2
    assert compact_bytes < bound < indent_bytes

    written, serialized = scheduler_evidence_payload._serialized_evidence_within_limit(
        _context(bound),
        json.loads(json.dumps(payload)),
        artifact_path=_FIXED_ARTIFACT_PATH,
    )

    assert not _is_indent(serialized)
    assert _utf8_len(serialized) <= bound
    assert written["status"] == "submitted"
    assert "limit" not in written
    assert written["evidence_compaction"]["mode"] == "non_blocking_summary"
    assert written["evidence_compaction"]["summarized_fields"] == [
        "candidates",
        "blocked_candidates",
        "skipped_candidates",
    ]
    assert json.loads(serialized) == written


# ---------------------------------------------------------------------------
# Tier 4b on a 132-candidate submitted pass: writer, result, CLI and readers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def long_submitted_pass(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """A REAL 66-model x 2-source ``run_once()`` pass (132 submits), written twice.

    ``full``: with an unbounded byte limit, so the artifact is the full pass.
    ``tier``: with a limit halfway between the compact and the indent-2 size of
    that pass's candidate-list summary -- measured with the pre-#3170 helpers, so
    the bound is known before the compact retry exists and the red run on the
    old writer is honest (it falls back to ``resource_limit_blocked``).
    """

    scheduler_evidence_payload = _payload_writer()
    from tests.test_production_scheduler import (
        FakeActiveRepository,
        FakeAdapter,
        FakeProductionOrchestratorWithStageEvidence,
        FakeRegistry,
        ProductionScheduler,
        _AlwaysReadyCanonicalReadinessProvider,
        _config,
        _dt,
        _model,
    )

    def _pass(root: Path) -> Any:
        root.mkdir(parents=True, exist_ok=True)
        orchestrator = FakeProductionOrchestratorWithStageEvidence()
        return ProductionScheduler(
            # ``backfill_enabled``: a pass without backfill is ``scope_narrowed``
            # and ``list-operator-actions`` could never read it as evaluating.
            _config(
                root,
                now=_dt("2026-05-21T12:00:00Z"),
                dry_run=False,
                sources=_SOURCES,
                backfill_enabled=True,
                lookback_hours=12,
            ),
            registry=FakeRegistry(
                [_model(f"model_{index:03d}", f"basin_{index:03d}") for index in range(_N_MODELS_PER_SOURCE)]
            ),
            adapters={source: FakeAdapter(source, [("2026-05-21T06:00:00Z", True)]) for source in _SOURCES},
            active_repository=FakeActiveRepository(active=False),
            canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
            orchestrator_factory=lambda _source_id: orchestrator,
        ).run_once()

    tmp_path = tmp_path_factory.mktemp("long_pass").resolve()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", 10**12)
        reference = _pass(tmp_path / "full")
    full = json.loads(json.dumps(reference.evidence))
    assert full["status"] == "submitted"
    assert full["counts"]["submitted_count"] == 2 * _N_MODELS_PER_SOURCE == len(full["model_run_evidence"])
    assert "limit" not in full and "evidence_compaction" not in full

    summary = _tier_four_payload(full, 99_999_999)
    indent_bytes = _utf8_len(scheduler_evidence_payload._serialize_evidence_json(summary))
    compact_bytes = _utf8_len(scheduler_evidence_payload._serialize_evidence_json(summary, compact=True))
    bound = (indent_bytes + compact_bytes) // 2
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", bound)
        tier = _pass(tmp_path / "tier")
    return SimpleNamespace(
        full=full,
        full_root=Path(reference.artifact_path or "").parent,
        tier=tier,
        tier_path=Path(tier.artifact_path or ""),
        bound=bound,
        indent_bytes=indent_bytes,
        compact_bytes=compact_bytes,
    )


def test_a_long_submitted_pass_keeps_its_status_in_the_artifact_result_and_cli(
    long_submitted_pass: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.orchestrator import cli
    from tests.test_production_scheduler import ProductionSchedulerConfig, SchedulerPassResult

    state = long_submitted_pass
    serialized = state.tier_path.read_text(encoding="utf-8")
    persisted = json.loads(serialized)

    assert state.compact_bytes < state.bound < state.indent_bytes
    assert not _is_indent(serialized)
    assert _utf8_len(serialized) <= state.bound
    assert persisted["status"] == "submitted"
    assert state.tier.status == "submitted"
    assert state.tier.evidence["status"] == "submitted"
    assert "limit" not in persisted
    assert persisted["evidence_compaction"]["mode"] == "non_blocking_summary"
    assert persisted["evidence_compaction"]["summarized_fields"] == [
        "candidates",
        "blocked_candidates",
        "skipped_candidates",
    ]
    assert persisted["counts"]["submitted_count"] == len(persisted["model_run_evidence"]) == 132
    assert persisted["model_run_evidence"] == state.tier.evidence["model_run_evidence"]

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
            assert max_passes == 1
            return [state.tier]

    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)
    payload = cli._plan_production(
        sources=_SOURCES,
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=(),
        basin_ids=(),
        dry_run=False,
        continuous=True,
        interval_seconds=300.0,
        max_passes=1,
        workspace_root=str(tmp_path),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "submitted"
    assert payload["passes"][0]["status"] == "submitted"


def test_readiness_reads_the_compact_summary_pass_exactly_like_the_full_pass(
    long_submitted_pass: SimpleNamespace,
) -> None:
    from services.production_closure import readiness_scheduler_evidence

    persisted = json.loads(long_submitted_pass.tier_path.read_text(encoding="utf-8"))
    full_errors = readiness_scheduler_evidence._scheduler_evidence_errors(long_submitted_pass.full)

    assert persisted["status"] == "submitted"
    assert readiness_scheduler_evidence._scheduler_evidence_errors(persisted) == full_errors
    assert not [
        error
        for error in full_errors
        if "submitted_count_model_run_evidence_mismatch" in error
        or "_missing_" in error
        or "_identity_mismatch" in error
    ]


def test_list_operator_actions_reads_the_compact_summary_pass_as_evaluating(
    long_submitted_pass: SimpleNamespace,
) -> None:
    from services.orchestrator.operator_action_listing import list_operator_actions

    full_receipt, full_exit = list_operator_actions(evidence_root=str(long_submitted_pass.full_root), passes=6)
    tier_receipt, tier_exit = list_operator_actions(
        evidence_root=str(long_submitted_pass.tier_path.parent),
        passes=6,
    )

    assert json.loads(long_submitted_pass.tier_path.read_text(encoding="utf-8"))["status"] == "submitted"
    assert tier_exit == full_exit
    assert tier_receipt["operator_actions"] == full_receipt["operator_actions"]
    assert tier_receipt["non_evaluating_passes"] == full_receipt["non_evaluating_passes"] == []


def test_the_stall_probe_counts_the_compact_summary_pass_as_progress(long_submitted_pass: SimpleNamespace) -> None:
    from datetime import timedelta

    from scripts import node22_scheduler_stall_health as probe

    record = probe.read_pass_record(long_submitted_pass.tier_path)

    assert record.status == "submitted"
    assert (record.shape, record.submitted_count) == (probe.SHAPE_PROGRESS, 132)
    assert (
        probe.resource_limit_passes_in_window(
            [record],
            now=record.started_at + timedelta(minutes=1),
            lookback_minutes=120,
        )
        == []
    )
