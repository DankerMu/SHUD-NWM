from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from packages.common.forcing_domain_handoff import (
    FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
    FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    FORCING_PACKAGE_MANIFEST_URI_FIELD,
    REASON_COMPATIBILITY_URI_MISMATCH,
    REASON_COMPATIBILITY_URI_UNSAFE,
    REASON_FIELD_MISSING,
    REASON_IDENTITY_FIELD_MISSING,
    REASON_IDENTITY_MISMATCH,
    REASON_INTERP_WEIGHT_DUPLICATE,
    REASON_OBJECT_STORE_ROOT_UNAVAILABLE,
    REASON_PACKAGE_CHECKSUM_MISMATCH,
    REASON_PAYLOAD_CHECKSUM_MISMATCH,
    REASON_PAYLOAD_MALFORMED,
    REASON_PAYLOAD_MISSING,
    REASON_PAYLOAD_OUTSIDE_PACKAGE,
    REASON_PAYLOAD_PATH_UNSAFE,
    REASON_ROW_COUNT_MISMATCH,
    REASON_STATION_COUNT_MISMATCH,
    REASON_STATION_INVENTORY_DUPLICATE,
    REASON_STATION_TIMESERIES_VARIABLE_DUPLICATE,
    REASON_TEMPORAL_FIELD_MALFORMED,
    REASON_TEMPORAL_FIELD_MISSING,
    REASON_TEMPORAL_WINDOW_INVALID,
    REASON_TIMESERIES_LATTICE_DUPLICATE,
    REASON_TIMESERIES_LATTICE_EXTRA,
    REASON_TIMESERIES_LATTICE_MISSING,
    REASON_TIMESERIES_LATTICE_TOO_LARGE,
    validate_forcing_domain_handoff_path,
)
from packages.common.object_store import sha256_bytes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "forcing_domain_handoff"
SCHEMA_PATH = Path("schemas/forcing_domain_handoff.schema.json")
EXAMPLE_PATH = Path("schemas/examples/forcing_domain_handoff.example.json")
COMPLETE_RUN_ID = "fcst_gfs_2026062012_basins_qhh_shud"
FORCING_DOMAIN_PACKAGE_MANIFEST_RELATIVE_PATH = (
    "forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/forcing_domain_package.json"
)
FORCING_PACKAGE_MANIFEST_RELATIVE_PATH = (
    "forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/forcing_package.json"
)
PAYLOAD_TABLES = {
    "station_inventory": "met.met_station",
    "station_timeseries": "met.forcing_station_timeseries",
    "interpolation_weights": "met.interp_weight",
}


def _manifest_path(case: str, run_id: str) -> Path:
    return FIXTURE_ROOT / case / "object-store" / "runs" / run_id / "input" / "forcing_domain_handoff.json"


def _object_store_root(case: str) -> Path:
    return FIXTURE_ROOT / case / "object-store"


def _validate(case: str, run_id: str) -> dict[str, object]:
    return validate_forcing_domain_handoff_path(
        _manifest_path(case, run_id),
        object_store_root=_object_store_root(case),
    )


def _validate_case_root(case_root: Path, run_id: str = COMPLETE_RUN_ID) -> dict[str, object]:
    return validate_forcing_domain_handoff_path(
        case_root / "object-store" / "runs" / run_id / "input" / "forcing_domain_handoff.json",
        object_store_root=case_root / "object-store",
    )


def _copy_complete_case(tmp_path: Path) -> Path:
    target = tmp_path / "complete"
    shutil.copytree(FIXTURE_ROOT / "complete", target)
    return target


def _reason_codes(result: dict[str, object]) -> set[str]:
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    return {str(reason["code"]) for reason in reasons}


