from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.integration_helpers import apply_migrations_from_zero
from workers.model_registry.basins_reingest import BasinsReingestError, reingest_basin
from workers.model_registry.cli import _argparse_main

_QHH_SAMPLE_DIR = Path(__file__).parent / "fixtures" / "basins" / "qhh-sample"


@pytest.fixture(autouse=True)
def _publish_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``publish_basins_package`` requires OBJECT_STORE_ROOT/PREFIX even when
    # PR 3's reingest path doesn't materialize artifacts (copy_forcing=False) —
    # the env vars gate the publication preflight. Point at tmp_path so each
    # test is self-contained and doesn't leak into the real object store.
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")


# ---------------------------------------------------------------------------
# Reingest happy-path / idempotency / missing-basin coverage (real DB).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reingest_basin_happy_path(
    integration_database_url: str,
    tmp_path: Path,
) -> None:
    """First-pass reingest: receipt schema is complete, reach + crosswalk rows
    landed, and no geom is null / multi-part."""

    apply_migrations_from_zero(integration_database_url)
    basins_root, basin_slug, model_id = _stage_qhh_sample_basin(tmp_path)
    work_dir = tmp_path / "work"
    receipt_path = tmp_path / "receipt.json"

    receipt = reingest_basin(
        basin_slug=basin_slug,
        model_id=model_id,
        package_version=f"vbasins-reingest-{tmp_path.name}",
        basins_root=basins_root,
        database_url=integration_database_url,
        work_dir=work_dir,
        output_path=receipt_path,
        auth_actor_id="cli-model-admin",
        auth_roles=["model_admin"],
    )

    _assert_receipt_schema(receipt)
    assert receipt["basin_slug"] == basin_slug
    assert receipt["model_id"] == model_id
    assert receipt["river_shp_record_count"] == 5
    assert receipt["seg_shp_record_count"] == 18
    assert receipt["sp_riv_reach_count"] == 5
    assert receipt["imported_reach_count"] > 0
    assert receipt["crosswalk_row_count"] > 0
    assert receipt["geom_null_count"] == 0
    assert receipt["multi_part_violation_count"] == 0
    assert receipt["basin_id"] is not None

    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert on_disk == receipt


@pytest.mark.integration
def test_reingest_basin_is_idempotent(
    integration_database_url: str,
    tmp_path: Path,
) -> None:
    """Re-running reingest with the *same* package_version is the real
    operator-recovery scenario (SSH dropped mid-run, operator re-runs the
    same command). Verify identical inputs produce identical DB-derived
    counts; only the wall-clock receipt timestamps advance."""

    apply_migrations_from_zero(integration_database_url)
    basins_root, basin_slug, model_id = _stage_qhh_sample_basin(tmp_path)
    package_version = f"vbasins-reingest-{tmp_path.name}"

    work_dir_a = tmp_path / "work-a"
    receipt_path_a = tmp_path / "receipt-a.json"
    first = reingest_basin(
        basin_slug=basin_slug,
        model_id=model_id,
        package_version=package_version,
        basins_root=basins_root,
        database_url=integration_database_url,
        work_dir=work_dir_a,
        output_path=receipt_path_a,
    )

    work_dir_b = tmp_path / "work-b"
    receipt_path_b = tmp_path / "receipt-b.json"
    second = reingest_basin(
        basin_slug=basin_slug,
        model_id=model_id,
        package_version=package_version,
        basins_root=basins_root,
        database_url=integration_database_url,
        work_dir=work_dir_b,
        output_path=receipt_path_b,
    )
    assert first["imported_reach_count"] == second["imported_reach_count"]
    assert first["crosswalk_row_count"] == second["crosswalk_row_count"]
    assert first["geom_null_count"] == 0
    assert second["geom_null_count"] == 0
    assert first["multi_part_violation_count"] == 0
    assert second["multi_part_violation_count"] == 0
    # Timestamps strictly advance even when DB-derived counts stay identical.
    assert second["started_at"] > first["started_at"]
    assert second["finished_at"] > first["finished_at"]


