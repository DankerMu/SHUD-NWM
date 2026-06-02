from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg2.extras import Json

import workers.model_registry.qhh_production_bootstrap as qhh_bootstrap
from packages.common.model_registry import PsycopgModelRegistryStore
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from tests.test_basins_registry_import import _write_registry_fixture
from tests.test_production_scheduler import FakeAdapter, _dt
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
    assert stations[0].elevation_m == 0.0
    assert stations[1].forcing_filename == "X000002.csv"


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

    monkeypatch.setattr(qhh_bootstrap.os, "scandir", fake_scandir)
    monkeypatch.setattr(qhh_bootstrap, "stat_no_follow", fake_stat_no_follow)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._bounded_discovery_preflight(root, model_id="basins_qhh_shud", qhh_source_root=qhh_source_root)

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


def test_existing_output_segment_digest_ignores_deterministic_geometry_backfill_properties() -> None:
    expected = {
        "seed": "qhh_production_bootstrap",
        "model_id": "basins_qhh_shud",
        "basin_id": "qhh",
        "basin_version_id": "qhh_v1",
        "shud_output_river": True,
        "shud_riv_index": 1,
        "source": "qhh.sp.riv",
        "source_file": "/input/qhh.sp.riv",
        "source_sha256": "sha",
        "geometry_source": "gis_rivseg_iRiv",
        "output_identity": "qhh.sp.riv:1",
    }
    enriched = {
        **expected,
        "geometry_source_segment_count": 2,
        "geometry_source_length_m": 123.4,
    }

    normalized = qhh_bootstrap._output_segment_idempotency_properties(enriched, expected)

    assert normalized == expected


def test_seed_output_segment_rows_reports_geometry_backfilled_rows_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sp_riv_path = Path("/input/qhh.sp.riv")
    model = {
        "model_id": "basins_qhh_shud",
        "basin_id": "qhh",
        "basin_version_id": "qhh_v1",
        "river_network_version_id": "qhh_rivnet_v1",
    }
    expected = qhh_bootstrap._output_segment_expected_properties(
        model=model,
        project_name="qhh",
        index=1,
        sp_riv_path=sp_riv_path,
        sp_riv_checksum="sha",
    )
    stored = {
        **expected,
        "geometry_source_segment_count": 2,
        "geometry_source_length_m": 123.4,
    }

    class FakeCursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            del params
            self.sql = sql

        def fetchone(self) -> dict[str, Any]:
            assert "order_offset" in self.sql
            return {"order_offset": 0}

        def fetchall(self) -> list[dict[str, Any]]:
            assert "FROM core.river_segment" in self.sql
            return [
                {
                    "river_segment_id": "basins_qhh_shud_shud_riv_000001",
                    "river_network_version_id": "qhh_rivnet_v1",
                    "segment_order": 1,
                    "properties_json": stored,
                }
            ]

    monkeypatch.setattr("psycopg2.extras.execute_values", lambda *args, **kwargs: None)

    counts = qhh_bootstrap._seed_output_segment_rows(
        FakeCursor(),
        model=model,
        project_name="qhh",
        output_segment_count=1,
        sp_riv_path=sp_riv_path,
        sp_riv_checksum="sha",
    )

    assert counts == {"created": 0, "updated": 0, "unchanged": 1}


