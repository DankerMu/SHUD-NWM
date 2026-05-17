from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.met_validation import ProductionMetConfig, validate_met


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_validate_met_default_lane_writes_required_evidence_and_redacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_MET_OBJECT_PREFIX", "s3://nhms-prod/met")
    monkeypatch.setenv("NHMS_PRODUCTION_MET_GFS_ENDPOINT", "https://user:pass@example.invalid/gfs?token=secret")
    monkeypatch.setenv("CDSAPI_KEY", "super-secret-token")

    exit_code = slurm_validation.main(
        ["validate-met", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_149"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_149" / "met"
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert summary["evidence_dir"] == str(lane_dir)
    assert summary["object_prefix"] == "s3://nhms-prod/met/runs/m10_149/met"
    assert "raw_cycle_manifest.json" in summary["files"]
    assert sorted(summary["files"]) == sorted(path.name for path in lane_dir.glob("*.json") if path.is_file())

    preflight = _read_json(lane_dir / "preflight.json")
    assert preflight["enabled_sources"] == ["GFS", "IFS", "ERA5"]
    assert preflight["cached_fallback_policy"] == "deterministic_fixture"
    assert preflight["selected_model"]["selection_mode"] == "deterministic_model_fixture"
    assert preflight["cldas"]["status"] == "restricted"
    assert preflight["bounds"]["max_manifest_entries"] == 64

    source_config = _read_json(lane_dir / "source_config.json")
    source_modes = {source["source"]: source["execution_mode"] for source in source_config["sources"]}
    assert source_modes == {
        "GFS": "deterministic_fixture",
        "IFS": "deterministic_fixture",
        "ERA5": "deterministic_fixture",
        "CLDAS": "restricted",
    }
    source_statuses = {source["source"]: source["status"] for source in source_config["sources"]}
    assert source_statuses["CLDAS"] == "restricted"

    raw = _read_json(lane_dir / "raw_cycle_manifest.json")
    assert raw["status"] == "ready"
    assert raw["total_file_count"] == 49
    gfs = next(source for source in raw["sources"] if source["source"] == "GFS")
    assert gfs["status"] == "available"
    assert gfs["file_count"] == 15
    assert gfs["selected_forecast_hours"] == [0, 3]
    assert gfs["retry_count"] == 0
    assert gfs["object_uri"].startswith("s3://nhms-prod/met/runs/m10_149/met/raw/gfs/")
    assert len(gfs["checksums"]) > 1

    canonical = _read_json(lane_dir / "canonical_products.json")
    assert canonical["status"] == "ready"
    assert canonical["product_count"] == 14
    product = canonical["products"][0]
    assert set(product) >= {"source_cycle", "variable", "unit", "time_axis", "object_uri", "checksum", "lineage"}
    assert canonical["failure_checks"][0]["status"] == "blocked"
    assert canonical["failure_checks"][0]["downstream_forcing_ready"] is False

    forcing = _read_json(lane_dir / "forcing_manifest.json")
    assert forcing["status"] == "forcing_ready"
    assert forcing["forcing_package_uri"].startswith("s3://nhms-prod/met/runs/m10_149/met/forcing/gfs/")
    qc = _read_json(lane_dir / "forcing_qc.json")
    assert qc["status"] == "pass"
    assert qc["required_variables"]["missing"] == []
    assert qc["continuity"]["status"] == "pass"
    assert all(check["status"] == "pass" for check in qc["range_checks"])

    lineage = _read_json(lane_dir / "best_available_lineage.json")
    assert lineage["status"] == "ready"
    assert all(item["selected_source"] == "GFS" for item in lineage["per_valid_time"])
    assert any(
        item["source"] == "CLDAS" and item["execution_mode"] == "restricted"
        for item in lineage["skipped_or_restricted_sources"]
    )

    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.glob("*.json"))
    assert "super-secret-token" not in evidence_text
    assert "token=secret" not in evidence_text
    assert "user:pass@" not in evidence_text
    assert "https://example.invalid/gfs" in evidence_text


def test_validate_met_rejects_sensitive_object_prefix_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_MET_OBJECT_PREFIX", "s3://nhms-prod/met?token=secret")

    try:
        exit_code = slurm_validation.main(
            ["validate-met", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "badprefix"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "badprefix").exists()


def test_validate_met_live_gate_does_not_claim_success_without_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_MET_ALLOW_LIVE_NETWORK", "1")
    monkeypatch.setenv("NHMS_PRODUCTION_MET_LIVE_GFS", "1")

    summary = validate_met(
        ProductionMetConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="livegate", sources="GFS")
    )

    lane_dir = tmp_path / "artifacts" / "livegate" / "met"
    assert summary["status"] == "blocked"
    source_config = _read_json(lane_dir / "source_config.json")
    gfs = next(source for source in source_config["sources"] if source["source"] == "GFS")
    assert gfs["execution_mode"] == "not_executed"
    raw = _read_json(lane_dir / "raw_cycle_manifest.json")
    raw_gfs = next(source for source in raw["sources"] if source["source"] == "GFS")
    assert raw_gfs["status"] == "unavailable"
    assert raw_gfs["file_count"] == 0


@pytest.mark.parametrize(
    ("env_name", "env_value", "expected_error"),
    [
        ("NHMS_PRODUCTION_MET_MAX_MANIFEST_ENTRIES", "3", "PRODUCTION_MET_MANIFEST_ENTRY_LIMIT_EXCEEDED"),
        ("NHMS_PRODUCTION_MET_MAX_FORECAST_HOURS", "1", "PRODUCTION_MET_FORECAST_HOURS_EXCEED_LIMIT"),
        ("NHMS_PRODUCTION_MET_MAX_DETERMINISTIC_FILE_BYTES", "4096", "PRODUCTION_MET_FILE_BYTE_LIMIT_EXCEEDED"),
    ],
)
def test_validate_met_bounds_block_before_unbounded_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
    expected_error: str,
) -> None:
    monkeypatch.setenv(env_name, env_value)
    config = ProductionMetConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="bounded")

    with pytest.raises(Exception) as exc_info:
        validate_met(config)

    assert expected_error in str(getattr(exc_info.value, "error_code", exc_info.value))
    assert not (tmp_path / "artifacts" / "bounded" / "met" / "local-object-store").exists()