@pytest.mark.integration
def test_reingest_basin_missing_basin_slug_raises(
    integration_database_url: str,
    tmp_path: Path,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basins_root, real_slug, _ = _stage_qhh_sample_basin(tmp_path)
    bogus_slug = f"{real_slug}-does-not-exist"

    with pytest.raises(BasinsReingestError) as excinfo:
        reingest_basin(
            basin_slug=bogus_slug,
            model_id="basins_does_not_exist_shud",
            package_version="vbasins-reingest-missing",
            basins_root=basins_root,
            database_url=integration_database_url,
            work_dir=tmp_path / "work",
            output_path=tmp_path / "receipt.json",
        )
    payload = excinfo.value.to_payload()
    assert payload["error_code"] == "BASINS_REINGEST_BASIN_NOT_FOUND"
    assert payload["basin_slug"] == bogus_slug
    assert real_slug in payload.get("available_basin_slugs", [])


@pytest.mark.integration
def test_aggregate_script_writes_totals(
    integration_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage qhh-sample under two distinct slugs in the same root, invoke
    the aggregate script's main(), and assert the totals sum across basins."""

    apply_migrations_from_zero(integration_database_url)
    basins_root = tmp_path / "basins"
    basins_root.mkdir()
    slug_a, model_id_a = _stage_qhh_sample_into_root(basins_root, suffix="alpha")
    slug_b, model_id_b = _stage_qhh_sample_into_root(basins_root, suffix="beta")

    from scripts.reingest_all_basins_receipt import main as aggregate_main

    output_path = tmp_path / "aggregate.json"
    work_dir = tmp_path / "agg-work"
    monkeypatch.setenv("NHMS_CLI_AUTH_ACTOR_ID", "cli-model-admin")
    monkeypatch.setenv("NHMS_CLI_AUTH_ROLES", "model_admin")
    rc = aggregate_main(
        [
            "--basins-root",
            str(basins_root),
            "--database-url",
            integration_database_url,
            "--work-dir",
            str(work_dir),
            "--output",
            str(output_path),
            "--package-version",
            f"vbasins-reingest-agg-{tmp_path.name}",
            "--basin-slug",
            slug_a,
            "--basin-slug",
            slug_b,
            "--model-id-template",
            "basins_{slug}_shud",
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )
    assert rc == 0
    aggregate = json.loads(output_path.read_text(encoding="utf-8"))
    assert aggregate["schema_version"] == "basins.reingest_aggregate.v1"
    assert len(aggregate["basins"]) == 2
    assert aggregate["totals"]["failure_count"] == 0
    per_basin_sum = sum(int(b["imported_reach_count"]) for b in aggregate["basins"])
    assert aggregate["totals"]["imported_reach_count"] == per_basin_sum
    crosswalk_sum = sum(int(b["crosswalk_row_count"]) for b in aggregate["basins"])
    assert aggregate["totals"]["crosswalk_row_count"] == crosswalk_sum
    seen_model_ids = {b["model_id"] for b in aggregate["basins"]}
    assert seen_model_ids == {model_id_a, model_id_b}


# ---------------------------------------------------------------------------
# CLI surface coverage (argparse path runs without Click installed).
# ---------------------------------------------------------------------------


def test_reingest_basin_cli_help_lists_required_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--help` succeeds on the argparse path and lists the contract options."""

    with pytest.raises(SystemExit) as excinfo:
        _argparse_main(["reingest-basin", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--basin-slug" in out
    assert "--model-id" in out
    assert "--package-version" in out
    assert "--work-dir" in out
    assert "--output" in out


# ---------------------------------------------------------------------------
# qhh-sample fixture staging helpers
# ---------------------------------------------------------------------------


def _stage_qhh_sample_basin(tmp_path: Path) -> tuple[Path, str, str]:
    """Stage the qhh-sample fixture under tmp_path/basins/<slug>/ and return
    (basins_root, basin_slug, model_id). Slug derived from tmp_path so each
    test run gets a unique row identity (no cross-test CHECKSUM_CONFLICT)."""

    basins_root = tmp_path / "basins"
    basins_root.mkdir(exist_ok=True)
    basin_slug = f"qhh-sample-{tmp_path.name}".replace("_", "-").lower()
    _stage_qhh_sample_basin_dir(basins_root, basin_slug)
    model_id = f"basins_{_slug_id(basin_slug)}_shud"
    return basins_root, basin_slug, model_id


def _stage_qhh_sample_into_root(basins_root: Path, *, suffix: str) -> tuple[str, str]:
    """Stage qhh-sample under an existing root with a unique slug suffix."""

    basin_slug = f"qhh-sample-{suffix}-{basins_root.parent.name}".replace("_", "-").lower()
    _stage_qhh_sample_basin_dir(basins_root, basin_slug)
    model_id = f"basins_{_slug_id(basin_slug)}_shud"
    return basin_slug, model_id


def _stage_qhh_sample_basin_dir(basins_root: Path, basin_slug: str) -> None:
    """Stage SHUD canonical files + qhh-sample shapefiles for one basin."""

    input_name = f"alias-{basin_slug}".replace("_", "-").lower()
    input_dir = basins_root / basin_slug / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.ic",
        "cfg.calib",
        "sp.mesh",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
        "tsd.rl",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.riv", input_dir / f"{input_name}.sp.riv")
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.rivseg", input_dir / f"{input_name}.sp.rivseg")
    # Header normalisation: source declares the full production counts (1633
    # reaches / 3738 segments); rewrite to match the 5-record sample.
    sp_riv_path = input_dir / f"{input_name}.sp.riv"
    sp_riv_text = sp_riv_path.read_text(encoding="utf-8").splitlines()
    sp_riv_text[0] = "5 6"
    sp_riv_path.write_text("\n".join(sp_riv_text) + "\n", encoding="utf-8")
    sp_rivseg_path = input_dir / f"{input_name}.sp.rivseg"
    sp_rivseg_text = sp_rivseg_path.read_text(encoding="utf-8").splitlines()
    sp_rivseg_text[0] = "18 4"
    sp_rivseg_path.write_text("\n".join(sp_rivseg_text) + "\n", encoding="utf-8")
    gis_dst = input_dir / "gis"
    gis_dst.mkdir()
    for layer in ("river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            shutil.copy2(
                _QHH_SAMPLE_DIR / "gis" / f"{layer}.{suffix}",
                gis_dst / f"{layer}.{suffix}",
            )
    _write_domain_shapefile(gis_dst / "domain")
    forcing = basins_root / basin_slug / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")


def _write_domain_shapefile(base: Path) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    outer = [(100.0, 30.0), (101.0, 30.0), (101.0, 31.0), (100.0, 31.0)]
    closed_outer = [list(point) for point in [*outer, outer[0]]]
    writer.poly([closed_outer])
    writer.record(1)
    writer.close()
    base.with_suffix(".prj").write_text(
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n',
        encoding="utf-8",
    )


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _assert_receipt_schema(receipt: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "basin_slug",
        "model_id",
        "package_version",
        "started_at",
        "finished_at",
        "river_shp_record_count",
        "seg_shp_record_count",
        "sp_riv_reach_count",
        "basin_id",
        "imported_reach_count",
        "crosswalk_row_count",
        "geom_null_count",
        "max_edge_meters_observed",
        "multi_part_violation_count",
        "tile_cache_purged_count",
    }
    missing = expected_keys - set(receipt.keys())
    assert not missing, f"receipt is missing required keys: {sorted(missing)}"
    assert receipt["schema_version"] == "basins.reingest.v1"
    assert receipt["tile_cache_purged_count"] == 0
    assert receipt["max_edge_meters_observed"] is None


