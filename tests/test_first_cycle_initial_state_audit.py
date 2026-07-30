"""Tests for the #1164 read-only first-cycle initial-state audit tool.

The tool reconciles "does the model package ship a qualified ``*.cfg.ic``?"
against "what did the earliest business run actually do?" and emits a
schema-versioned receipt.  It is an operator forensics tool for the stock
defect (#1164): six basins onboarded on 2026-07-05 silently cold-started even
though their packages carried non-zero calibrated ICs.

Requirement coverage (spec delta "A read-only audit SHALL reconcile packaged-IC
qualification against first-run evidence"):
- defect rows are reproduced per model x source (``cold_start_with_qualified_ic``);
- consumption and no-IC rows classify correctly;
- missing run evidence degrades to ``undetermined`` rather than guessing;
- the receipt carries the manifest-digest-only ``limits`` statement;
- nothing outside the receipt root is written.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from packages.common.source_identity import normalize_source_id
from scripts import audit_first_cycle_initial_state as audit

EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _package_manifest(
    model_id: str,
    *,
    ic_sha256: str | None = None,
    ic_size_bytes: int = 131072,
    include_ic: bool = True,
) -> dict[str, Any]:
    included_files: list[dict[str, Any]] = [
        {
            "relative_path": f"{model_id}.sp.mesh",
            "role": "shud_input",
            "size_bytes": 4096,
            "sha256": _sha256(b"mesh"),
        }
    ]
    if include_ic:
        included_files.append(
            {
                "relative_path": f"{model_id}.cfg.ic",
                "role": "shud_input",
                "size_bytes": ic_size_bytes,
                "sha256": ic_sha256 or _sha256(model_id.encode("utf-8")),
            }
        )
    return {
        "schema_version": "nhms.basins_package_manifest.v1",
        "model_id": model_id,
        "version": "vbasins-test",
        "package_checksum": _sha256(f"package-{model_id}".encode()),
        "included_files": included_files,
    }


def _run_manifest(
    *,
    run_id: str,
    model_id: str,
    source_id: str,
    cycle_time: str,
    quality: str,
    init_mode: int,
    packaged_ic_checksum: str | None = None,
) -> dict[str, Any]:
    initial_state: dict[str, Any] = {
        "state_id": None,
        "ic_file_uri": None,
        "valid_time": None,
        "checksum": None,
        "quality": quality,
    }
    if packaged_ic_checksum is not None:
        initial_state["packaged_ic_checksum"] = packaged_ic_checksum
    return {
        "run_id": run_id,
        "run_type": "forecast",
        "source_id": source_id,
        "cycle_time": cycle_time,
        "model": {"model_id": model_id},
        "initial_state": initial_state,
        "runtime": {"init_mode": init_mode},
    }


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.object_store_root = tmp_path / "object-store"
        self.workspace_root = tmp_path / "workspace"
        self.registry_manifest = tmp_path / "registry" / "registry_manifest.json"
        self.receipt_path = tmp_path / "receipts" / "audit.json"
        self.object_store_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._models: list[dict[str, Any]] = []

    def add_model(self, model_id: str, package_manifest: dict[str, Any] | None) -> None:
        manifest_key = f"models/{model_id}/manifest.json"
        if package_manifest is not None:
            _write_json(self.object_store_root / manifest_key, package_manifest)
        self._models.append(
            {
                "model_id": model_id,
                "basin_id": model_id,
                "model_package_uri": f"s3://nhms/models/{model_id}/package/",
                "manifest_uri": f"s3://nhms/{manifest_key}",
                "package_checksum": _sha256(f"package-{model_id}".encode()),
                "resource_profile": {
                    "manifest_uri": f"s3://nhms/{manifest_key}",
                    "package_checksum": _sha256(f"package-{model_id}".encode()),
                },
            }
        )

    def add_object_store_run(self, manifest: dict[str, Any]) -> None:
        _write_json(
            self.object_store_root / "runs" / str(manifest["run_id"]) / "input" / "manifest.json",
            manifest,
        )

    def add_workspace_run(self, manifest: dict[str, Any]) -> None:
        _write_json(
            self.workspace_root / "runs" / str(manifest["run_id"]) / "input" / "manifest.json",
            manifest,
        )

    def publish_registry(self) -> None:
        _write_json(
            self.registry_manifest,
            {
                "schema_version": "nhms.scheduler.registry_manifest.v1",
                "generated_at": "2026-07-30T00:00:00Z",
                "models": self._models,
            },
        )

    def argv(self, *extra: str) -> list[str]:
        return [
            "--registry-manifest",
            str(self.registry_manifest),
            "--object-store-root",
            str(self.object_store_root),
            "--object-store-prefix",
            "s3://nhms",
            "--workspace-root",
            str(self.workspace_root),
            "--receipt-path",
            str(self.receipt_path),
            *extra,
        ]

    def receipt(self) -> dict[str, Any]:
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))

    def tree_digest(self) -> str:
        digest = hashlib.sha256()
        for base in (self.object_store_root, self.workspace_root, self.registry_manifest.parent):
            for path in sorted(base.rglob("*")):
                digest.update(str(path.relative_to(self.root)).encode("utf-8"))
                if path.is_file():
                    digest.update(path.read_bytes())
        return digest.hexdigest()


def _row(receipt: dict[str, Any], model_id: str, source: str) -> dict[str, Any]:
    # Rows carry the canonical source identity (``normalize_source_id`` keeps
    # ``gfs`` lower-case but upper-cases ``IFS``), so normalize the lookup too.
    canonical_source = normalize_source_id(source)
    matches = [
        row
        for row in receipt["rows"]
        if row["model_id"] == model_id and row["source"] == canonical_source
    ]
    assert len(matches) == 1, f"expected exactly one row for {model_id}/{source}"
    return matches[0]


def test_audit_reproduces_stock_defect_rows_per_model_and_source(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    ic_sha256 = _sha256(b"calibrated-ic")
    fixture.add_model("dth_ls", _package_manifest("dth_ls", ic_sha256=ic_sha256))
    for source in ("gfs", "ifs"):
        fixture.add_object_store_run(
            _run_manifest(
                run_id=f"fcst_{source}_2026070500_dth_ls",
                model_id="dth_ls",
                source_id=source.upper(),
                cycle_time="2026-07-05T00:00:00Z",
                quality="cold_start_no_state",
                init_mode=1,
            )
        )
    fixture.publish_registry()

    assert audit.main(fixture.argv()) == 0

    receipt = fixture.receipt()
    assert receipt["schema_version"] == audit.SCHEMA_VERSION
    assert receipt["outcome"] == "completed"
    assert len(receipt["rows"]) == 2
    for source in ("gfs", "ifs"):
        row = _row(receipt, "dth_ls", source)
        assert row["verdict"] == "cold_start_with_qualified_ic"
        assert row["ic_qualified"] is True
        assert row["first_cycle"] == "2026070500"
        assert row["first_run_quality"] == "cold_start_no_state"
        assert row["first_run_init_mode"] == 1
    assert receipt["totals"]["cold_start_with_qualified_ic"] == 2


def test_audit_classifies_consumption_and_missing_ic_and_missing_evidence(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    consumed_sha256 = _sha256(b"consumed-ic")
    fixture.add_model("consumed_model", _package_manifest("consumed_model", ic_sha256=consumed_sha256))
    fixture.add_model("no_ic_model", _package_manifest("no_ic_model", include_ic=False))
    fixture.add_model("zero_ic_model", _package_manifest("zero_ic_model", ic_size_bytes=0))
    fixture.add_model("no_run_model", _package_manifest("no_run_model"))
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_consumed_model",
            model_id="consumed_model",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="packaged_calibrated_state",
            init_mode=3,
            packaged_ic_checksum=consumed_sha256,
        )
    )
    for model_id in ("no_ic_model", "zero_ic_model"):
        fixture.add_object_store_run(
            _run_manifest(
                run_id=f"fcst_gfs_2026070500_{model_id}",
                model_id=model_id,
                source_id="GFS",
                cycle_time="2026-07-05T00:00:00Z",
                quality="cold_start_no_state",
                init_mode=1,
            )
        )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    receipt = fixture.receipt()
    assert _row(receipt, "consumed_model", "gfs")["verdict"] == "consumed_package_ic"
    assert _row(receipt, "no_ic_model", "gfs")["verdict"] == "cold_start_no_ic"
    assert _row(receipt, "zero_ic_model", "gfs")["verdict"] == "cold_start_no_ic"
    no_run = _row(receipt, "no_run_model", "gfs")
    assert no_run["verdict"] == "undetermined"
    assert no_run["first_cycle"] is None
    assert no_run["first_run_quality"] is None


def test_audit_unreadable_package_manifest_is_undetermined_not_no_ic(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("broken_model", None)  # registry references a manifest that is absent
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_broken_model",
            model_id="broken_model",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "broken_model", "gfs")
    assert row["ic_qualified"] is None
    assert row["verdict"] == "undetermined"


def test_audit_picks_the_earliest_cycle_across_workspace_and_object_store(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("dth_zj", _package_manifest("dth_zj"))
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_dth_zj",
            model_id="dth_zj",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="fresh",
            init_mode=3,
        )
    )
    # Earlier cycle lives only in the workspace lane.
    fixture.add_workspace_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_dth_zj",
            model_id="dth_zj",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "dth_zj", "gfs")
    assert row["first_cycle"] == "2026070500"
    assert row["verdict"] == "cold_start_with_qualified_ic"


def test_audit_receipt_declares_manifest_digest_only_limits(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("dth_ls", _package_manifest("dth_ls"))
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    limits = fixture.receipt()["limits"]
    assert limits["package_objects_rehashed"] is False
    assert "manifest" in str(limits["note"]).lower()


def test_audit_writes_nothing_outside_the_receipt_path(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("dth_ls", _package_manifest("dth_ls"))
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_dth_ls",
            model_id="dth_ls",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()
    before = fixture.tree_digest()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    assert fixture.tree_digest() == before
    assert fixture.receipt_path.is_file()
    produced = sorted(
        path for path in fixture.receipt_path.parent.rglob("*") if path.is_file()
    )
    assert produced == [fixture.receipt_path]


def test_audit_requires_absolute_paths(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("dth_ls", _package_manifest("dth_ls"))
    fixture.publish_registry()

    assert audit.main(["--registry-manifest", "relative/registry.json", "--receipt-path", "x.json"]) == 1


def test_audit_verdicts_are_a_closed_set() -> None:
    assert audit.VERDICTS == (
        "consumed_package_ic",
        "cold_start_with_qualified_ic",
        "cold_start_no_ic",
        "undetermined",
    )


@pytest.mark.parametrize(
    ("quality", "init_mode", "ic_qualified", "expected"),
    [
        ("packaged_calibrated_state", 3, True, "consumed_package_ic"),
        ("cold_start_no_state", 1, True, "cold_start_with_qualified_ic"),
        ("cold_start_no_state", 1, False, "cold_start_no_ic"),
        ("cold_start_stale_state", 1, True, "cold_start_with_qualified_ic"),
        ("fresh", 3, True, "undetermined"),
        (None, None, True, "undetermined"),
        ("cold_start_no_state", 1, None, "undetermined"),
    ],
)
def test_audit_verdict_table(
    quality: str | None, init_mode: int | None, ic_qualified: bool | None, expected: str
) -> None:
    assert (
        audit.classify_verdict(
            ic_qualified=ic_qualified,
            first_run_quality=quality,
            first_run_init_mode=init_mode,
        )
        == expected
    )