def _assert_bounded_row_diagnostics(result: dict[str, object], role: str) -> None:
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    serialized = json.dumps(reasons, sort_keys=True)
    assert "rows[99]" not in serialized
    assert len(reasons) < 25

    row_reasons = [
        reason
        for reason in reasons
        if isinstance(reason, dict) and reason.get("role") == role and ".rows." in str(reason.get("field"))
    ]
    assert row_reasons
    assert all(int(reason.get("occurrence_count", 0)) >= 1 for reason in row_reasons)
    assert all(len(reason.get("samples", [])) <= 5 for reason in row_reasons)


def _tree_snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _forcing_domain_package_manifest_path(case_root: Path) -> Path:
    return case_root / "object-store" / FORCING_DOMAIN_PACKAGE_MANIFEST_RELATIVE_PATH


def _forcing_package_manifest_path(case_root: Path) -> Path:
    return case_root / "object-store" / FORCING_PACKAGE_MANIFEST_RELATIVE_PATH


def _handoff_manifest_path(case_root: Path, run_id: str = COMPLETE_RUN_ID) -> Path:
    return case_root / "object-store" / "runs" / run_id / "input" / "forcing_domain_handoff.json"


def _payload_path(case_root: Path, filename: str) -> Path:
    return (
        case_root
        / "object-store"
        / "forcing"
        / "gfs"
        / "2026062012"
        / "basins_qhh_v2026_06"
        / "basins_qhh_shud"
        / "payloads"
        / filename
    )


def _sync_forcing_domain_package_manifest_checksum(case_root: Path) -> None:
    package_manifest_path = _forcing_domain_package_manifest_path(case_root)
    checksum = sha256_bytes(package_manifest_path.read_bytes())
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD] = checksum
    _write_json(_handoff_manifest_path(case_root), handoff)


def _sync_forcing_package_manifest_checksum(case_root: Path) -> None:
    checksum = sha256_bytes(_forcing_package_manifest_path(case_root).read_bytes())
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD] = checksum
    _write_json(_handoff_manifest_path(case_root), handoff)


def _sync_payload_ref(case_root: Path, role: str, filename: str) -> None:
    checksum = sha256_bytes(_payload_path(case_root, filename).read_bytes())
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    payload = payloads[role]
    assert isinstance(payload, dict)
    payload["checksum_sha256"] = checksum
    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_payloads = package_manifest["payloads"]
    assert isinstance(package_payloads, dict)
    package_payload = package_payloads[role]
    assert isinstance(package_payload, dict)
    package_payload["checksum_sha256"] = checksum
    _write_json(_handoff_manifest_path(case_root), handoff)
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)


def _set_payload_rows(case_root: Path, role: str, filename: str, rows: list[object]) -> None:
    _payload_path(case_root, filename).write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    row_count = len(rows)
    table = PAYLOAD_TABLES[role]
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    payload = payloads[role]
    assert isinstance(payload, dict)
    payload["row_count"] = row_count
    table_counts = handoff["table_row_counts"]
    assert isinstance(table_counts, dict)
    table_counts[table] = row_count
    _write_json(_handoff_manifest_path(case_root), handoff)

    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_payloads = package_manifest["payloads"]
    assert isinstance(package_payloads, dict)
    package_payload = package_payloads[role]
    assert isinstance(package_payload, dict)
    package_payload["row_count"] = row_count
    package_table_counts = package_manifest["table_row_counts"]
    assert isinstance(package_table_counts, dict)
    package_table_counts[table] = row_count
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_payload_ref(case_root, role, filename)


