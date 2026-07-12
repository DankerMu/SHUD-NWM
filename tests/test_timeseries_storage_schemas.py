import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_BASES = (
    "product_archive_manifest",
    "archive_completeness_receipt",
    "salvage_manifest",
    "archive_rebuild_drill_receipt",
    "timeseries_retention_receipt",
)


def _validator() -> str:
    validator = shutil.which("check-jsonschema")
    if validator is None:
        raise RuntimeError("check-jsonschema is required; run `uv sync --all-extras --dev`")
    return validator


def _document(base: str) -> dict[str, Any]:
    path = ROOT / "schemas" / "examples" / f"{base}.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_document(tmp_path: Path, base: str, document: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    candidate = tmp_path / f"{base}.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")
    return subprocess.run(
        [
            _validator(),
            "--schemafile",
            str(ROOT / "schemas" / f"{base}.schema.json"),
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("base", SCHEMA_BASES)
def test_timeseries_storage_schema_and_example_are_valid(tmp_path: Path, base: str) -> None:
    schema = ROOT / "schemas" / f"{base}.schema.json"
    metaschema = subprocess.run(
        [_validator(), "--check-metaschema", str(schema)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert metaschema.returncode == 0, metaschema.stdout + metaschema.stderr

    result = _validate_document(tmp_path, base, _document(base))
    assert result.returncode == 0, result.stdout + result.stderr


def test_completeness_receipt_requires_each_window_verdict(tmp_path: Path) -> None:
    document = _document("archive_completeness_receipt")
    del document["windows"][0]["verdict"]

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode != 0


def test_completeness_receipt_distinguishes_equal_window_sibling_subjects(tmp_path: Path) -> None:
    document = _document("archive_completeness_receipt")
    first, second = document["windows"]

    assert first["window"] == second["window"]
    assert first["subject"] != second["subject"]
    assert first["verdict"] == "gap"
    assert second["verdict"] == "complete"
    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("lane", "subject"),
    [
        ("forcing", {"forcing_version_id": "forcing-v1"}),
        ("runs", {"run_id": "run-42"}),
        ("states", {"state_id": "state-42"}),
    ],
)
def test_completeness_receipt_accepts_each_lane_subject(
    tmp_path: Path,
    lane: str,
    subject: dict[str, str],
) -> None:
    document = _document("archive_completeness_receipt")
    document["windows"] = [document["windows"][0]]
    document["windows"][0]["lane"] = lane
    document["windows"][0]["subject"] = subject

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("lane", "subject"),
    [
        ("forcing", None),
        ("forcing", {"run_id": "run-42"}),
        ("runs", {"forcing_version_id": "forcing-v1"}),
        ("states", {"run_id": "run-42"}),
        ("unknown", {"state_id": "state-42"}),
    ],
)
def test_completeness_receipt_rejects_missing_or_cross_lane_subject(
    tmp_path: Path,
    lane: str,
    subject: dict[str, str] | None,
) -> None:
    document = _document("archive_completeness_receipt")
    window = document["windows"][0]
    window["lane"] = lane
    if subject is None:
        del window["subject"]
    else:
        window["subject"] = subject

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode != 0


def test_completeness_receipt_requires_coverage_mechanism_separate_from_lane(tmp_path: Path) -> None:
    document = _document("archive_completeness_receipt")
    del document["windows"][0]["coverage"]

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("coverage", "verdict"),
    [
        ("product-archive", "complete"),
        ("db-export", "complete"),
        ("hot-object-store", "complete"),
        ("hot-object-store", "pending-archive"),
        ("none", "gap"),
    ],
)
def test_completeness_receipt_accepts_valid_coverage_verdict_pairs(
    tmp_path: Path,
    coverage: str,
    verdict: str,
) -> None:
    document = _document("archive_completeness_receipt")
    document["windows"] = [document["windows"][0]]
    document["windows"][0]["coverage"] = coverage
    document["windows"][0]["verdict"] = verdict

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("coverage", "verdict"),
    [
        ("product-archive", "pending-archive"),
        ("product-archive", "gap"),
        ("db-export", "pending-archive"),
        ("db-export", "gap"),
        ("hot-object-store", "gap"),
        ("none", "complete"),
        ("none", "pending-archive"),
    ],
)
def test_completeness_receipt_rejects_contradictory_coverage_verdict_pairs(
    tmp_path: Path,
    coverage: str,
    verdict: str,
) -> None:
    document = _document("archive_completeness_receipt")
    document["windows"] = [document["windows"][0]]
    document["windows"][0]["coverage"] = coverage
    document["windows"][0]["verdict"] = verdict

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("coverage", "verdict"),
    [
        ("product-archive", "complete"),
        ("hot-object-store", "complete"),
        ("hot-object-store", "pending-archive"),
        ("none", "gap"),
    ],
)
def test_state_completeness_receipt_accepts_non_salvage_coverage_pairs(
    tmp_path: Path,
    coverage: str,
    verdict: str,
) -> None:
    document = _document("archive_completeness_receipt")
    document["windows"] = [document["windows"][0]]
    document["windows"][0].update(
        {
            "lane": "states",
            "subject": {"state_id": "state-42"},
            "coverage": coverage,
            "verdict": verdict,
        }
    )

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_state_completeness_receipt_rejects_db_export_coverage(tmp_path: Path) -> None:
    document = _document("archive_completeness_receipt")
    document["windows"] = [document["windows"][0]]
    document["windows"][0].update(
        {
            "lane": "states",
            "subject": {"state_id": "state-42"},
            "coverage": "db-export",
            "verdict": "complete",
        }
    )

    result = _validate_document(tmp_path, "archive_completeness_receipt", document)
    assert result.returncode != 0


