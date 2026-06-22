from __future__ import annotations

import json
from pathlib import Path

from packages.common.forcing_domain_handoff import (
    REASON_PAYLOAD_CHECKSUM_MISSING,
    REASON_PAYLOAD_MISSING,
    REASON_PAYLOAD_OUTSIDE_PACKAGE,
    REASON_PAYLOAD_PATH_UNSAFE,
    REASON_TEMPORAL_FIELD_MISSING,
    validate_forcing_domain_handoff_path,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "forcing_domain_handoff"
SCHEMA_PATH = Path("schemas/forcing_domain_handoff.schema.json")


def _manifest_path(case: str, run_id: str) -> Path:
    return FIXTURE_ROOT / case / "object-store" / "runs" / run_id / "input" / "forcing_domain_handoff.json"


def _object_store_root(case: str) -> Path:
    return FIXTURE_ROOT / case / "object-store"


def _validate(case: str, run_id: str) -> dict[str, object]:
    return validate_forcing_domain_handoff_path(
        _manifest_path(case, run_id),
        object_store_root=_object_store_root(case),
    )


def _reason_codes(result: dict[str, object]) -> set[str]:
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    return {str(reason["code"]) for reason in reasons}


def _tree_snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_complete_fixture_validates_identity_checksums_station_count_and_table_rows() -> None:
    result = _validate("complete", "fcst_gfs_2026062012_basins_qhh_shud")

    assert result["available"] is True
    assert result["status"] == "available"
    assert result["unavailable_reasons"] == []

    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["identity"] == {
        "run_id": "fcst_gfs_2026062012_basins_qhh_shud",
        "source_id": "GFS",
        "source": "gfs",
        "model_id": "basins_qhh_shud",
        "basin_id": "qhh",
        "basin_version_id": "basins_qhh_v2026_06",
        "forcing_version_id": "forc_gfs_2026062012_basins_qhh_shud",
        "scenario_id": "forecast_gfs_deterministic",
    }
    assert evidence["temporal_bounds"] == {
        "cycle_time": "2026-06-20T12:00:00Z",
        "start_time": "2026-06-20T12:00:00Z",
        "end_time": "2026-06-27T12:00:00Z",
    }
    assert evidence["forcing_version"] == {
        "forcing_version_id": "forc_gfs_2026062012_basins_qhh_shud",
        "forcing_package_uri": "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/forcing_domain_package.json",
        "checksum_sha256": "c143f229e5eec98047ca92da8de0fd3d223d2091f224407646e71819fbdd3f8b",
        "station_count": 2,
    }
    assert evidence["table_row_counts"] == {
        "met.forcing_version": 1,
        "met.met_station": 2,
        "met.forcing_station_timeseries": 4,
        "met.interp_weight": 4,
    }

    payloads = evidence["payloads"]
    assert isinstance(payloads, dict)
    assert payloads["station_inventory"]["actual_checksum_sha256"] == (
        "c3fe04fb5757b71a48df66930b833ebc1c05abad73bbc3948e8f42f4385c5b1d"
    )
    assert payloads["station_inventory"]["actual_row_count"] == 2
    assert payloads["station_timeseries"]["actual_checksum_sha256"] == (
        "fa12eae314c2c7654d799fbce0edea0d81f6bcb668fe8031ce0618638e9c3390"
    )
    assert payloads["station_timeseries"]["actual_row_count"] == 4
    assert payloads["interpolation_weights"]["actual_checksum_sha256"] == (
        "e10613530de21585eb4b81cec9272581812fe418f7c0e33aaec4c90a2fc4fe20"
    )
    assert payloads["interpolation_weights"]["actual_row_count"] == 4


def test_incomplete_fixture_returns_stable_reasons_and_redacts_credentials() -> None:
    result = _validate("incomplete", "fcst_gfs_2026062012_basins_qhh_shud_incomplete")

    assert result["available"] is False
    assert {
        REASON_TEMPORAL_FIELD_MISSING,
        REASON_PAYLOAD_CHECKSUM_MISSING,
        REASON_PAYLOAD_MISSING,
    }.issubset(_reason_codes(result))

    serialized = json.dumps(result, sort_keys=True)
    assert "user:pass@" not in serialized
    assert "token=secret" not in serialized
    assert "s3://nhms/runs/fcst_gfs_2026062012_basins_qhh_shud_incomplete/input/manifest.json" in serialized


def test_path_safety_rejects_traversal_and_sibling_package_payloads() -> None:
    result = _validate("unsafe", "fcst_gfs_2026062012_basins_qhh_shud_unsafe")

    assert result["available"] is False
    assert {REASON_PAYLOAD_PATH_UNSAFE, REASON_PAYLOAD_OUTSIDE_PACKAGE}.issubset(_reason_codes(result))


def test_validation_helper_does_not_write_files() -> None:
    root = _object_store_root("incomplete")
    before = _tree_snapshot(root)

    result = _validate("incomplete", "fcst_gfs_2026062012_basins_qhh_shud_incomplete")

    assert result["available"] is False
    assert _tree_snapshot(root) == before


def test_schema_names_required_downstream_compatibility_and_table_fields() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert set(schema["required"]) >= {
        "run_id",
        "source_id",
        "source",
        "cycle_time",
        "start_time",
        "end_time",
        "model_id",
        "basin_id",
        "basin_version_id",
        "model_package_uri",
        "forcing_version_id",
        "forcing_uri",
        "forcing_package_uri",
        "forcing_package_checksum_sha256",
        "scenario_id",
        "run_manifest_uri",
        "output_uri",
    }
    assert set(schema["properties"]["table_row_counts"]["required"]) == {
        "met.forcing_version",
        "met.met_station",
        "met.forcing_station_timeseries",
        "met.interp_weight",
    }
    payload_properties = schema["properties"]["payloads"]["properties"]
    assert payload_properties["station_inventory"]["allOf"][1]["properties"]["table"]["const"] == "met.met_station"
    assert (
        payload_properties["station_timeseries"]["allOf"][1]["properties"]["table"]["const"]
        == "met.forcing_station_timeseries"
    )
    assert payload_properties["interpolation_weights"]["allOf"][1]["properties"]["table"]["const"] == (
        "met.interp_weight"
    )