def _set_station_timeseries_rows(case_root: Path, rows: list[object]) -> None:
    _set_payload_rows(case_root, "station_timeseries", "station_timeseries.json", rows)


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
        "end_time": "2026-06-20T15:00:00Z",
    }
    assert evidence["forcing_version"] == {
        "forcing_version_id": "forc_gfs_2026062012_basins_qhh_shud",
        "forcing_package_uri": "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/",
        "forcing_package_manifest_uri": (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/"
            "basins_qhh_shud/forcing_package.json"
        ),
        "forcing_package_manifest_checksum_sha256": (
            "7d4251776311e114cb3fe1a3a832abf88200297c2af4f8d571fa0a90877ab7f5"
        ),
        "forcing_domain_package_manifest_uri": (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/"
            "basins_qhh_shud/forcing_domain_package.json"
        ),
        "forcing_domain_package_manifest_checksum_sha256": (
            "3e0762b2d559777102fc7c5e3cd35a7ab0039c06c9a99e8ca3564cec1b5b8510"
        ),
        "station_count": 2,
    }
    assert evidence["table_row_counts"] == {
        "met.forcing_version": 1,
        "met.met_station": 2,
        "met.forcing_station_timeseries": 8,
        "met.interp_weight": 4,
    }

    payloads = evidence["payloads"]
    assert isinstance(payloads, dict)
    assert payloads["station_inventory"]["actual_checksum_sha256"] == (
        "c3fe04fb5757b71a48df66930b833ebc1c05abad73bbc3948e8f42f4385c5b1d"
    )
    assert payloads["station_inventory"]["actual_row_count"] == 2
    assert payloads["station_timeseries"]["actual_checksum_sha256"] == (
        "44aee482ee1175ed2fe296402a14e3d63b1c80883817bbbbaed7a9d073869d64"
    )
    assert payloads["station_timeseries"]["actual_row_count"] == 8
    assert payloads["interpolation_weights"]["actual_checksum_sha256"] == (
        "e10613530de21585eb4b81cec9272581812fe418f7c0e33aaec4c90a2fc4fe20"
    )
    assert payloads["interpolation_weights"]["actual_row_count"] == 4


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("contract_id", None, REASON_FIELD_MISSING),
        ("run_id", None, REASON_IDENTITY_FIELD_MISSING),
        ("forcing_version_id", None, REASON_IDENTITY_FIELD_MISSING),
        ("end_time", None, REASON_TEMPORAL_FIELD_MISSING),
        ("end_time", "not-a-time", REASON_TEMPORAL_FIELD_MALFORMED),
    ],
)
def test_top_level_contract_identity_and_temporal_errors_short_circuit_payload_validation(
    tmp_path: Path,
    field: str,
    value: object,
    expected_reason: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    if value is None:
        del handoff[field]
    else:
        handoff[field] = value
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert expected_reason in codes
    assert REASON_PACKAGE_CHECKSUM_MISMATCH not in codes
    assert REASON_PAYLOAD_CHECKSUM_MISMATCH not in codes
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


@pytest.mark.parametrize("field", ["run_manifest_uri", "forcing_uri"])
def test_missing_compatibility_uri_short_circuits_package_payload_and_table_evidence(
    tmp_path: Path,
    field: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    del handoff[field]
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert REASON_FIELD_MISSING in codes
    assert REASON_PACKAGE_CHECKSUM_MISMATCH not in codes
    assert REASON_PAYLOAD_CHECKSUM_MISMATCH not in codes
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


def test_incomplete_fixture_returns_stable_reasons_and_redacts_credentials() -> None:
    result = _validate("incomplete", "fcst_gfs_2026062012_basins_qhh_shud_incomplete")

    assert result["available"] is False
    codes = _reason_codes(result)
    assert REASON_TEMPORAL_FIELD_MISSING in codes
    assert REASON_PAYLOAD_MISSING not in codes

    serialized = json.dumps(result, sort_keys=True)
    assert "user:pass@" not in serialized
    assert "token=secret" not in serialized
    assert "s3://nhms/runs/fcst_gfs_2026062012_basins_qhh_shud_incomplete/input/manifest.json" in serialized


def test_path_safety_rejects_traversal_and_sibling_package_payloads(tmp_path: Path) -> None:
    case_root = tmp_path / "unsafe"
    shutil.copytree(FIXTURE_ROOT / "unsafe", case_root)
    forcing_package_manifest_path = _forcing_package_manifest_path(case_root)
    forcing_package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        forcing_package_manifest_path,
        {
            "forcing_version_id": "forc_gfs_2026062012_basins_qhh_shud_unsafe",
            "model_id": "basins_qhh_shud",
            "source_id": "GFS",
            "cycle_time": "2026-06-20T12:00:00Z",
            "start_time": "2026-06-20T12:00:00Z",
            "end_time": "2026-06-20T15:00:00Z",
            "basin_id": "qhh",
            "basin_version_id": "basins_qhh_v2026_06",
            "station_count": 2,
            "timestep_count": 2,
            "variable_count": 2,
            "time_range": {
                "start_time": "2026-06-20T12:00:00Z",
                "end_time": "2026-06-20T15:00:00Z",
            },
            "row_time_range": {
                "start_time": "2026-06-20T12:00:00Z",
                "end_time": "2026-06-20T15:00:00Z",
            },
            "variable_set": ["PRCP", "TEMP"],
            "units": {"PRCP": "mm/day", "TEMP": "degC"},
            "quality_flags": {"canonical_products": ["ok"], "station_timeseries": ["ok"]},
            "station_order": ["qhh_forc_001", "qhh_forc_002"],
            "files": [],
            "lineage": {"producer_version": "fixture", "output_files": []},
        },
    )
    handoff = _read_json(
        _handoff_manifest_path(case_root, run_id="fcst_gfs_2026062012_basins_qhh_shud_unsafe")
    )
    handoff[FORCING_PACKAGE_MANIFEST_URI_FIELD] = (
        "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/forcing_package.json"
    )
    handoff[FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD] = sha256_bytes(forcing_package_manifest_path.read_bytes())
    _write_json(_handoff_manifest_path(case_root, run_id="fcst_gfs_2026062012_basins_qhh_shud_unsafe"), handoff)

    result = _validate_case_root(case_root, run_id="fcst_gfs_2026062012_basins_qhh_shud_unsafe")

    assert result["available"] is False
    assert {REASON_PAYLOAD_PATH_UNSAFE, REASON_PAYLOAD_OUTSIDE_PACKAGE}.issubset(_reason_codes(result))


def test_forcing_domain_package_manifest_checksum_mismatch_is_reported_directly(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_PACKAGE_CHECKSUM_MISMATCH in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


def test_forcing_package_manifest_checksum_mismatch_short_circuits_readiness_evidence(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_PACKAGE_CHECKSUM_MISMATCH in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


@pytest.mark.parametrize(
    ("value", "expected_reason"),
    [
        (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/other_model/forcing_package.json",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/payloads/forcing_package.json",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "s3://nhms/raw/gfs/2026062012/forcing_package.json",
            REASON_COMPATIBILITY_URI_UNSAFE,
        ),
    ],
)
def test_forcing_package_manifest_uri_must_be_canonical_package_manifest(
    tmp_path: Path,
    value: str,
    expected_reason: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_PACKAGE_MANIFEST_URI_FIELD] = value
    handoff[FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert expected_reason in codes
    assert REASON_PACKAGE_CHECKSUM_MISMATCH not in codes
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


def test_payload_checksum_mismatch_is_reported_directly(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    fake_checksum = "1" * 64

    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["checksum_sha256"] = fake_checksum
    _write_json(_handoff_manifest_path(case_root), handoff)

    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_payloads = package_manifest["payloads"]
    assert isinstance(package_payloads, dict)
    package_station_timeseries = package_payloads["station_timeseries"]
    assert isinstance(package_station_timeseries, dict)
    package_station_timeseries["checksum_sha256"] = fake_checksum
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)
    _payload_path(case_root, "station_timeseries.json").write_text("{", encoding="utf-8")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert REASON_PAYLOAD_CHECKSUM_MISMATCH in codes
    assert REASON_PAYLOAD_MALFORMED not in codes
    assert REASON_ROW_COUNT_MISMATCH not in codes
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    payload_evidence = evidence["payloads"]
    assert isinstance(payload_evidence, dict)
    assert "station_timeseries" not in payload_evidence


def test_missing_payload_ref_is_reported_when_top_level_contract_is_valid(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    del payloads["station_timeseries"]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_PAYLOAD_MISSING in _reason_codes(result)


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
        FORCING_PACKAGE_MANIFEST_URI_FIELD,
        FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
        FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
        FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD,
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
    station_timeseries_schema = payload_properties["station_timeseries"]["allOf"][1]
    assert set(station_timeseries_schema["required"]) == {
        "variables",
        "units",
        "time_lattice",
    }
    assert station_timeseries_schema["properties"]["variables"]["uniqueItems"] is True
    time_lattice_ref = station_timeseries_schema["properties"]["time_lattice"]["items"]["$ref"]
    assert time_lattice_ref == "#/definitions/time_lattice_segment"
    time_lattice_segment_schema = schema["definitions"]["time_lattice_segment"]
    assert {"variable", "variables", "native_resolution"} <= set(time_lattice_segment_schema["properties"])
    assert time_lattice_segment_schema["properties"]["variables"]["uniqueItems"] is True
    assert payload_properties["interpolation_weights"]["allOf"][1]["properties"]["table"]["const"] == (
        "met.interp_weight"
    )


def test_complete_example_matches_fixture_and_validates_against_schema_when_cli_available() -> None:
    assert _read_json(EXAMPLE_PATH) == _read_json(_manifest_path("complete", COMPLETE_RUN_ID))

    validator = shutil.which("check-jsonschema")
    if validator is None:
        pytest.skip("check-jsonschema is not installed in this environment")
    subprocess.run(
        [validator, "--schemafile", str(SCHEMA_PATH), str(EXAMPLE_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_station_timeseries_lattice_missing_tuple_is_unavailable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    rows = [
        row
        for row in rows
        if not (
            row["station_id"] == "qhh_forc_002"
            and row["variable"] == "TEMP"
            and row["valid_time"] == "2026-06-20T15:00:00Z"
        )
    ]
    _set_station_timeseries_rows(case_root, rows)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_TIMESERIES_LATTICE_MISSING in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


def test_station_timeseries_lattice_extra_tuple_is_unavailable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    extra = dict(rows[0])
    extra["valid_time"] = "2026-06-20T18:00:00Z"
    rows.append(extra)
    _set_station_timeseries_rows(case_root, rows)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_TIMESERIES_LATTICE_EXTRA in _reason_codes(result)


def test_station_timeseries_lattice_duplicate_tuple_is_unavailable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    rows.append(dict(rows[0]))
    _set_station_timeseries_rows(case_root, rows)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_TIMESERIES_LATTICE_DUPLICATE in _reason_codes(result)


def test_station_inventory_duplicate_station_id_and_unique_count_mismatch_are_unavailable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_inventory.json").read_text(encoding="utf-8"))
    rows[1]["station_id"] = rows[0]["station_id"]
    _payload_path(case_root, "station_inventory.json").write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    _sync_payload_ref(case_root, "station_inventory", "station_inventory.json")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert REASON_STATION_INVENTORY_DUPLICATE in codes
    assert REASON_STATION_COUNT_MISMATCH in codes


def test_station_timeseries_declared_variables_must_be_unique(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["variables"] = ["PRCP", "TEMP", "PRCP"]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_STATION_TIMESERIES_VARIABLE_DUPLICATE in _reason_codes(result)


def test_station_timeseries_row_native_resolution_must_match_time_lattice_segment(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["time_lattice"] = [
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2026-06-20T12:00:00Z",
            "native_resolution": "3h",
        },
        {
            "valid_time_start": "2026-06-20T15:00:00Z",
            "valid_time_end": "2026-06-20T15:00:00Z",
            "native_resolution": "1h",
        },
    ]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    assert any(
        reason["code"] == REASON_IDENTITY_MISMATCH
        and reason.get("field", "").endswith(".native_resolution")
        and any(sample.get("expected") == "1h" for sample in reason.get("samples", []))
        for reason in reasons
    )


def test_station_timeseries_time_lattice_can_scope_native_resolution_by_variable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    for row in rows:
        if row["variable"] == "TEMP":
            row["native_resolution"] = "1h"
    _set_station_timeseries_rows(case_root, rows)

    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["time_lattice"] = [
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2026-06-20T12:00:00Z",
            "variable": "PRCP",
            "native_resolution": "3h",
        },
        {
            "valid_time_start": "2026-06-20T15:00:00Z",
            "valid_time_end": "2026-06-20T15:00:00Z",
            "variable": "PRCP",
            "native_resolution": "3h",
        },
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2026-06-20T12:00:00Z",
            "variables": ["TEMP"],
            "native_resolution": "1h",
        },
        {
            "valid_time_start": "2026-06-20T15:00:00Z",
            "valid_time_end": "2026-06-20T15:00:00Z",
            "variables": ["TEMP"],
            "native_resolution": "1h",
        },
    ]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is True
    assert result["unavailable_reasons"] == []


def test_station_timeseries_time_lattice_must_cover_every_declared_variable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    _set_station_timeseries_rows(case_root, [row for row in rows if row["variable"] == "PRCP"])

    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["time_lattice"] = [
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2026-06-20T15:00:00Z",
            "variable": "PRCP",
            "native_resolution": "3h",
        }
    ]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    missing_reason = next(reason for reason in reasons if reason["code"] == REASON_TIMESERIES_LATTICE_MISSING)
    assert missing_reason["field"] == "payloads.station_timeseries.time_lattice"
    assert any(sample.get("variable") == "TEMP" for sample in missing_reason.get("samples", []))


def test_station_timeseries_time_lattice_must_cover_each_variable_window(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    rows = [
        row
        for row in rows
        if (row["variable"] == "PRCP" and row["valid_time"] == "2026-06-20T12:00:00Z")
        or (row["variable"] == "TEMP" and row["valid_time"] == "2026-06-20T15:00:00Z")
    ]
    _set_station_timeseries_rows(case_root, rows)

    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["time_lattice"] = [
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2026-06-20T12:00:00Z",
            "variable": "PRCP",
            "native_resolution": "3h",
        },
        {
            "valid_time_start": "2026-06-20T15:00:00Z",
            "valid_time_end": "2026-06-20T15:00:00Z",
            "variable": "TEMP",
            "native_resolution": "3h",
        },
    ]
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    missing_reason = next(reason for reason in reasons if reason["code"] == REASON_TIMESERIES_LATTICE_MISSING)
    samples = missing_reason.get("samples", [])
    assert any(sample.get("variable") == "PRCP" and sample.get("missing") == "valid_time_end" for sample in samples)
    assert any(sample.get("variable") == "TEMP" and sample.get("missing") == "valid_time_start" for sample in samples)


def test_station_timeseries_lattice_too_large_is_reported_without_materializing_diff(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff["end_time"] = "2036-06-20T15:00:00Z"
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    station_timeseries = payloads["station_timeseries"]
    assert isinstance(station_timeseries, dict)
    station_timeseries["time_lattice"] = [
        {
            "valid_time_start": "2026-06-20T12:00:00Z",
            "valid_time_end": "2036-06-20T15:00:00Z",
            "native_resolution": "1min",
        }
    ]
    _write_json(_handoff_manifest_path(case_root), handoff)

    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_manifest["end_time"] = "2036-06-20T15:00:00Z"
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert REASON_TIMESERIES_LATTICE_TOO_LARGE in codes
    assert REASON_TEMPORAL_WINDOW_INVALID not in codes
    assert REASON_TIMESERIES_LATTICE_MISSING not in codes
    assert REASON_TIMESERIES_LATTICE_EXTRA not in codes


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b'{"metadata":"not rows"}',
        b"[]",
    ],
)
def test_uncountable_or_empty_payload_rows_are_unavailable(tmp_path: Path, content: bytes) -> None:
    case_root = _copy_complete_case(tmp_path)
    _payload_path(case_root, "station_inventory.json").write_bytes(content)
    _sync_payload_ref(case_root, "station_inventory", "station_inventory.json")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert {REASON_PAYLOAD_MALFORMED, REASON_ROW_COUNT_MISMATCH}.issubset(_reason_codes(result))


@pytest.mark.parametrize(
    ("role", "filename"),
    [
        ("station_inventory", "station_inventory.json"),
        ("station_timeseries", "station_timeseries.json"),
        ("interpolation_weights", "interp_weights.json"),
    ],
)
def test_row_level_missing_field_diagnostics_are_bounded(
    tmp_path: Path,
    role: str,
    filename: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    _set_payload_rows(case_root, role, filename, [{} for _ in range(120)])

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_FIELD_MISSING in _reason_codes(result)
    _assert_bounded_row_diagnostics(result, role)


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("source_id", "IFS", REASON_IDENTITY_MISMATCH),
        ("cycle_time", "2026-06-20T15:00:00Z", REASON_COMPATIBILITY_URI_MISMATCH),
        ("basin_version_id", "basins_other_v1", REASON_COMPATIBILITY_URI_MISMATCH),
        ("model_id", "other_model", REASON_COMPATIBILITY_URI_MISMATCH),
    ],
)
def test_package_path_components_are_bound_to_manifest_identity(
    tmp_path: Path,
    field: str,
    value: str,
    expected_reason: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[field] = value
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert expected_reason in _reason_codes(result)


def test_package_path_identity_mismatch_short_circuits_payload_evidence(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    updated_identity = {
        "source_id": "IFS",
        "source": "ifs",
        "model_id": "basins_qhh_ifs",
        "basin_version_id": "basins_qhh_ifs_v2026_06",
    }

    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff.update(updated_identity)
    _write_json(_handoff_manifest_path(case_root), handoff)

    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_manifest.update(updated_identity)
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_COMPATIBILITY_URI_MISMATCH in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "fcst_gfs_2026062012_basins_qhh_shud_stale"),
        ("forcing_version_id", "forc_gfs_2026062012_basins_qhh_shud_stale"),
    ],
)
def test_forcing_domain_package_manifest_identity_must_match_top_level_handoff(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    package_manifest[field] = value
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


def test_forcing_domain_package_manifest_payload_refs_must_match_top_level_handoff(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    package_manifest = _read_json(_forcing_domain_package_manifest_path(case_root))
    payloads = package_manifest["payloads"]
    assert isinstance(payloads, dict)
    station_inventory = payloads["station_inventory"]
    assert isinstance(station_inventory, dict)
    station_inventory["row_count"] = 3
    _write_json(_forcing_domain_package_manifest_path(case_root), package_manifest)
    _sync_forcing_domain_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_ROW_COUNT_MISMATCH in _reason_codes(result)


def test_station_inventory_rows_are_bound_to_basin_identity(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_inventory.json").read_text(encoding="utf-8"))
    rows[0]["basin_version_id"] = "basins_other_v1"
    _payload_path(case_root, "station_inventory.json").write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    _sync_payload_ref(case_root, "station_inventory", "station_inventory.json")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


def test_station_timeseries_rows_are_bound_to_forcing_identity(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "station_timeseries.json").read_text(encoding="utf-8"))
    rows[0]["forcing_version_id"] = "forc_other"
    _payload_path(case_root, "station_timeseries.json").write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    _sync_payload_ref(case_root, "station_timeseries", "station_timeseries.json")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


def test_interpolation_weight_rows_are_bound_to_model_identity(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "interp_weights.json").read_text(encoding="utf-8"))
    rows[0]["model_id"] = "other_model"
    _payload_path(case_root, "interp_weights.json").write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    _sync_payload_ref(case_root, "interpolation_weights", "interp_weights.json")

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


def test_interpolation_weight_rows_reject_duplicate_identity_key(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "interp_weights.json").read_text(encoding="utf-8"))
    rows.append(dict(rows[0]))
    _set_payload_rows(case_root, "interpolation_weights", "interp_weights.json", rows)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_INTERP_WEIGHT_DUPLICATE in _reason_codes(result)
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    duplicate_reason = next(reason for reason in reasons if reason["code"] == REASON_INTERP_WEIGHT_DUPLICATE)
    assert duplicate_reason["duplicate_count"] == 1
    assert len(duplicate_reason["samples"]) == 1


def test_direct_grid_interpolation_weight_rows_reject_station_variable_duplicate(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    rows = json.loads(_payload_path(case_root, "interp_weights.json").read_text(encoding="utf-8"))
    direct_grid_rows = [dict(rows[0]), {**dict(rows[0]), "grid_cell_id": "gfs_cell_101_39"}]
    for row in direct_grid_rows:
        row["method"] = "direct_grid"
    _set_payload_rows(case_root, "interpolation_weights", "interp_weights.json", direct_grid_rows)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    reasons = result["unavailable_reasons"]
    assert isinstance(reasons, list)
    duplicate_reason = next(
        reason
        for reason in reasons
        if reason["code"] == REASON_INTERP_WEIGHT_DUPLICATE and reason.get("method") == "direct_grid"
    )
    assert duplicate_reason["duplicate_count"] == 1
    assert "grid_cell_id" not in duplicate_reason["samples"][0]


def test_invalid_package_uri_short_circuits_payload_reads(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff["forcing_package_uri"] = (
        "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/"
        "basins_qhh_shud/../other_model"
    )
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_COMPATIBILITY_URI_UNSAFE in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        (
            "model_package_uri",
            "s3://nhms/models/other_model/v2026_06/package/model_package.json",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "forcing_uri",
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/other_model/",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "run_manifest_uri",
            "s3://nhms/runs/fcst_gfs_2026062012_other/input/manifest.json",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "output_uri",
            f"s3://nhms/runs/{COMPLETE_RUN_ID}/output/../logs/manifest.json",
            REASON_COMPATIBILITY_URI_UNSAFE,
        ),
        (
            "model_package_uri",
            "s3://nhms/raw/gfs/2026062012/model_package.json",
            REASON_COMPATIBILITY_URI_UNSAFE,
        ),
    ],
)
def test_compatibility_provenance_uris_are_object_store_normalized_and_identity_bound(
    tmp_path: Path,
    field: str,
    value: str,
    expected_reason: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[field] = value
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert expected_reason in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}


@pytest.mark.parametrize(
    ("value", "expected_reason"),
    [
        (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/other_model/forcing_domain_package.json",
            REASON_COMPATIBILITY_URI_MISMATCH,
        ),
        (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/"
            "basins_qhh_shud/payloads/forcing_domain_package.json",
            REASON_COMPATIBILITY_URI_UNSAFE,
        ),
        (
            "s3://nhms/raw/gfs/2026062012/forcing_domain_package.json",
            REASON_COMPATIBILITY_URI_UNSAFE,
        ),
    ],
)
def test_forcing_domain_package_manifest_uri_must_stay_in_package_scope_and_short_circuit_payloads(
    tmp_path: Path,
    value: str,
    expected_reason: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD] = value
    handoff[FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD] = "0" * 64
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    codes = _reason_codes(result)
    assert expected_reason in codes
    assert REASON_PACKAGE_CHECKSUM_MISMATCH not in codes
    assert REASON_PAYLOAD_CHECKSUM_MISMATCH not in codes
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}
    assert evidence["table_row_counts"] == {}


def test_symlink_object_store_root_returns_stable_unavailable_reason(tmp_path: Path) -> None:
    symlink_root = tmp_path / "object-store-link"
    try:
        symlink_root.symlink_to(_object_store_root("complete").resolve(), target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink not available: {error}")

    result = validate_forcing_domain_handoff_path(
        symlink_root / "runs" / COMPLETE_RUN_ID / "input" / "forcing_domain_handoff.json",
        object_store_root=symlink_root,
    )

    assert result["available"] is False
    assert REASON_OBJECT_STORE_ROOT_UNAVAILABLE in _reason_codes(result)