def test_product_archive_manifest_rejects_row_count(tmp_path: Path) -> None:
    document = _document("product_archive_manifest")
    document["row_count"] = 100

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize("lane", ["forcing", "runs"])
def test_product_archive_manifest_requires_producer_provenance_for_product_lanes(
    tmp_path: Path, lane: str
) -> None:
    document = _document("product_archive_manifest")
    if lane == "runs":
        document["identity"] = {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "run_id": "run-42",
        }
        parent = "runs/gfs/2026053100/run-42"
        document["archive"]["path"] = f"{parent}/archive.tar.zst"
        document["archive"]["manifest_path"] = f"{parent}/manifest.json"
    document.pop("producer")
    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("identity", "parent"),
    [
        (
            {
                "lane": "forcing",
                "source": "gfs",
                "cycle_identity": "2026053100",
                "cycle_time": "2026-05-31T00:00:00Z",
                "basin_version_id": "yangtze-v1",
                "model_id": "yangtze-shud-v12",
            },
            "forcing/gfs/2026053100/yangtze-v1/yangtze-shud-v12",
        ),
        (
            {
                "lane": "runs",
                "source": "gfs",
                "cycle_identity": "2026053100",
                "cycle_time": "2026-05-31T00:00:00Z",
                "run_id": "run-42",
            },
            "runs/gfs/2026053100/run-42",
        ),
        (
            {
                "lane": "states",
                "source": "gfs",
                "cycle_identity": "2026053100",
                "cycle_time": "2026-05-31T00:00:00Z",
                "model_id": "model-v1",
            },
            "states/gfs/2026053100/model-v1",
        ),
    ],
)
def test_product_archive_manifest_accepts_each_lane_identity(
    tmp_path: Path,
    identity: dict[str, str],
    parent: str,
) -> None:
    document = _document("product_archive_manifest")
    document["identity"] = identity
    document["archive"]["path"] = f"{parent}/archive.tar.zst"
    document["archive"]["manifest_path"] = f"{parent}/manifest.json"
    if identity["lane"] == "runs":
        document["producer"]["kind"] = "run-manifest"
        document["producer"]["subject_id"] = identity["run_id"]
        document["producer"]["manifest_path"] = "input/manifest.json"
    elif identity["lane"] == "states":
        document.pop("producer")

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "identity",
    [
        {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "model_id": "model-v1",
        },
        {
            "lane": "forcing",
            "source": "gfs/ifs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
            "run_id": "run-42",
        },
        {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "model_id": "model-v1",
        },
        {
            "lane": "states",
            "source": "gfs",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "model_id": "model-v1",
            "run_id": "run-42",
        },
    ],
)
def test_product_archive_manifest_rejects_unsafe_missing_or_cross_lane_identity(
    tmp_path: Path,
    identity: dict[str, str],
) -> None:
    document = _document("product_archive_manifest")
    document["identity"] = identity

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize("source", ["GFS", "era5", "ifs", "unknown-provider"])
def test_product_archive_manifest_rejects_noncanonical_source_ids(tmp_path: Path, source: str) -> None:
    document = _document("product_archive_manifest")
    document["identity"]["source"] = source

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize("source", ["gfs", "ERA5", "IFS"])
def test_product_archive_manifest_accepts_canonical_source_ids(tmp_path: Path, source: str) -> None:
    document = _document("product_archive_manifest")
    document["identity"]["source"] = source
    source_segment = source.lower()
    parent = f"forcing/{source_segment}/2026053100/yangtze-v1/yangtze-shud-v12"
    document["archive"]["path"] = f"{parent}/archive.tar.zst"
    document["archive"]["manifest_path"] = f"{parent}/manifest.json"

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_product_archive_manifest_accepts_legacy_unqualified_state_source(tmp_path: Path) -> None:
    document = _document("product_archive_manifest")
    parent = "states/legacy-unqualified/2026053100/model-v1"
    document["identity"] = {
        "lane": "states",
        "source": "legacy-unqualified",
        "cycle_identity": "2026053100",
        "cycle_time": "2026-05-31T00:00:00Z",
        "model_id": "model-v1",
    }
    document["archive"]["path"] = f"{parent}/archive.tar.zst"
    document["archive"]["manifest_path"] = f"{parent}/manifest.json"

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "identity",
    [
        {
            "lane": "forcing",
            "source": "legacy-unqualified",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        {
            "lane": "runs",
            "source": "legacy-unqualified",
            "cycle_identity": "2026053100",
            "cycle_time": "2026-05-31T00:00:00Z",
            "run_id": "run-42",
        },
    ],
)
def test_product_archive_manifest_rejects_legacy_unqualified_non_state_source(
    tmp_path: Path,
    identity: dict[str, str],
) -> None:
    document = _document("product_archive_manifest")
    document["identity"] = identity

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize("cycle_time", [None, "not-a-time", "2026-05-31T08:00:00+08:00"])
def test_product_archive_manifest_rejects_missing_invalid_or_non_utc_cycle_time(
    tmp_path: Path,
    cycle_time: str | None,
) -> None:
    document = _document("product_archive_manifest")
    if cycle_time is None:
        del document["identity"]["cycle_time"]
    else:
        document["identity"]["cycle_time"] = cycle_time

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


