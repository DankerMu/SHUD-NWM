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
        pytest.skip("check-jsonschema is not installed in this environment")
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


def test_product_archive_manifest_rejects_row_count(tmp_path: Path) -> None:
    document = _document("product_archive_manifest")
    document["row_count"] = 100

    result = _validate_document(tmp_path, "product_archive_manifest", document)
    assert result.returncode != 0


def test_salvage_manifest_requires_exported_row_count(tmp_path: Path) -> None:
    document = _document("salvage_manifest")
    del document["exports"][0]["exported_row_count"]

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
    forbidden = ("resolve_archive_root", "resolve_archive_storage_config", "archive_provenance_paths")

    for source in display_sources:
        content = source.read_text(encoding="utf-8")
        assert all(symbol not in content for symbol in forbidden), source