def test_validate_met_rejects_path_escape_before_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    try:
        exit_code = slurm_validation.main(
            ["validate-met", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "../escape"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_MET_RUN_ID_UNSAFE" in capsys.readouterr().err
    assert not (tmp_path / "artifacts").exists()


def test_validate_met_same_run_requires_force_and_force_replaces_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["validate-met", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "rerun"]
    assert slurm_validation.main(args) == 0
    capsys.readouterr()

    try:
        exit_code = slurm_validation.main(args)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_MET_OBJECT_BUNDLE_EXISTS" in capsys.readouterr().err
    assert slurm_validation.main([*args, "--force"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "ready"


def test_validate_met_disabled_sources_record_skipped_without_success(tmp_path: Path) -> None:
    summary = validate_met(
        ProductionMetConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="gfsonly", sources="GFS")
    )
    lane_dir = tmp_path / "artifacts" / "gfsonly" / "met"

    assert summary["status"] == "ready"
    raw = _read_json(lane_dir / "raw_cycle_manifest.json")
    modes = {source["source"]: source["execution_mode"] for source in raw["sources"]}
    assert modes["GFS"] == "deterministic_fixture"
    assert modes["IFS"] == "skipped"
    assert modes["ERA5"] == "skipped"
    assert modes["CLDAS"] == "restricted"
    assert next(source for source in raw["sources"] if source["source"] == "IFS")["file_count"] == 0


def test_argparse_validate_met_fallback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = slurm_validation._argparse_main(
        ["validate-met", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "argparse"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "ready"
    assert (tmp_path / "artifacts" / "argparse" / "met" / "summary.json").exists()
