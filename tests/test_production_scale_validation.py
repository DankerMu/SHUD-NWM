from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.scale_validation import (
    MAX_EVIDENCE_PAYLOAD_BYTES,
    EvidenceWriter,
    ProductionScaleConfig,
    ProductionScaleValidationError,
    _argparse_main,
    validate_scale,
)


def test_validate_scale_default_lane_writes_required_ready_evidence(tmp_path: Path) -> None:
    summary = validate_scale(ProductionScaleConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_151"))

    lane_dir = tmp_path / "artifacts" / "m10_151" / "scale"
    assert summary["status"] == "ready"
    assert summary["dataset_source"] == "deterministic_large_fixture"
    assert summary["production_mvt_readiness_claimed"] is False
    assert summary["live_db_executed"] is False
    assert summary["live_api_executed"] is False
    assert summary["live_frontend_executed"] is False

    for name in [
        "preflight.json",
        "dataset_manifest.json",
        "thresholds.json",
        "query_latency_evidence.json",
        "tile_evidence.json",
        "frontend_large_layer_evidence.json",
        "resource_bounds_evidence.json",
        "environment.json",
        "summary.json",
    ]:
        assert (lane_dir / name).is_file()

    preflight = _read_json(lane_dir / "preflight.json")
    assert preflight["evidence_dir"] == str(lane_dir)
    assert preflight["minimum_counts"] == {"model_count": 16, "segment_count": 100000}
    assert preflight["tile_content_type_expectation"] == "application/geo+json"
    assert preflight["execution_policy"]["postgis_required"] is False
    assert preflight["execution_policy"]["mvt_encoder_required"] is False

    dataset = _read_json(lane_dir / "dataset_manifest.json")
    assert dataset["status"] == "ready"
    assert dataset["segment_count"] == 125000
    assert dataset["model_count"] == 32
    assert dataset["checksum"]
    assert dataset["crs"].startswith("EPSG:4490")
    assert set(dataset["bbox_sizes"]) == {"national", "urban", "yangtze"}

    thresholds = _read_json(lane_dir / "thresholds.json")
    assert thresholds["version"] == "m10-scale-thresholds-v1"
    assert thresholds["p95_query_targets_ms"]["river_bbox_ms"] == 250.0
    assert thresholds["pass_fail_semantics"]["malformed_or_non_finite_samples"] == "blocked"

    tile = _read_json(lane_dir / "tile_evidence.json")
    assert tile["status"] == "ready"
    assert tile["geojson_compatibility_mode"] is True
    assert tile["production_mvt_readiness_claimed"] is False
    assert tile["observed_content_type"] == "application/json"


def test_validate_scale_mvt_expectation_creates_explicit_release_blocker(tmp_path: Path) -> None:
    summary = validate_scale(
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="mvt_blocked",
            tile_content_type_expectation="application/x-protobuf",
        )
    )

    lane_dir = tmp_path / "artifacts" / "mvt_blocked" / "scale"
    tile = _read_json(lane_dir / "tile_evidence.json")
    assert summary["status"] == "blocked"
    assert tile["status"] == "blocked"
    assert tile["production_mvt_readiness_claimed"] is False
    blocker = tile["blockers"][0]
    assert blocker["error_code"] == "PRODUCTION_SCALE_MVT_DELIVERY_BLOCKED"
    assert blocker["expected_content_type"] == "application/x-protobuf"
    assert "production MVT readiness is not achieved" in blocker["message"]
    assert "/api/v1/tiles/flood-return-period" in blocker["affected_endpoints"]


def test_validate_scale_threshold_and_count_failures_block_readiness(tmp_path: Path) -> None:
    threshold_path = tmp_path / "thresholds.json"
    threshold_path.write_text(
        json.dumps(
            {
                "version": "strict-test",
                "minimum_counts": {"segment_count": 200000, "model_count": 40},
                "p95_query_targets_ms": {"river_bbox_ms": 100.0},
            }
        ),
        encoding="utf-8",
    )

    summary = validate_scale(
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="threshold_fail",
            thresholds_file=threshold_path,
        )
    )

    lane_dir = tmp_path / "artifacts" / "threshold_fail" / "scale"
    dataset = _read_json(lane_dir / "dataset_manifest.json")
    query = _read_json(lane_dir / "query_latency_evidence.json")
    error_codes = {blocker["error_code"] for blocker in summary["blockers"]}
    assert summary["status"] == "blocked"
    assert dataset["status"] == "blocked"
    assert query["status"] == "blocked"
    assert "PRODUCTION_SCALE_SEGMENT_COUNT_BELOW_THRESHOLD" in error_codes
    assert "PRODUCTION_SCALE_MODEL_COUNT_BELOW_THRESHOLD" in error_codes
    assert "PRODUCTION_SCALE_QUERY_P95_THRESHOLD_EXCEEDED" in error_codes


