"""#2539: the db-free forecast runtime manifest conforms to ``run_manifest.schema.json``.

Seam: the REAL ``orchestrate_cycle`` over a REAL ``FileOrchestrationJournalRepository``
(only Slurm is faked), so the validated bytes are exactly what the chain writes to
``runs/<run_id>/input/manifest.json`` and hands to the SHUD sbatch.  One cohort carries
the three initial-condition shapes the builder distinguishes: a warm start (prefilled
state), a cold start (no state at all) and a packaged-IC bootstrap (#1164 scheduler
evidence).  The schema is the independent oracle; the format checker is the one CI's
``check-jsonschema`` uses, so ``date-time`` is enforced the same way here and there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from check_jsonschema.formats import FormatOptions, make_format_checker
from check_jsonschema.regex_variants import RegexImplementation, RegexVariantName

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "run_manifest.schema.json"
_CYCLE = "2026050100"


def _validator() -> jsonschema.Draft7Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    checker = make_format_checker(
        FormatOptions(regex_impl=RegexImplementation(RegexVariantName.default)), schema["$schema"]
    )
    return jsonschema.Draft7Validator(schema, format_checker=checker)


def _violations(manifest: dict[str, Any]) -> list[str]:
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in _validator().iter_errors(manifest)
    )


def _basin(index: int, **extra: Any) -> dict[str, Any]:
    model_id = f"model_{index}"
    return {
        "model_id": model_id,
        "basin_id": f"basin_{index}",
        "basin_version_id": f"basin_v{index}",
        "river_network_version_id": f"river_v{index}",
        "run_id": f"fcst_gfs_{_CYCLE}_{model_id}",
        "candidate_id": f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
        "model_package_uri": f"s3://nhms/models/{model_id}.tar",
        "model_package_checksum": f"sha256:model-{index}",
        **extra,
    }


_WARM = _basin(
    0,
    init_state_id="state_gfs_model_0_2026050100_gfs_2026043012_f012",
    init_state_uri="s3://nhms/states/gfs/model_0/2026050100/state.cfg.ic",
    init_state_checksum="sha256:state-model_0",
    init_state_valid_time="2026-05-01T00:00:00Z",
)
_COLD = _basin(1)
_PACKAGED_IC = _basin(
    2,
    state_evidence={
        "decision": "submit",
        "strict_warm_start": {
            "mode": "db_free_packaged_ic_bootstrap",
            "ready": True,
            "status": "ready",
            "cold_start_reason": None,
            "packaged_ic_checksum": "b" * 64,
        },
    },
)


@pytest.fixture(scope="module")
def built_manifests(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """The three runtime manifests one real db-free forecast cycle writes, keyed by IC shape."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    tmp_path = tmp_path_factory.mktemp("run-manifest-contract")
    orchestrator = _orchestrator(
        tmp_path,
        FileOrchestrationJournalRepository(tmp_path / "journal"),
        FakeCycleSlurmClient(),
        terminal_stage="forecast",
    )
    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, [dict(_WARM), dict(_COLD), dict(_PACKAGED_IC)])
    assert result.status == "succeeded"

    manifests: dict[str, dict[str, Any]] = {}
    for shape, basin in (("warm", _WARM), ("cold", _COLD), ("packaged_ic", _PACKAGED_IC)):
        path = tmp_path / "workspace" / "runs" / basin["run_id"] / "input" / "manifest.json"
        # The object-store copy is the SAME bytes the sbatch reads; check both faces agree.
        object_copy = tmp_path / "object-store" / "runs" / basin["run_id"] / "input" / "manifest.json"
        assert path.read_bytes() == object_copy.read_bytes()
        manifests[shape] = json.loads(path.read_text(encoding="utf-8"))
    return manifests