def test_salvage_manifest_requires_exported_row_count(tmp_path: Path) -> None:
    document = _document("salvage_manifest")
    del document["exports"][0]["exported_row_count"]

    result = _validate_document(tmp_path, "salvage_manifest", document)
    assert result.returncode != 0


def _selector(table: str, identity: dict[str, str]) -> dict[str, Any]:
    return {
        "table": table,
        "identity": identity,
        "window": {"start": "2026-05-28T00:00:00Z", "end": "2026-05-29T00:00:00Z"},
    }


@pytest.mark.parametrize(
    "selector",
    [
        _selector("met.forcing_station_timeseries", {"forcing_version_id": "forcing-v1"}),
        _selector("hydro.river_timeseries", {"run_id": "run-42"}),
    ],
)
@pytest.mark.parametrize("base", ["archive_completeness_receipt", "salvage_manifest"])
def test_completeness_and_salvage_selectors_accept_identical_exact_contracts(
    tmp_path: Path,
    base: str,
    selector: dict[str, Any],
) -> None:
    document = _document(base)
    if base == "archive_completeness_receipt":
        document["salvage_selectors"] = [selector]
    else:
        document["exports"][0]["selector"] = selector

    result = _validate_document(tmp_path, base, document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "selector",
    [
        _selector("met.forcing_station_timeseries", {"forcing_verison_id": "forcing-v1"}),
        _selector("met.forcing_station_timeseries", {"forcing_version_id": "forcing-v1", "extra": "x"}),
        _selector("met.forcing_station_timeseries", {"run_id": "run-42"}),
        _selector("hydro.river_timeseries", {"forcing_version_id": "forcing-v1"}),
        _selector("hydro.unknown_timeseries", {"run_id": "run-42"}),
    ],
)
@pytest.mark.parametrize("base", ["archive_completeness_receipt", "salvage_manifest"])
def test_completeness_and_salvage_selectors_reject_identical_invalid_contracts(
    tmp_path: Path,
    base: str,
    selector: dict[str, Any],
) -> None:
    document = _document(base)
    if base == "archive_completeness_receipt":
        document["salvage_selectors"] = [selector]
    else:
        document["exports"][0]["selector"] = selector

    result = _validate_document(tmp_path, base, document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("base", "field", "unsafe_path"),
    [
        ("product_archive_manifest", "archive", "/forcing/gfs/cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "C:/forcing/gfs/cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "forcing/gfs/../cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "forcing/gfs/./cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "forcing/gfs//cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "forcing\\gfs\\cycle\\archive.tar.zst"),
        ("product_archive_manifest", "archive", "forcing/gfs/\u0001cycle/archive.tar.zst"),
        ("product_archive_manifest", "archive", "other/gfs/cycle/archive.tar.zst"),
        ("product_archive_manifest", "file", "/nested/file.csv"),
        ("product_archive_manifest", "file", "nested/../file.csv"),
        ("product_archive_manifest", "file", "nested/./file.csv"),
        ("product_archive_manifest", "file", "nested//file.csv"),
        ("product_archive_manifest", "file", "nested\\file.csv"),
        ("product_archive_manifest", "file", "nested/\u0001file.csv"),
        ("salvage_manifest", "object", "/db-export/forcing/data.csv.zst"),
        ("salvage_manifest", "object", "C:/db-export/forcing/data.csv.zst"),
        ("salvage_manifest", "object", "db-export/forcing/../data.csv.zst"),
        ("salvage_manifest", "object", "db-export/forcing/./data.csv.zst"),
        ("salvage_manifest", "object", "db-export//data.csv.zst"),
        ("salvage_manifest", "object", "db-export\\forcing\\data.csv.zst"),
        ("salvage_manifest", "object", "db-export/forcing/\u0001data.csv.zst"),
        ("salvage_manifest", "object", "other/forcing/data.csv.zst"),
    ],
)
def test_archive_schema_paths_reject_absolute_traversal_or_unsafe_segments(
    tmp_path: Path,
    base: str,
    field: str,
    unsafe_path: str,
) -> None:
    document = _document(base)
    if field == "archive":
        document["archive"]["path"] = unsafe_path
    elif field == "file":
        document["files"][0]["path"] = unsafe_path
    else:
        document["exports"][0]["object"]["path"] = unsafe_path

    result = _validate_document(tmp_path, base, document)
    assert result.returncode != 0


def test_archive_schema_paths_accept_nested_root_relative_paths(tmp_path: Path) -> None:
    product = _document("product_archive_manifest")
    product["files"][0]["path"] = "nested/stations/X110.0Y30.0.csv"
    salvage = _document("salvage_manifest")
    salvage["exports"][0]["object"]["path"] = "db-export/forcing/version-v1/nested/data.csv.zst"

    for base, document in (("product_archive_manifest", product), ("salvage_manifest", salvage)):
        result = _validate_document(tmp_path, base, document)
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "object_path",
    [
        "db-export/forcing/data.json",
        "db-export/forcing/data.csv",
        "db-export/forcing/data.tar.zst",
    ],
)
def test_salvage_manifest_rejects_non_csv_zst_object_suffix(tmp_path: Path, object_path: str) -> None:
    document = _document("salvage_manifest")
    document["exports"][0]["object"]["path"] = object_path

    result = _validate_document(tmp_path, "salvage_manifest", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "missing",
    [
        "comparisons",
        "comparisons.cycles",
        "comparisons.selectors",
        "comparisons.counts",
        "staging_database",
        "coverage",
    ],
)
def test_drill_pass_requires_verdict_specific_details(tmp_path: Path, missing: str) -> None:
    document = _document("archive_rebuild_drill_receipt")
    if "." in missing:
        parent, child = missing.split(".", maxsplit=1)
        del document[parent][child]
    else:
        del document[missing]

    result = _validate_document(tmp_path, "archive_rebuild_drill_receipt", document)
    assert result.returncode != 0