def test_validate_scale_query_latency_records_p95_and_blocks_malformed_samples(tmp_path: Path) -> None:
    validate_scale(ProductionScaleConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="latency_ok"))
    query = _read_json(tmp_path / "artifacts" / "latency_ok" / "scale" / "query_latency_evidence.json")
    river_bbox = next(item for item in query["queries"] if item["query"] == "river_bbox")
    assert river_bbox["latency_samples_ms"] == [158.0, 165.0, 171.0, 168.0, 174.0]
    assert river_bbox["p95_ms"] == 173.4
    assert river_bbox["threshold_ms"] == 250.0
    assert river_bbox["threshold_passed"] is True
    assert river_bbox["plan_hash"]
    assert query["live_db_executed"] is False
    assert query["live_api_executed"] is False

    summary = validate_scale(
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="latency_bad",
            latency_fixture="non_finite",
        )
    )
    assert summary["status"] == "blocked"
    assert any(
        blocker["error_code"] == "PRODUCTION_SCALE_LATENCY_SAMPLE_INVALID"
        for blocker in _read_json(tmp_path / "artifacts" / "latency_bad" / "scale" / "query_latency_evidence.json")[
            "blockers"
        ]
    )


def test_validate_scale_tile_metadata_plan_uses_published_tile_tables(tmp_path: Path) -> None:
    validate_scale(ProductionScaleConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="tile_plan"))

    query = _read_json(tmp_path / "artifacts" / "tile_plan" / "scale" / "query_latency_evidence.json")
    tile_metadata = next(item for item in query["queries"] if item["query"] == "tile_metadata")
    assert "map.tile_layer" in tile_metadata["plan_text"]
    assert "map.tile_cache" in tile_metadata["plan_text"]
    assert "map.tile_manifest" not in tile_metadata["plan_text"]
    assert tile_metadata["index_usage_recorded"] is True


def test_validate_scale_rejects_oversized_thresholds_file_before_json_parse(tmp_path: Path) -> None:
    threshold_path = tmp_path / "thresholds.json"
    threshold_path.write_bytes(b"{" + (b" " * MAX_EVIDENCE_PAYLOAD_BYTES))

    with pytest.raises(ProductionScaleValidationError) as exc_info:
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="oversized_thresholds",
            thresholds_file=threshold_path,
        )

    assert exc_info.value.error_code == "PRODUCTION_SCALE_THRESHOLDS_INVALID"
    assert "exceeds configured limit" in exc_info.value.message