def test_fixture_covers_the_three_initial_condition_shapes(built_manifests: dict[str, dict[str, Any]]) -> None:
    """Guard the fixture itself: each basin really takes its builder branch."""

    warm, cold, packaged = (built_manifests[shape]["initial_state"] for shape in ("warm", "cold", "packaged_ic"))
    assert warm["state_id"] == _WARM["init_state_id"]
    assert warm["ic_file_uri"] == _WARM["init_state_uri"]
    assert (cold["state_id"], cold["ic_file_uri"], cold["quality"]) == (None, None, "cold_start_no_state")
    assert (packaged["state_id"], packaged["ic_file_uri"]) == (None, None)
    assert packaged["quality"] == "packaged_calibrated_state"
    assert [built_manifests[shape]["runtime"]["init_mode"] for shape in ("warm", "cold", "packaged_ic")] == [3, 1, 3]


@pytest.mark.parametrize("shape", ["warm", "cold", "packaged_ic"])
def test_real_builder_manifest_validates_against_the_schema(
    built_manifests: dict[str, dict[str, Any]], shape: str
) -> None:
    assert _violations(built_manifests[shape]) == []


def test_builder_writes_the_manifest_schema_version(built_manifests: dict[str, dict[str, Any]]) -> None:
    example = json.loads((_SCHEMA_PATH.parent / "examples" / "run_manifest.example.json").read_text())
    for manifest in built_manifests.values():
        assert manifest["schema_version"] == example["schema_version"] == "1.0"


def test_no_written_key_is_removed(built_manifests: dict[str, dict[str, Any]]) -> None:
    """Never break a reader: every top-level key the builder wrote before #2539 is still written."""

    pre_change_keys = {
        "run_id",
        "run_type",
        "submission_attempt",
        "candidate_id",
        "scenario_id",
        "source_id",
        "cycle_time",
        "start_time",
        "end_time",
        "forecast_horizon_hours",
        "workspace_dir",
        "object_store_root",
        "object_store_prefix",
        "identity",
        "model",
        "forcing",
        "initial_state",
        "runtime",
        "outputs",
        "display",
        "quality_states",
        "residual_blockers",
    }
    for manifest in built_manifests.values():
        assert pre_change_keys <= set(manifest)


def test_an_undeclared_top_level_key_fails_the_contract(built_manifests: dict[str, dict[str, Any]]) -> None:
    manifest = {**built_manifests["warm"], "undeclared_contract_drift": True}

    violations = _violations(manifest)

    assert len(violations) == 1
    assert "undeclared_contract_drift" in violations[0]
    assert "Additional properties are not allowed" in violations[0]


def test_the_format_checker_enforces_date_time_like_ci(built_manifests: dict[str, dict[str, Any]]) -> None:
    """The contract test is only as strong as its format checker: a bad timestamp must fail."""

    manifest = {**built_manifests["cold"], "cycle_time": "2026-05-01 00:00"}

    assert _violations(manifest) == ["cycle_time: '2026-05-01 00:00' is not a 'date-time'"]


def test_runtime_manifest_validator_requires_only_schema_declared_fields(tmp_path: Path) -> None:
    """Every field ``validate_forecast_runtime_manifest`` requires is declared by the schema.

    The validator's own refusal of an empty manifest names the full required set.
    """

    from services.orchestrator.chain import OrchestratorError
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    orchestrator = _orchestrator(
        tmp_path, FileOrchestrationJournalRepository(tmp_path / "journal"), FakeCycleSlurmClient()
    )
    manifest_path = orchestrator._workspace_path("runs", "fcst_gfs_2026050100_model_0", "input", "manifest.json")
    orchestrator._safe_workspace_write_bytes(manifest_path, b"{}")

    with pytest.raises(OrchestratorError) as raised:
        orchestrator._validate_forecast_runtime_manifest(manifest_path, {}, task_index=0)

    assert raised.value.error_code == "RUNTIME_MANIFEST_INVALID"
    required = raised.value.details["missing_fields"]
    assert "workspace_dir" in required and "object_store_root" in required
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    for dotted in required:
        node = schema
        for part in dotted.split("."):
            node = node["properties"][part]