def test_drill_fail_requires_per_item_differences(tmp_path: Path) -> None:
    document = _document("archive_rebuild_drill_receipt")
    document["verdict"] = "FAIL"
    del document["comparisons"]

    result = _validate_document(tmp_path, "archive_rebuild_drill_receipt", document)
    assert result.returncode != 0


def test_drill_fail_with_per_item_differences_is_valid(tmp_path: Path) -> None:
    document = _document("archive_rebuild_drill_receipt")
    document["verdict"] = "FAIL"
    del document["comparisons"]
    document["differences"] = [{"item": "run-42:river_stage", "expected": 24, "actual": 23}]

    result = _validate_document(tmp_path, "archive_rebuild_drill_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_product_only_drill_pass_accepts_required_empty_selector_list(tmp_path: Path) -> None:
    document = _document("archive_rebuild_drill_receipt")
    document["coverage"] = [item for item in document["coverage"] if item["source"] != "db-export"]
    document["comparisons"]["selectors"] = []

    result = _validate_document(tmp_path, "archive_rebuild_drill_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_retention_refusal_requires_reason(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.0",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "enforce",
        "outcome": "refused",
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


def test_retention_refusal_with_reason_is_valid(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.0",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "enforce",
        "outcome": "refused",
        "refusal_reason": "archive completeness receipt is stale",
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


def test_retention_dry_run_with_candidates_is_valid(tmp_path: Path) -> None:
    document = {
        "schema_version": "1.0",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "dry-run",
        "outcome": "dry-run",
        "candidate_chunks": ["_hyper_1_42_chunk"],
        "deferred_remainder": ["_hyper_1_43_chunk"],
    }

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid_variant", ["missing-candidates", "carries-dropped-chunks"])
def test_retention_dry_run_rejects_invalid_outcome_details(tmp_path: Path, invalid_variant: str) -> None:
    document = {
        "schema_version": "1.0",
        "generated_at": "2026-07-11T12:30:00Z",
        "mode": "dry-run",
        "outcome": "dry-run",
        "candidate_chunks": ["_hyper_1_42_chunk"],
        "deferred_remainder": [],
    }
    if invalid_variant == "missing-candidates":
        del document["candidate_chunks"]
    else:
        document["dropped_chunks"] = [{"name": "_hyper_1_42_chunk", "freed_bytes": 0}]

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "missing",
    [
        "dropped_chunks",
        "dropped_chunks.0.name",
        "dropped_chunks.0.freed_bytes",
        "deferred_remainder",
        "salvage_backed_windows",
    ],
)
def test_retention_enforce_requires_outcome_details(tmp_path: Path, missing: str) -> None:
    document = copy.deepcopy(_document("timeseries_retention_receipt"))
    if missing.startswith("dropped_chunks.0."):
        del document["dropped_chunks"][0][missing.rsplit(".", maxsplit=1)[1]]
    else:
        del document[missing]

    result = _validate_document(tmp_path, "timeseries_retention_receipt", document)
    assert result.returncode != 0


def test_display_api_has_no_archive_resolver_dependency() -> None:
    display_sources = (ROOT / "apps" / "api").rglob("*.py")
    forbidden = (
        "resolve_archive_root",
        "resolve_archive_storage_config",
        "archive_provenance_paths",
        "archive_identity_for_state_reference",
    )

    for source in display_sources:
        content = source.read_text(encoding="utf-8")
        assert all(symbol not in content for symbol in forbidden), source