def test_scheduler_ready_profile_overrides_cannot_replace_canonical_identity(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    paths = qhh_bootstrap._prepare_bootstrap_paths(
        basins_root=root,
        qhh_basin_slug="qhh",
        qhh_project_name="qhh",
        model_id="basins_qhh_shud",
        package_version="vbasins-qhh-production",
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        work_dir=tmp_path / "work",
    )
    preflight_sources = qhh_bootstrap._prepare_preflight_sources_from_bounded_json(
        paths.inventory_path,
        paths.package_manifest_path,
        model_id="basins_qhh_shud",
    )
    sources = qhh_bootstrap._prepare_sources_from_preflight(preflight_sources, model_id="basins_qhh_shud")
    stations, tsd_checksum = read_qhh_tsd_forc(input_dir / "qhh.tsd.forc", input_dir)
    output_count, sp_checksum = read_qhh_output_segment_count(input_dir / "qhh.sp.riv", input_dir)
    profile = qhh_bootstrap._scheduler_ready_resource_profile(
        qhh_bootstrap.QhhBootstrapContext(
            sources=sources,
            paths=paths,
            stations=stations,
            output_segment_count=output_count,
            tsd_forc_checksum=tsd_checksum,
            sp_riv_checksum=sp_checksum,
            shud_code_version="basins-shud",
        ),
        resource_profile_overrides={
            "model_id": "evil",
            "project_name": "evil",
            "station_count": 999,
            "output_segment_count": 999,
            "runnable": False,
            "package_checksum": "evil",
            "memory_gb": 16,
            "partition": "debug",
        },
    )

    assert profile["model_id"] == "basins_qhh_shud"
    assert profile["project_name"] == "qhh"
    assert profile["station_count"] == 2
    assert profile["output_segment_count"] == 2
    assert profile["runnable"] is True
    assert profile["package_checksum"] == sources.manifest["package_checksum"]
    assert profile["memory_gb"] == 16
    assert profile["partition"] == "debug"


def test_scheduler_ready_profile_rebuild_strips_existing_run_scoped_identity(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    paths = qhh_bootstrap._prepare_bootstrap_paths(
        basins_root=root,
        qhh_basin_slug="qhh",
        qhh_project_name="qhh",
        model_id="basins_qhh_shud",
        package_version="vbasins-qhh-production",
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        work_dir=tmp_path / "work",
    )
    preflight_sources = qhh_bootstrap._prepare_preflight_sources_from_bounded_json(
        paths.inventory_path,
        paths.package_manifest_path,
        model_id="basins_qhh_shud",
    )
    sources = qhh_bootstrap._prepare_sources_from_preflight(preflight_sources, model_id="basins_qhh_shud")
    stations, tsd_checksum = read_qhh_tsd_forc(input_dir / "qhh.tsd.forc", input_dir)
    output_count, sp_checksum = read_qhh_output_segment_count(input_dir / "qhh.sp.riv", input_dir)
    expected = qhh_bootstrap._scheduler_ready_resource_profile(
        qhh_bootstrap.QhhBootstrapContext(
            sources=sources,
            paths=paths,
            stations=stations,
            output_segment_count=output_count,
            tsd_forc_checksum=tsd_checksum,
            sp_riv_checksum=sp_checksum,
            shud_code_version="basins-shud",
        ),
        resource_profile_overrides={},
    )

    merged = qhh_bootstrap._canonical_scheduler_ready_resource_profile(
        {
            "partition": "debug",
            "canonical_product_id": "stale-canon",
            "published_manifest_id": "stale-manifest",
            "pipeline_job_id": "stale-job",
            "output_uri": "s3://nhms/runs/stale/output/",
            "custom_unowned": "preserve-me-not",
        },
        expected,
        resource_profile_overrides={"memory_gb": 12, "output_uri": "s3://evil/stale/"},
    )

    assert merged["partition"] == "standard"
    assert merged["memory_gb"] == 12
    assert merged["package_checksum"] == sources.manifest["package_checksum"]
    assert merged["model_package_uri"] == sources.manifest["model_package_uri"]
    assert not {
        "canonical_product_id",
        "published_manifest_id",
        "pipeline_job_id",
        "output_uri",
        "custom_unowned",
    } & set(merged)


def test_generated_qhh_inventory_enforces_budget_during_discovery_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, _inventory_path, _manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    qhh_source_root = root / "qhh"
    calls: list[Any] = []

    def fake_preflight(root_arg: Path, *, model_id: str, qhh_source_root: Path) -> None:
        calls.append(("preflight", root_arg, model_id, qhh_source_root))

    def fake_discover(root_arg: Path, *, budget: Any = None) -> dict[str, Any]:
        calls.append(("discover", root_arg, budget.max_entries if budget is not None else None))
        raise qhh_bootstrap.BasinsDiscoveryError(
            "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED",
            "budget exhausted",
            path=str(root_arg),
        )

    monkeypatch.setattr(qhh_bootstrap, "_bounded_discovery_preflight", fake_preflight)
    monkeypatch.setattr(qhh_bootstrap, "discover_basins_inventory", fake_discover)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._discover_qhh_inventory(root, model_id=model_id, qhh_source_root=qhh_source_root)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED"
    assert calls[0][0] == "preflight"
    assert calls[1] == ("discover", root, MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES)


def test_generated_qhh_inventory_blocks_deep_calib_descendants_after_preflight(
    tmp_path: Path,
) -> None:
    root, _input_dir, _inventory_path, _manifest_path, model_id = _qhh_registry_fixture(tmp_path)
    qhh_source_root = root / "qhh"
    deep = qhh_source_root / "CALIB" / "d1" / "d2" / "d3" / "d4" / "d5" / "d6"
    deep.mkdir(parents=True)
    (deep / "calib.txt").write_text("calibration\n", encoding="utf-8")

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        qhh_bootstrap._discover_qhh_inventory(root, model_id=model_id, qhh_source_root=qhh_source_root)

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DISCOVERY_DEPTH_EXCEEDED"
    assert exc_info.value.details["no_mutation_expected"] is True


def test_duplicate_active_preflight_matches_same_package_identity_without_qhh_flags() -> None:
    executed: dict[str, Any] = {}

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            executed["sql"] = sql
            executed["params"] = params

        def fetchall(self) -> list[dict[str, Any]]:
            return [
                {
                    "model_id": "basins_qhh_shud",
                    "basin_id": "qhh",
                    "basin_version_id": "qhh_vbasins",
                    "river_network_version_id": "qhh_rivnet_vbasins",
                    "model_package_uri": "s3://pkg/package/",
                    "resource_profile": {},
                    "duplicate_reason": "model_id",
                },
                {
                    "model_id": "other_model",
                    "basin_id": "other_basin",
                    "basin_version_id": "other_basin_v1",
                    "river_network_version_id": "other_rivnet_v1",
                    "model_package_uri": "s3://pkg/package/",
                    "resource_profile": {},
                    "duplicate_reason": "model_package_uri",
                },
            ]

    sources = SimpleNamespace(
        manifest={
            "model_package_uri": "s3://pkg/package/",
            "package_checksum": "package-sha",
            "source_inventory_checksum": "inventory-sha",
        },
        model={"basin_slug": "qhh", "shud_input_name": "qhh"},
        ids={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
        },
    )

    rows = qhh_bootstrap._active_qhh_identity_rows(FakeCursor(), sources, model_id="basins_qhh_shud")

    assert rows == [
        {
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
            "duplicate_reason": "model_id",
        },
        {
            "model_id": "other_model",
            "basin_id": "other_basin",
            "basin_version_id": "other_basin_v1",
            "river_network_version_id": "other_rivnet_v1",
            "duplicate_reason": "model_package_uri",
        },
    ]
    assert "mi.model_package_uri = %s" in executed["sql"]
    assert "mi.resource_profile->>'package_checksum' = %s" in executed["sql"]
    assert "source_inventory_checksum" not in executed["sql"]
    assert "s3://pkg/package/" in executed["params"]
    assert "package-sha" in executed["params"]


def test_duplicate_active_preflight_does_not_match_source_inventory_checksum_only() -> None:
    executed: dict[str, Any] = {}

    class FakeCursor:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            executed["sql"] = sql
            executed["params"] = params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    sources = SimpleNamespace(
        manifest={
            "model_package_uri": "s3://pkg/qhh/package/",
            "package_checksum": "qhh-package-sha",
            "source_inventory_checksum": "shared-inventory-sha",
        },
        model={"basin_slug": "qhh", "shud_input_name": "qhh"},
        ids={
            "model_id": "basins_qhh_shud",
            "basin_id": "qhh",
            "basin_version_id": "qhh_vbasins",
            "river_network_version_id": "qhh_rivnet_vbasins",
        },
    )

    rows = qhh_bootstrap._active_qhh_identity_rows(FakeCursor(), sources, model_id="basins_qhh_shud")

    assert rows == []
    assert "source_inventory_checksum" not in executed["sql"]
    assert "shared-inventory-sha" not in executed["params"]


def test_bootstrap_cli_outputs_typed_blocker_for_missing_database_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _input_dir, inventory_path, manifest_path, _model_id = _qhh_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _argparse_main(
        [
            "bootstrap-qhh-production",
            "--basins-root",
            str(root),
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "QHH_BOOTSTRAP_DATABASE_URL_MISSING"


@pytest.mark.integration
def test_bootstrap_qhh_production_success_idempotent_and_scheduler_ready(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-success"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        evidence_dir=evidence_dir,
        evidence_path=evidence_dir / "first.json",
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["active"] is True
    assert first["scheduler_readiness"]["ready"] is True
    assert first["station_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["output_segment_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["evidence_write_omitted"] is False
    assert first["output_segment_count"] == 2
    assert first["package_identity"]["manifest_uri"] == json.loads(manifest_path.read_text(encoding="utf-8"))[
        "manifest_uri"
    ]
    assert first["package_identity"]["model_package_uri"] == first["model_package_uri"]
    assert first["package_identity"]["package_checksum"]
    assert first["package_identity"]["manifest_sha256"]
    assert first["source_files"]["qhh_tsd_forc"]["station_count"] == 2
    assert first["source_files"]["qhh_sp_riv"]["output_segment_count"] == 2
    assert first["non_goal_proof"]["forcing_version_rows_created"] == 0
    assert first["non_goal_proof"]["forcing_station_timeseries_rows_created"] == 0
    assert first["non_goal_proof"] == {
        "forcing_version_rows_created": 0,
        "forcing_station_timeseries_rows_created": 0,
        "shud_runtime_executed": False,
        "slurm_submitted": False,
        "published_display_artifacts": False,
    }
    assert second["station_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert second["output_segment_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert second["package_identity"] == first["package_identity"]
    assert json.loads((evidence_dir / "first.json").read_text(encoding="utf-8"))["model_id"] == model_id

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mi.active_flag,
                       mi.lifecycle_state,
                       mi.shud_code_version,
                       mi.model_package_uri,
                       mi.resource_profile,
                       bv.basin_id
                FROM core.model_instance mi
                JOIN core.basin_version bv
                  ON bv.basin_version_id = mi.basin_version_id
                WHERE mi.model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM met.met_station
                WHERE properties_json->>'model_id' = %s
                  AND station_role = 'forcing_grid'
                """,
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                """,
                (first["river_network_version_id"],),
            )
            output_count = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
            forcing_count = int(cursor.fetchone()["count"])

    assert model["active_flag"] is True
    assert model["lifecycle_state"] == "active"
    assert model["shud_code_version"] == "basins-shud"
    assert model["resource_profile"]["runnable"] is True
    assert model["resource_profile"]["project_name"] == "qhh"
    assert model["resource_profile"]["shud_input_name"] == "qhh"
    assert model["resource_profile"]["model_id"] == model_id
    assert model["resource_profile"]["model_package_uri"] == first["model_package_uri"]
    assert model["resource_profile"]["package_checksum"] == first["package_identity"]["package_checksum"]
    assert model["resource_profile"]["source_inventory_checksum"] == first["package_identity"][
        "source_inventory_checksum"
    ]
    assert model["resource_profile"]["station_count"] == 2
    assert model["resource_profile"]["output_segment_count"] == 2
    assert model["resource_profile"]["qhh_tsd_forc_sha256"] == first["source_files"]["qhh_tsd_forc"]["sha256"]
    assert model["resource_profile"]["qhh_sp_riv_sha256"] == first["source_files"]["qhh_sp_riv"]["sha256"]
    assert "forcing_uri" not in model["resource_profile"]
    assert "forecast_cycle" not in model["resource_profile"]
    assert "publish_uri" not in model["resource_profile"]
    assert station_count == 2
    assert output_count == 2
    assert forcing_count == 0

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / "workspace",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    reasons = {item["reason"] for item in result.evidence["model_discovery"]["exclusions"]}
    scheduler_candidate = next(item for item in result.evidence["candidates"] if item["model_id"] == model_id)
    assert scheduler_candidate["output_segment_count"] == 2
    assert scheduler_candidate["resource_profile"]["output_segment_count"] == 2
    assert scheduler_candidate["resource_profile"]["project_name"] == "qhh"
    assert not {"not_shud_model", "not_runnable", "incomplete_model_metadata"} & reasons


@pytest.mark.integration
def test_bootstrap_station_failure_rolls_back_model_readiness(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-rollback-model"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            fail_after_model_metadata=True,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK"
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s AND active_flag = true",
                (model_id,),
            )
            active_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
    assert active_count == 0
    assert station_count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failure_flag", "failure_point"),
    [
        ("fail_during_station_seed", "station_seed"),
        ("fail_during_output_segment_seed", "output_segment_seed"),
    ],
)
def test_bootstrap_seed_failures_roll_back_rows_and_scheduler_visibility(
    tmp_path: Path,
    integration_database_url: str,
    failure_flag: str,
    failure_point: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = f"qhh-rollback-{failure_point.replace('_', '-')}"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            **{failure_flag: True},
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK"
    assert exc_info.value.details["failure_point"] == failure_point
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.model_instance
                WHERE model_id = %s
                  AND active_flag = true
                  AND COALESCE(lifecycle_state, 'active') = 'active'
                """,
                (model_id,),
            )
            active_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                  AND properties_json->>'model_id' = %s
                """,
                (model_id,),
            )
            output_count = int(cursor.fetchone()["count"])

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / f"workspace-{failure_point}",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()

    assert active_count == 0
    assert station_count == 0
    assert output_count == 0
    assert model_id not in {item["model_id"] for item in result.evidence["candidates"]}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("stale_kind", "error_code"),
    [
        ("station", "QHH_BOOTSTRAP_STALE_STATION_IDENTITY"),
        ("output", "QHH_BOOTSTRAP_STALE_OUTPUT_SEGMENT_IDENTITY"),
    ],
)
def test_bootstrap_blocks_stale_qhh_sibling_rows_before_scheduler_visibility(
    tmp_path: Path,
    integration_database_url: str,
    stale_kind: str,
    error_code: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = f"qhh-stale-{stale_kind}"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.model_instance
                SET active_flag = false,
                    lifecycle_state = 'inactive'
                WHERE model_id = %s
                """,
                (model_id,),
            )
            if stale_kind == "station":
                cursor.execute(
                    """
                    INSERT INTO met.met_station (
                        station_id,
                        basin_version_id,
                        station_name,
                        geom,
                        elevation_m,
                        station_role,
                        active_flag,
                        properties_json
                    )
                    VALUES (
                        'qhh_forc_999',
                        %s,
                        'Stale QHH forcing station',
                        ST_SetSRID(ST_MakePoint(100.9, 30.9), 4490),
                        0,
                        'forcing_grid',
                        true,
                        %s
                    )
                    """,
                    (
                        first["basin_version_id"],
                        Json(
                            {
                                "seed": "qhh_production_bootstrap",
                                "model_id": model_id,
                                "basin_version_id": first["basin_version_id"],
                                "source": "qhh.tsd.forc",
                            }
                        ),
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id,
                        river_network_version_id,
                        segment_order,
                        properties_json
                    )
                    VALUES (
                        %s,
                        %s,
                        999,
                        %s
                    )
                    """,
                    (
                        f"{model_id}_shud_riv_999999",
                        first["river_network_version_id"],
                        Json(
                            {
                                "seed": "qhh_production_bootstrap",
                                "model_id": model_id,
                                "basin_version_id": first["basin_version_id"],
                                "shud_output_river": True,
                            }
                        ),
                    ),
                )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == error_code
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / f"workspace-{stale_kind}",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT active_flag,
                       COALESCE(lifecycle_state, CASE WHEN active_flag THEN 'active' ELSE 'inactive' END)
                         AS lifecycle_state
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
            forcing_count = int(cursor.fetchone()["count"])

    assert model["active_flag"] is False
    assert model["lifecycle_state"] == "inactive"
    assert forcing_count == 0
    assert model_id not in {item["model_id"] for item in result.evidence["candidates"]}


@pytest.mark.integration
def test_bootstrap_replaces_stale_run_scoped_resource_profile_and_scheduler_derives_current_identity(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-stale-profile"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.model_instance
                SET active_flag = false,
                    lifecycle_state = 'inactive',
                    resource_profile = resource_profile || %s
                WHERE model_id = %s
                """,
                (
                    Json(
                        {
                            "canonical_product_id": "stale-canon",
                            "published_manifest_id": "stale-manifest",
                            "pipeline_job_id": "stale-job",
                            "output_uri": "s3://nhms/runs/stale/output/",
                            "partition": "debug",
                        }
                    ),
                    model_id,
                ),
            )

    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / "workspace-stale-profile",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    candidate = next(item for item in result.evidence["candidates"] if item["model_id"] == model_id)
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT resource_profile FROM core.model_instance WHERE model_id = %s", (model_id,))
            profile = cursor.fetchone()["resource_profile"]

    assert second["package_identity"] == first["package_identity"]
    assert not {"canonical_product_id", "published_manifest_id", "pipeline_job_id", "output_uri"} & set(profile)
    assert profile["partition"] == "standard"
    assert candidate["canonical_product_id"] == "canon_gfs_2026052106"
    assert candidate["published_manifest_id"] == f"manifest_fcst_gfs_2026052106_{model_id}"
    assert "pipeline_job_id" not in candidate["production_identity_contract"]["identity"]


@pytest.mark.integration
def test_bootstrap_duplicate_active_model_blocks_before_station_writes(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-duplicate-active"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group)
                VALUES ('basins_qhh_duplicate', 'Duplicate QHH', 'integration')
                """
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate',
                    'vbasins',
                    ST_Multi(ST_MakeEnvelope(100, 30, 101, 31, 4490)),
                    true,
                    'integration://qhh-duplicate',
                    'dup-basin-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    1,
                    'integration://qhh-duplicate-rivnet',
                    'dup-rivnet-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                )
                VALUES (
                    'basins_qhh_duplicate_mesh_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    'integration://qhh-duplicate-mesh',
                    'dup-mesh-sha',
                    %s
                )
                """,
                (Json({}),),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    model_package_uri,
                    active_flag,
                    lifecycle_state,
                    resource_profile
                )
                SELECT
                    'basins_qhh_shud_duplicate',
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_mesh_vbasins',
                    'duplicate-calib',
                    shud_code_version,
                    model_package_uri,
                    true,
                    'active',
                    %s
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (Json({}), model_id),
            )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DUPLICATE_ACTIVE_MODEL"
    assert any(item["duplicate_reason"] == "model_package_uri" for item in exc_info.value.details["active_models"])
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                ("basins_qhh_shud_duplicate",),
            )
            duplicate_station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                  AND properties_json->>'model_id' = %s
                """,
                ("basins_qhh_shud_duplicate",),
            )
            duplicate_output_count = int(cursor.fetchone()["count"])
    assert duplicate_station_count == 0
    assert duplicate_output_count == 0


@pytest.mark.integration
def test_registry_import_ignores_bootstrap_output_identity_rows_on_rerun(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-registry-rerun"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["registry_import"]["row_counts"]["river_segment"] == 2
    assert second["registry_import"]["row_counts"]["river_segment"] == 0
    assert second["status"] == "bootstrapped"


def _qhh_registry_fixture(tmp_path: Path, *, basin_slug: str = "qhh") -> tuple[Path, Path, Path, Path, str]:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug=basin_slug,
        sp_segment_count=2,
    )
    input_dir = _rename_fixture_input_to_qhh(input_dir)
    _write_valid_qhh_tsd_forc(input_dir)
    _refresh_inventory_and_manifest(tmp_path, root, inventory_path, manifest_path)
    return root, input_dir, inventory_path, manifest_path, model_id


def _rename_fixture_input_to_qhh(input_dir: Path) -> Path:
    for path in list(input_dir.glob("alias-a.*")):
        target = input_dir / path.name.replace("alias-a", "qhh", 1)
        path.rename(target)
    qhh_input_dir = input_dir.parent / "qhh"
    input_dir.rename(qhh_input_dir)
    return qhh_input_dir


def _write_valid_qhh_tsd_forc(input_dir: Path) -> None:
    input_dir.joinpath("qhh.tsd.forc").write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )


def _refresh_inventory_and_manifest(
    tmp_path: Path,
    root: Path,
    inventory_path: Path,
    manifest_path: Path,
) -> None:
    from tests.test_basins_registry_import import _package_manifest_for_model
    from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory

    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    manifest = _package_manifest_for_model(model, model["model_id"], inventory=inventory)
    manifest["package_checksum"] = f"package-sha-{model['model_id']}"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    del tmp_path
