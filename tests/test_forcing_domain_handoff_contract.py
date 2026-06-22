from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from packages.common.forcing_domain_handoff import (
    PACKAGE_MANIFEST_CHECKSUM_FIELD,
    PACKAGE_MANIFEST_URI_FIELD,
    REASON_IDENTITY_MISMATCH,
    REASON_OBJECT_STORE_ROOT_UNAVAILABLE,
    REASON_PACKAGE_PATH_UNSAFE,
    REASON_PAYLOAD_MALFORMED,
    REASON_PAYLOAD_MISSING,
    REASON_PAYLOAD_OUTSIDE_PACKAGE,
    REASON_PAYLOAD_PATH_UNSAFE,
    REASON_ROW_COUNT_MISMATCH,
    REASON_TEMPORAL_FIELD_MISSING,
    REASON_TEMPORAL_WINDOW_INVALID,
    validate_forcing_domain_handoff_path,
)
from packages.common.object_store import sha256_bytes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "forcing_domain_handoff"
SCHEMA_PATH = Path("schemas/forcing_domain_handoff.schema.json")
EXAMPLE_PATH = Path("schemas/examples/forcing_domain_handoff.example.json")
COMPLETE_RUN_ID = "fcst_gfs_2026062012_basins_qhh_shud"
PACKAGE_MANIFEST_RELATIVE_PATH = (
    "forcing/gfs/2026062012/basins_qhh_v2026_06/basins_qhh_shud/forcing_domain_package.json"
)


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


def _tree_snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _package_manifest_path(case_root: Path) -> Path:
    return case_root / "object-store" / PACKAGE_MANIFEST_RELATIVE_PATH


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


def _sync_package_manifest_checksum(case_root: Path) -> None:
    package_manifest_path = _package_manifest_path(case_root)
    checksum = sha256_bytes(package_manifest_path.read_bytes())
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[PACKAGE_MANIFEST_CHECKSUM_FIELD] = checksum
    _write_json(_handoff_manifest_path(case_root), handoff)


def _sync_payload_ref(case_root: Path, role: str, filename: str) -> None:
    checksum = sha256_bytes(_payload_path(case_root, filename).read_bytes())
    handoff = _read_json(_handoff_manifest_path(case_root))
    payloads = handoff["payloads"]
    assert isinstance(payloads, dict)
    payload = payloads[role]
    assert isinstance(payload, dict)
    payload["checksum_sha256"] = checksum
    package_manifest = _read_json(_package_manifest_path(case_root))
    package_payloads = package_manifest["payloads"]
    assert isinstance(package_payloads, dict)
    package_payload = package_payloads[role]
    assert isinstance(package_payload, dict)
    package_payload["checksum_sha256"] = checksum
    _write_json(_handoff_manifest_path(case_root), handoff)
    _write_json(_package_manifest_path(case_root), package_manifest)
    _sync_package_manifest_checksum(case_root)


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
        "package_manifest_uri": (
            "s3://nhms/forcing/gfs/2026062012/basins_qhh_v2026_06/"
            "basins_qhh_shud/forcing_domain_package.json"
        ),
        "package_manifest_checksum_sha256": "e4ff49b1d0932f2028938ffe7bd4577e5510ee985ea8c3be53485ca49c06f19d",
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
        PACKAGE_MANIFEST_URI_FIELD,
        PACKAGE_MANIFEST_CHECKSUM_FIELD,
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


def test_station_timeseries_undercoverage_is_unavailable(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff["end_time"] = "2026-06-20T18:00:00Z"
    _write_json(_handoff_manifest_path(case_root), handoff)
    package_manifest = _read_json(_package_manifest_path(case_root))
    package_manifest["end_time"] = "2026-06-20T18:00:00Z"
    _write_json(_package_manifest_path(case_root), package_manifest)
    _sync_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_TEMPORAL_WINDOW_INVALID in _reason_codes(result)


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
    ("field", "value"),
    [
        ("source_id", "IFS"),
        ("cycle_time", "2026-06-20T15:00:00Z"),
        ("basin_version_id", "basins_other_v1"),
        ("model_id", "other_model"),
    ],
)
def test_package_path_components_are_bound_to_manifest_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    handoff = _read_json(_handoff_manifest_path(case_root))
    handoff[field] = value
    _write_json(_handoff_manifest_path(case_root), handoff)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


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

    package_manifest = _read_json(_package_manifest_path(case_root))
    package_manifest.update(updated_identity)
    _write_json(_package_manifest_path(case_root), package_manifest)
    _sync_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)
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
def test_package_manifest_identity_must_match_top_level_handoff(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    package_manifest = _read_json(_package_manifest_path(case_root))
    package_manifest[field] = value
    _write_json(_package_manifest_path(case_root), package_manifest)
    _sync_package_manifest_checksum(case_root)

    result = _validate_case_root(case_root)

    assert result["available"] is False
    assert REASON_IDENTITY_MISMATCH in _reason_codes(result)


def test_package_manifest_payload_refs_must_match_top_level_handoff(tmp_path: Path) -> None:
    case_root = _copy_complete_case(tmp_path)
    package_manifest = _read_json(_package_manifest_path(case_root))
    payloads = package_manifest["payloads"]
    assert isinstance(payloads, dict)
    station_inventory = payloads["station_inventory"]
    assert isinstance(station_inventory, dict)
    station_inventory["row_count"] = 3
    _write_json(_package_manifest_path(case_root), package_manifest)
    _sync_package_manifest_checksum(case_root)

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
    assert REASON_PACKAGE_PATH_UNSAFE in _reason_codes(result)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["payloads"] == {}


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
