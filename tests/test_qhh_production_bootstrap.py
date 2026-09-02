"""QHH production bootstrap: preflight, parser and refusal surfaces (issue #1948 partition A)."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from psycopg2.extras import Json

import workers.model_registry.qhh_production_bootstrap as qhh_bootstrap
from tests.qhh_production_bootstrap_helpers import _qhh_registry_fixture
from workers.model_registry.cli import _argparse_main
from workers.model_registry.qhh_production_bootstrap import (
    MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
    MAX_QHH_OUTPUT_SEGMENTS,
    MAX_QHH_TSD_FORC_BYTES,
    QhhProductionBootstrapError,
    bootstrap_qhh_production,
    read_qhh_output_segment_count,
    read_qhh_tsd_forc,
)


def test_read_qhh_tsd_forc_reports_created_station_identity(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh" / "input" / "qhh"
    input_dir.mkdir(parents=True)
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )

    stations, checksum = read_qhh_tsd_forc(tsd_forc, input_dir)

    assert checksum
    assert [station.station_id for station in stations] == ["qhh_forc_001", "qhh_forc_002"]
    assert stations[0].station_name == "QHH forcing station 001"
    assert stations[0].elevation_m == 0.0
    assert stations[1].forcing_filename == "X000002.csv"


def test_read_qhh_tsd_forc_namespaces_station_identity_by_project(tmp_path: Path) -> None:
    # Second-basin (#291 §3B.1): non-qhh project_name must NOT collide with qhh PKs.
    input_dir = tmp_path / "heihe" / "input" / "heihe"
    input_dir.mkdir(parents=True)
    tsd_forc = input_dir / "heihe.tsd.forc"
    tsd_forc.write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )

    heihe_stations, _ = read_qhh_tsd_forc(tsd_forc, input_dir, project_name="heihe")
    assert [s.station_id for s in heihe_stations] == ["heihe_forc_001", "heihe_forc_002"]
    assert heihe_stations[0].station_name == "HEIHE forcing station 001"

    # qhh path unchanged: default and explicit project_name="qhh" still yield qhh_forc_NNN.
    qhh_stations, _ = read_qhh_tsd_forc(tsd_forc, input_dir, project_name="qhh")
    assert [s.station_id for s in qhh_stations] == ["qhh_forc_001", "qhh_forc_002"]
    assert qhh_stations[0].station_name == "QHH forcing station 001"
    assert {s.station_id for s in qhh_stations}.isdisjoint(
        {s.station_id for s in heihe_stations}
    )


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        (
            "2 6\n/forcing\nID Lon Lat X Y Z Filename\n1 100 30 1 1 1 X000001.csv\n",
            "QHH_BOOTSTRAP_STATION_COUNT_MISMATCH",
        ),
        ("1 6\n/forcing\nID Lon Lat X Y Z Filename\nbad row\n", "QHH_BOOTSTRAP_TSD_FORC_MALFORMED"),
    ],
)
def test_read_qhh_tsd_forc_rejects_mismatch_and_malformed(
    tmp_path: Path,
    content: str,
    error_code: str,
) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(content, encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == error_code
    assert exc_info.value.details["no_mutation_expected"] is True


@pytest.mark.parametrize("xyz", [("nan", "2", "3"), ("1", "inf", "3"), ("1", "2", "-inf")])
def test_read_qhh_tsd_forc_rejects_non_finite_xyz_metadata(
    tmp_path: Path,
    xyz: tuple[str, str, str],
) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    x, y, z = xyz
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(
        "1 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        f"1 100 30 {x} {y} {z} X000001.csv\n",
        encoding="utf-8",
    )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_TSD_FORC_MALFORMED"
    assert exc_info.value.details["malformed_rows"][0]["reason"] == "non_finite_xyz"
    assert exc_info.value.details["no_mutation_expected"] is True


@pytest.mark.parametrize(
    ("station_index", "reason"),
    [
        ("1.5", "invalid_forcing_index"),
        ("nan", "invalid_forcing_index"),
        ("inf", "invalid_forcing_index"),
        ("250001", "invalid_forcing_index"),
        ("999999999999999999999999999999", "invalid_forcing_index"),
    ],
)
def test_read_qhh_tsd_forc_rejects_malformed_station_index_tokens(
    tmp_path: Path,
    station_index: str,
    reason: str,
) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(
        "1 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        f"{station_index} 100 30 1 2 3 X000001.csv\n",
        encoding="utf-8",
    )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_TSD_FORC_MALFORMED"
    assert exc_info.value.details["malformed_rows"][0]["reason"] == reason
    assert exc_info.value.details["no_mutation_expected"] is True


@pytest.mark.parametrize(
    "filename",
    ["/tmp/X000001.csv", "nested/X000001.csv", "../X000001.csv", r"nested\\X000001.csv", ".", "..", "bad\x00.csv"],
)
def test_read_qhh_tsd_forc_rejects_raw_path_like_filename_tokens(
    tmp_path: Path,
    filename: str,
) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_text(
        "1 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        f"1 100 30 1 2 3 {filename}\n",
        encoding="utf-8",
    )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_TSD_FORC_MALFORMED"
    assert exc_info.value.details["malformed_rows"][0]["reason"] == "invalid_forcing_filename"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_tsd_forc_rejects_oversized_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.write_bytes(b"1 6\n" + (b"x" * (MAX_QHH_TSD_FORC_BYTES + 1)))

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_TSD_FORC_OVERSIZED"


def test_read_qhh_tsd_forc_rejects_symlink_leaf(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    outside = tmp_path / "outside.tsd.forc"
    outside.write_text("1\nmeta\nheader\n1 100 30 1 1 1 X000001.csv\n", encoding="utf-8")
    link = input_dir / "qhh.tsd.forc"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(link, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE"


def test_read_qhh_tsd_forc_rejects_non_regular_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    tsd_forc = input_dir / "qhh.tsd.forc"
    tsd_forc.mkdir(parents=True)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_tsd_forc(tsd_forc, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PROJECT_FILE_UNSAFE"


def test_bootstrap_reports_missing_qhh_project_file_before_database(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    input_dir.joinpath("qhh.tsd.forc").unlink()

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PROJECT_FILE_MISSING"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_symlink_qhh_source_ancestor_before_database(tmp_path: Path) -> None:
    target_root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path / "target")
    root = tmp_path / "basins"
    root.mkdir()
    try:
        (root / "qhh").symlink_to(target_root / "qhh", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink support unavailable: {error}")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_output_segment_count_rejects_out_of_range_positive_count(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    sp_riv.write_text(f"{MAX_QHH_OUTPUT_SEGMENTS + 1} 6\n1 0 0 0.01 100 0\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_output_segment_count(sp_riv, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_INVALID"
    assert exc_info.value.details["output_segment_count"] == MAX_QHH_OUTPUT_SEGMENTS + 1
    assert exc_info.value.details["max_output_segment_count"] == MAX_QHH_OUTPUT_SEGMENTS
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_output_segment_count_rejects_header_body_count_mismatch(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    sp_riv.write_text("2 6\n1 0 0 0.01 100 0\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_output_segment_count(sp_riv, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_MISMATCH"
    assert exc_info.value.details["expected_count"] == 2
    assert exc_info.value.details["parsed_count"] == 1
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_output_segment_count_rejects_malformed_body_row(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    sp_riv.write_text("1 6\nbad 0 0 0.01 100 0\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_output_segment_count(sp_riv, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_SP_RIV_MALFORMED"
    assert exc_info.value.details["malformed_rows"] == [{"line_number": 2, "reason": "invalid_segment_token"}]
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_output_segment_count_rejects_non_exact_segment_tokens(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    sp_riv.write_text("2 6\n1 0 0 0.01 100 0\n3 0 0 0.01 100 0\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        read_qhh_output_segment_count(sp_riv, input_dir)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_SP_RIV_MALFORMED"
    assert exc_info.value.details["missing_segment_tokens"] == [2]
    assert exc_info.value.details["extra_segment_tokens"] == [3]
    assert exc_info.value.details["no_mutation_expected"] is True


def test_read_qhh_output_segment_count_accepts_multiblock_shud_format(tmp_path: Path) -> None:
    input_dir = tmp_path / "qhh"
    input_dir.mkdir()
    sp_riv = input_dir / "qhh.sp.riv"
    # Standard SHUD layout: count header, column-name line, `count` data rows, then
    # unrelated trailing blocks (channel types, coordinates) that must be ignored.
    sp_riv.write_text(
        "\n".join(
            [
                "3\t6",
                "Index\tDown\tType\tSlope\tLength\tBC",
                "1\t2\t2\t0.001\t100\t0",
                "2\t3\t2\t-0.002\t200\t0",
                "3\t4\t2\t0.003\t300\t0",
                "5\t9",
                "ChannelType\tBankSlope\tWidth\tDepth\tSinuosity\tManningN\tCwr\tKsatH\tBedThick",
                "1\t1\t10\t2\t1\t0.03\t0.6\t0.0001\t0.1",
                "2\t1\t12\t3\t1\t0.03\t0.6\t0.0001\t0.1",
                "3\t1\t14\t4\t1\t0.03\t0.6\t0.0001\t0.1",
                "4\t1\t16\t5\t1\t0.03\t0.6\t0.0001\t0.1",
                "5\t1\t18\t6\t1\t0.03\t0.6\t0.0001\t0.1",
                "3\t6",
                "From.x\tFrom.y\tTo.x\tTo.y\tArea\tElev",
                "0\t0\t1\t1\t10\t100",
                "1\t1\t2\t2\t20\t200",
                "2\t2\t3\t3\t30\t300",
                "",
            ]
        ),
        encoding="utf-8",
    )

    count, checksum = read_qhh_output_segment_count(sp_riv, input_dir)

    assert count == 3
    assert checksum == hashlib.sha256(sp_riv.read_bytes()).hexdigest()


def test_bootstrap_rejects_relative_traversal_project_path(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    root.mkdir()

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            qhh_basin_slug="../qhh",
            work_dir=tmp_path / "work",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PROJECT_PATH_UNSAFE"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_discovery_entry_overflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _input_dir, _inventory_path, _manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    for index in range(MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES + 1):
        (root / f"unrelated-{index:04d}").mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            work_dir=tmp_path / "work",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED"
    assert exc_info.value.details["max_entries"] == MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES


def test_bounded_discovery_entry_limit_streams_without_materializing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "basins"
    qhh_source_root = root / "qhh"
    qhh_source_root.mkdir(parents=True)
    consumed = 0

    class FakeDirEntry:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeScandir:
        def __enter__(self) -> FakeScandir:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def __iter__(self) -> FakeScandir:
            return self

        def __next__(self) -> FakeDirEntry:
            nonlocal consumed
            consumed += 1
            if consumed > MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES + 100:
                raise AssertionError("bounded discovery consumed beyond the configured limit")
            return FakeDirEntry(f"entry-{consumed}")

    def fake_scandir(path: Path) -> FakeScandir:
        assert Path(path) == root
        return FakeScandir()

    def fake_stat_no_follow(path: Path, containment_root: Path | None = None) -> Any:
        del containment_root
        mode = stat.S_IFDIR if Path(path) == root else stat.S_IFREG
        return type("FakeStat", (), {"st_mode": mode})()

    # #1765: `qhh_bootstrap.os` is the real `os` module, so this patch is global.
    # `tmp_path_retention_policy = "failed"` rmtree()s tmp_path in the fixture
    # finalizer, which calls os.scandir(fd) -- scope the patch to the call under
    # test so it can never outlive the assertion and break teardown.
    with monkeypatch.context() as patch:
        patch.setattr(qhh_bootstrap.os, "scandir", fake_scandir)
        patch.setattr(qhh_bootstrap, "stat_no_follow", fake_stat_no_follow)

        with pytest.raises(QhhProductionBootstrapError) as exc_info:
            qhh_bootstrap._bounded_discovery_preflight(
                root, model_id="basins_qhh_shud", qhh_source_root=qhh_source_root
            )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED"
    assert consumed == MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES + 1


def test_bounded_discovery_depth_cap_reports_package_not_found_for_too_deep_qhh_root(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    qhh_source_root = root / "a" / "b" / "c" / "d" / "qhh"
    qhh_source_root.mkdir(parents=True)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._bounded_discovery_preflight(root, model_id="basins_qhh_shud", qhh_source_root=qhh_source_root)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_NOT_FOUND"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_evidence_no_clobber_before_database(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "bootstrap.json"
    evidence_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=evidence_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_EVIDENCE_NO_CLOBBER"


def test_bootstrap_rejects_evidence_path_outside_root(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=tmp_path / "outside.json",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE"


def test_bootstrap_rejects_regular_file_evidence_lane_before_database(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    lane = evidence_dir / "lane"
    lane.write_text("unchanged\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path="lane/bootstrap.json",
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE"
    assert exc_info.value.details["no_mutation_expected"] is True
    assert lane.read_text(encoding="utf-8") == "unchanged\n"


def test_bootstrap_removes_reserved_evidence_when_database_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "bootstrap.json"

    def fail_database(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        assert evidence_path.exists()
        assert evidence_path.read_bytes() == b""
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DATABASE_ERROR",
            "Injected database failure after evidence reservation.",
            model_id=model_id,
        )

    monkeypatch.setattr(qhh_bootstrap, "_bootstrap_database", fail_database)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            evidence_path=evidence_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DATABASE_ERROR"
    assert not evidence_path.exists()


def test_bootstrap_cli_omits_final_evidence_write_failure_after_database_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "bootstrap.json"

    def fake_database(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        assert evidence_path.exists()
        return {
            "schema_version": qhh_bootstrap.QHH_BOOTSTRAP_SCHEMA_VERSION,
            "status": "bootstrapped",
            "model_id": model_id,
            "scheduler_readiness": {"ready": True},
        }

    def fail_final_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED",
            "Injected final evidence write failure.",
            model_id=model_id,
            path=str(evidence_path),
        )

    monkeypatch.setattr(qhh_bootstrap, "_bootstrap_database", fake_database)
    monkeypatch.setattr(qhh_bootstrap, "_write_reserved_evidence_path", fail_final_write)

    exit_code = _argparse_main(
        [
            "bootstrap-qhh-production",
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--basins-root",
            str(root),
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--evidence-dir",
            str(evidence_dir),
            "--evidence-path",
            str(evidence_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "bootstrapped"
    assert payload["scheduler_readiness"]["ready"] is True
    assert payload["evidence_write_omitted"] is True
    assert payload["evidence_write_error"]["error_code"] == "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED"
    assert not evidence_path.exists()


def test_bootstrap_rejects_malformed_manifest_json(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_MANIFEST_INVALID"


def test_bootstrap_rejects_oversized_package_manifest_before_database(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.write_bytes(b"{" + b'"x":' + b'"' + (b"a" * (qhh_bootstrap.MAX_QHH_JSON_BYTES + 1)) + b'"}')

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PACKAGE_MANIFEST_INVALID_OVERSIZED"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_precomputed_sources_from_different_physical_qhh_root(tmp_path: Path) -> None:
    source_root, source_input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(
        tmp_path / "source"
    )
    (
        current_root,
        _current_input_dir,
        _current_inventory_path,
        _current_manifest_path,
        _model_id,
    ) = _qhh_registry_fixture(
        tmp_path / "current"
    )
    source_input_dir.joinpath("qhh.sp.riv").write_text("not-a-valid-river-count\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=current_root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert source_root != current_root
    assert exc_info.value.error_code == "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH"
    assert set(exc_info.value.details["fields"]) == {"source_root", "input_dir"}
    assert exc_info.value.details["actual_source_root"] == str(source_root / "qhh")
    assert exc_info.value.details["expected_source_root"] == str(current_root / "qhh")
    assert exc_info.value.details["no_mutation_expected"] is True


def test_bootstrap_rejects_manifest_digest_mismatch(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.with_suffix(manifest_path.suffix + ".sha256").write_text("0" * 64 + "\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_MANIFEST_DIGEST_MISMATCH"


def test_bootstrap_rejects_oversized_checksum_sidecar_before_database(tmp_path: Path) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    manifest_path.with_suffix(manifest_path.suffix + ".sha256").write_bytes(
        b"0" * (qhh_bootstrap.MAX_QHH_CHECKSUM_BYTES + 1)
    )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            basins_root=root,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_CHECKSUM_OVERSIZED"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_seed_station_rows_passes_properties_without_digest_to_insert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_execute_values(
        cursor: Any,
        sql: str,
        argslist: list[tuple[Any, ...]],
        *,
        template: str,
        page_size: int,
    ) -> None:
        del cursor, sql, page_size
        calls.append({"argslist": argslist, "template": template})

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            del sql, params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    station = qhh_bootstrap.QhhForcingStation(
        station_id="qhh_forc_001",
        station_name="QHH forcing station 001",
        forcing_index=1,
        longitude=100.1,
        latitude=30.1,
        x=1.0,
        y=2.0,
        z=-9999.0,
        elevation_m=0.0,
        forcing_filename="X000001.csv",
        original_id="1",
    )
    monkeypatch.setattr(qhh_bootstrap, "execute_values", fake_execute_values, raising=False)
    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)

    qhh_bootstrap._seed_station_rows(
        FakeCursor(),
        model={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_v1",
        },
        stations=[station],
        project_name="qhh",
        tsd_forc_path=tmp_path / "qhh.tsd.forc",
        tsd_forc_checksum="sha",
    )

    assert calls
    argslist = calls[0]["argslist"]
    assert len(argslist[0]) == 9
    assert calls[0]["template"].count("%s") == 9
    assert isinstance(argslist[0][8], Json)