@pytest.mark.parametrize(
    "payload",
    [
        {"minimum_counts": {"segment_count": "many"}},
        {"minimum_counts": {"model_count": 0}},
        {"max_tile_bytes": "large"},
        {"frontend_budgets": {"load_ms": 0}},
        {"frontend_budgets": {"render_ms": "fast"}},
        {"frontend_budgets": {"timeline_ms": -1}},
        {"frontend_budgets": {"chart_ms": False}},
        {"frontend_budgets": {"memory_mb": None}},
        {"object_listing_limit": 1.5},
    ],
)
def test_validate_scale_threshold_integer_fields_raise_stable_errors(tmp_path: Path, payload: dict) -> None:
    threshold_path = tmp_path / "thresholds.json"
    threshold_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProductionScaleValidationError) as exc_info:
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="bad_thresholds",
            thresholds_file=threshold_path,
        )

    assert exc_info.value.error_code == "PRODUCTION_SCALE_THRESHOLDS_INVALID"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("min_segment_count", 0),
        ("min_segment_count", -1),
        ("min_model_count", 0),
        ("min_model_count", -1),
    ],
)
def test_validate_scale_explicit_min_count_overrides_raise_stable_errors(
    tmp_path: Path,
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ProductionScaleValidationError) as exc_info:
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="bad_min_override",
            **{field_name: value},
        )

    assert exc_info.value.error_code == "PRODUCTION_SCALE_CONFIG_INVALID"
    assert f"{field_name} must be positive" in exc_info.value.message


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--min-segment-count", "0"),
        ("--min-segment-count", "-1"),
        ("--min-model-count", "0"),
        ("--min-model-count", "-1"),
    ],
)
def test_validate_scale_cli_explicit_min_count_overrides_fail_stably(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    exit_code = _argparse_main(
        [
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "bad_cli_min_override",
            flag,
            value,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PRODUCTION_SCALE_CONFIG_INVALID" in captured.err
    assert "must be positive" in captured.err


def test_scale_evidence_writer_rejects_files_outside_current_lane(tmp_path: Path) -> None:
    config = ProductionScaleConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="lane_only")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()

    with pytest.raises(ProductionScaleValidationError) as exc_info:
        writer.write_json(config.evidence_root / config.run_id / "outside_scale.json", {"status": "unsafe"})

    assert exc_info.value.error_code == "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE"


def test_validate_scale_frontend_evidence_records_breakpoints_and_no_live_claim(tmp_path: Path) -> None:
    validate_scale(
        ProductionScaleConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="frontend",
            frontend_breakpoints="desktop:1366x768,mobile:375x812",
        )
    )
    frontend = _read_json(tmp_path / "artifacts" / "frontend" / "scale" / "frontend_large_layer_evidence.json")
    assert frontend["status"] == "ready"
    assert frontend["live_frontend_executed"] is False
    assert frontend["mock_only_live_readiness_claimed"] is False
    assert [item["breakpoint"] for item in frontend["breakpoints"]] == ["desktop:1366x768", "mobile:375x812"]
    assert {item["mode"] for item in frontend["breakpoints"]} == {"desktop", "mobile"}
    assert frontend["recoverable_states"]["oversized_or_unavailable_breaks_page"] is False


def test_validate_scale_run_id_idempotency_force_path_safety_and_redaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["--evidence-root", str(tmp_path / "artifacts"), "--run-id", "rerun"]
    assert _argparse_main(args) == 0
    assert _argparse_main(args) == 1
    assert "PRODUCTION_SCALE_EVIDENCE_EXISTS" in capsys.readouterr().err
    assert _argparse_main([*args, "--force"]) == 0

    with pytest.raises(ProductionScaleValidationError) as exc_info:
        ProductionScaleConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="../escape")
    assert exc_info.value.error_code == "PRODUCTION_SCALE_RUN_ID_UNSAFE"

    symlink_root = tmp_path / "symlink-root"
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    symlink_root.symlink_to(target_root, target_is_directory=True)
    with pytest.raises(ProductionScaleValidationError) as symlink_exc:
        ProductionScaleConfig.from_env(evidence_root=symlink_root, run_id="safe")
    assert symlink_exc.value.error_code == "PRODUCTION_SCALE_EVIDENCE_SYMLINK"

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    exit_code = slurm_validation._argparse_main(
        [
            "validate-scale",
            "--evidence-root",
            str(tmp_path / "redacted"),
            "--run-id",
            "redacted",
            "--object-prefix",
            "s3://bucket/prefix/password=supersecret",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "supersecret" not in captured.out
    assert "supersecret" not in captured.err
    assert "PRODUCTION_SCALE_OBJECT_PREFIX_UNSAFE" in captured.err

    exit_code = slurm_validation._argparse_main(
        [
            "validate-scale",
            "--evidence-root",
            str(tmp_path / "redacted"),
            "--run-id",
            "redacted",
            "--api-base-url",
            "https://api.example",
        ]
    )
    assert exit_code == 0
    assert "supersecret" not in capsys.readouterr().out
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "redacted" / "redacted" / "scale").glob("*.json")
    )
    assert "supersecret" not in evidence_text
    assert "[redacted]" in evidence_text


def test_validate_scale_click_and_argparse_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    click_exit = slurm_validation._click_main(
        [
            "validate-scale",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "click",
        ]
    )
    assert click_exit == 0
    click_summary = json.loads(capsys.readouterr().out)
    assert click_summary["status"] == "ready"

    argparse_exit = _argparse_main(
        [
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "argparse",
        ]
    )
    assert argparse_exit == 0
    argparse_summary = json.loads(capsys.readouterr().out)
    assert argparse_summary["status"] == "ready"

    combined_argparse_exit = slurm_validation._argparse_main(
        [
            "validate-scale",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "combined",
        ]
    )
    assert combined_argparse_exit == 0
    combined_summary = json.loads(capsys.readouterr().out)
    assert combined_summary["schema"] == "nhms.production_closure.scale.v1"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
