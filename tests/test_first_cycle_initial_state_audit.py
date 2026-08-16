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
import os
from pathlib import Path
from typing import Any

import pytest

from packages.common.source_identity import normalize_source_id
from scripts import audit_first_cycle_initial_state as audit

EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _variant_package_key(model_id: str) -> str:
    """Store-relative package directory of a direct-grid variant registry row."""
    return f"models/direct_grid_variants/{model_id}/dg-gfs-abcdef123456/package"


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

    def add_direct_grid_variant_model(
        self,
        model_id: str,
        *,
        shud_input_name: str,
        ic_content: bytes | None,
    ) -> None:
        """Register a production-shaped direct-grid variant row.

        The variant manifest carries ONLY ``direct_grid_forcing`` (no
        ``included_files``), so qualification can only come from the tier-(b)
        probe of ``{model_package_uri}{shud_input_name}.cfg.ic``.  This is the
        shape all 36 production registry rows currently have.
        """
        package_key = _variant_package_key(model_id)
        manifest_key = f"{package_key}/manifest.json"
        _write_json(
            self.object_store_root / manifest_key,
            {
                "direct_grid_forcing": {
                    "forcing_mapping_mode": "direct_grid",
                    "binding_uri": f"s3://nhms/{package_key}/direct_grid_binding.json",
                    "model_input_package_id": "dg-input-abcdef123456",
                    "applicable_source_ids": ["gfs", "IFS"],
                    "grid_id": "gfs_0p25",
                    "station_bindings": [],
                }
            },
        )
        if ic_content is not None:
            ic_path = self.object_store_root / package_key / f"{shud_input_name}.cfg.ic"
            ic_path.parent.mkdir(parents=True, exist_ok=True)
            ic_path.write_bytes(ic_content)
        self._models.append(
            {
                "model_id": model_id,
                "basin_id": model_id,
                "model_package_uri": f"s3://nhms/{package_key}/",
                "manifest_uri": f"s3://nhms/{manifest_key}",
                "package_checksum": _sha256(f"package-{model_id}".encode()),
                "resource_profile": {
                    "lineage": "direct_grid_variant_registration",
                    "manifest_uri": f"s3://nhms/{manifest_key}",
                    "model_package_uri": f"s3://nhms/{package_key}/",
                    "shud_input_name": shud_input_name,
                    "package_checksum": _sha256(f"package-{model_id}".encode()),
                },
            }
        )

    def add_baseline_package_model(
        self,
        model_id: str,
        *,
        shud_input_name: str,
        ic_content: bytes | None,
    ) -> Path:
        """Register an inventory-shaped baseline row AND lay down its IC object.

        The inventory tier decides such a row from recorded digests alone; the
        object exists here so the audit's own-layer content probe (#1197) has real
        bytes to read.  Returns the IC object path so a test can obstruct it.
        """
        package_key = f"models/{model_id}/package"
        manifest_key = f"{package_key}/manifest.json"
        ic_bytes = ic_content if ic_content is not None else b""
        _write_json(
            self.object_store_root / manifest_key,
            {
                "schema_version": "nhms.basins_package_manifest.v1",
                "model_id": model_id,
                "version": "vbasins-test",
                "package_checksum": _sha256(f"package-{model_id}".encode()),
                "shud_input_name": shud_input_name,
                "included_files": [
                    {
                        "relative_path": f"{shud_input_name}.sp.mesh",
                        "role": "shud_input",
                        "size_bytes": 4096,
                        "sha256": _sha256(b"mesh"),
                    },
                    {
                        "relative_path": f"{shud_input_name}.cfg.ic",
                        "role": "shud_input",
                        "size_bytes": max(len(ic_bytes), 1),
                        "sha256": _sha256(ic_bytes) if ic_bytes else _sha256(model_id.encode()),
                    },
                ],
            },
        )
        ic_path = self.object_store_root / package_key / f"{shud_input_name}.cfg.ic"
        if ic_content is not None:
            ic_path.parent.mkdir(parents=True, exist_ok=True)
            ic_path.write_bytes(ic_content)
        self._models.append(
            {
                "model_id": model_id,
                "basin_id": model_id,
                "model_package_uri": f"s3://nhms/{package_key}/",
                "manifest_uri": f"s3://nhms/{manifest_key}",
                "package_checksum": _sha256(f"package-{model_id}".encode()),
                "resource_profile": {
                    "manifest_uri": f"s3://nhms/{manifest_key}",
                    "model_package_uri": f"s3://nhms/{package_key}/",
                    "shud_input_name": shud_input_name,
                    "package_checksum": _sha256(f"package-{model_id}".encode()),
                },
            }
        )
        return ic_path

    def variant_ic_path(self, model_id: str, shud_input_name: str) -> Path:
        """Absolute path of the canonical IC object probed for a variant row."""
        return self.object_store_root / _variant_package_key(model_id) / f"{shud_input_name}.cfg.ic"

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
        # A sweep that enumerated both lanes and read the earliest manifest is
        # complete, so the verdict is allowed to be a confident one.
        assert row["first_run_evidence_complete"] is True
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


def test_audit_unlistable_run_lane_does_not_promote_a_later_cycle_to_first(tmp_path: Path) -> None:
    """An un-enumerable lane is not "this lane holds no runs".

    The object-store lane holds the TRUE first cycle (a defect cold start) but
    its lane root cannot be listed (the no-follow lister refuses a symlinked
    directory, the same class as EACCES / NFS EIO).  Folding that into an empty
    lane promotes the later workspace cycle to "first" and reports a confident
    ``consumed_package_ic`` — a clean verdict for a basin whose real first cycle
    was never read.
    """
    fixture = _Fixture(tmp_path)
    ic_sha256 = _sha256(b"calibrated-ic")
    fixture.add_model("hidden_lane", _package_manifest("hidden_lane", ic_sha256=ic_sha256))
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_hidden_lane",
            model_id="hidden_lane",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    runs_root = fixture.object_store_root / "runs"
    detached = fixture.object_store_root / "runs-detached"
    runs_root.rename(detached)
    runs_root.symlink_to(detached)
    fixture.add_workspace_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_hidden_lane",
            model_id="hidden_lane",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="packaged_calibrated_state",
            init_mode=3,
            packaged_ic_checksum=ic_sha256,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "hidden_lane", "gfs")
    assert row["verdict"] == "undetermined"
    assert row["first_run_evidence_complete"] is False


def test_audit_unparseable_earlier_run_manifest_does_not_promote_a_later_cycle(
    tmp_path: Path,
) -> None:
    """A present-but-unparseable earlier manifest leaves the sweep incomplete."""
    fixture = _Fixture(tmp_path)
    ic_sha256 = _sha256(b"calibrated-ic")
    fixture.add_model("broken_first_run", _package_manifest("broken_first_run", ic_sha256=ic_sha256))
    broken = (
        fixture.object_store_root
        / "runs"
        / "fcst_gfs_2026070500_broken_first_run"
        / "input"
        / "manifest.json"
    )
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{ not json", encoding="utf-8")
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_broken_first_run",
            model_id="broken_first_run",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="packaged_calibrated_state",
            init_mode=3,
            packaged_ic_checksum=ic_sha256,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "broken_first_run", "gfs")
    assert row["verdict"] == "undetermined"
    assert row["first_run_evidence_complete"] is False


@pytest.mark.parametrize(
    "obstruction",
    ["symlink", "directory", "unreadable_parent"],
    ids=["symlinked_manifest", "directory_at_manifest", "eacces_input_dir"],
)
def test_audit_undecidable_earlier_run_manifest_stat_does_not_promote_a_later_cycle(
    tmp_path: Path, obstruction: str
) -> None:
    """The stat half of the run-manifest sweep must be three-way, like the parse half.

    The earlier cycle's ``input/manifest.json`` is PRESENT but cannot be
    stat-ed decidably: a symlink (``stat_no_follow`` refuses by policy), a
    directory at the manifest path (stat succeeds, not a regular file), or an
    ``input/`` directory the process cannot open (EACCES — the local stand-in for
    NFS ``EIO`` / permission drift on ``/ghdc/data/nwm``).  A two-way
    ``_is_regular_file`` guard folds all three into the same ``False`` as a
    genuinely manifest-less run directory, silently skips to the LATER cycle and
    still reports ``first_run_evidence_complete=true`` — i.e. a confident
    ``consumed_package_ic`` for a basin whose real first cycle was never read.
    That is exactly the fail-open shape #1164 exists to prevent.
    """
    if obstruction == "unreadable_parent" and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions, so EACCES cannot be provoked")

    fixture = _Fixture(tmp_path)
    ic_sha256 = _sha256(b"calibrated-ic")
    fixture.add_model("undecidable_first_run", _package_manifest("undecidable_first_run", ic_sha256=ic_sha256))
    earlier_input = (
        fixture.object_store_root
        / "runs"
        / "fcst_gfs_2026070500_undecidable_first_run"
        / "input"
    )
    earlier_input.mkdir(parents=True, exist_ok=True)
    manifest_path = earlier_input / "manifest.json"
    if obstruction == "symlink":
        real = fixture.object_store_root / "elsewhere_manifest.json"
        _write_json(
            real,
            _run_manifest(
                run_id="fcst_gfs_2026070500_undecidable_first_run",
                model_id="undecidable_first_run",
                source_id="GFS",
                cycle_time="2026-07-05T00:00:00Z",
                quality="cold_start_no_state",
                init_mode=1,
            ),
        )
        manifest_path.symlink_to(real)
    elif obstruction == "directory":
        manifest_path.mkdir()
    else:
        manifest_path.write_text("{}", encoding="utf-8")
        earlier_input.chmod(0o000)

    # The later cycle carries a clean consumption record; without a three-way
    # stat it would be promoted to "first" and the row would read
    # ``consumed_package_ic``.
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_undecidable_first_run",
            model_id="undecidable_first_run",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="packaged_calibrated_state",
            init_mode=3,
            packaged_ic_checksum=ic_sha256,
        )
    )
    fixture.publish_registry()

    try:
        assert audit.main(fixture.argv("--sources", "gfs")) == 0
        row = _row(fixture.receipt(), "undecidable_first_run", "gfs")
    finally:
        if obstruction == "unreadable_parent":
            # Restore before pytest's tmp_path cleanup walks the tree.
            earlier_input.chmod(0o700)

    assert row["first_run_evidence_complete"] is False
    assert row["verdict"] == "undetermined"


def test_audit_run_directory_without_a_manifest_keeps_the_sweep_complete(tmp_path: Path) -> None:
    """The absence side of the same three-way: a manifest-less run dir is decidable.

    A run directory that never got a manifest is confirmed absence of evidence
    for that cycle, not an undecidable read — skipping to the next cycle must
    NOT degrade the row, otherwise the completeness flag would fire on ordinary
    partially-materialized runs and every verdict would become useless.
    """
    fixture = _Fixture(tmp_path)
    fixture.add_model("empty_run_dir", _package_manifest("empty_run_dir"))
    (fixture.object_store_root / "runs" / "fcst_gfs_2026070500_empty_run_dir" / "input").mkdir(
        parents=True, exist_ok=True
    )
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070512_empty_run_dir",
            model_id="empty_run_dir",
            source_id="GFS",
            cycle_time="2026-07-05T12:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "empty_run_dir", "gfs")
    assert row["first_run_evidence_complete"] is True
    assert row["first_cycle"] == "2026070512"
    assert row["verdict"] == "cold_start_with_qualified_ic"


def test_audit_qualifies_a_direct_grid_variant_row_through_the_object_probe(tmp_path: Path) -> None:
    """The production 36/36 shape: no inventory, qualification from the IC object.

    Without tier (b) this row reads as ``cold_start_no_ic`` — a false negative on
    every production model, which is exactly what task 5.1 will hit on node-22.
    """
    fixture = _Fixture(tmp_path)
    ic_content = b"2\t1\t29626560.000000\n1\t0.1\t0.2\t0.3\t0.4\n"
    fixture.add_direct_grid_variant_model(
        "dth_ls_dg_gfs", shud_input_name="dth_ls", ic_content=ic_content
    )
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_dth_ls_dg_gfs",
            model_id="dth_ls_dg_gfs",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "dth_ls_dg_gfs", "gfs")
    assert row["verdict"] == "cold_start_with_qualified_ic"
    assert row["ic_qualified"] is True
    assert row["ic_qualification_source"] == "object_probe"
    assert row["ic_sha256"] == _sha256(ic_content)
    assert row["ic_relative_path"] == "dth_ls.cfg.ic"


@pytest.mark.parametrize("ic_content", [None, b""], ids=["object_missing", "object_empty"])
def test_audit_variant_row_without_a_usable_ic_object_is_not_qualified(
    tmp_path: Path, ic_content: bytes | None
) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_direct_grid_variant_model(
        "empty_dg", shud_input_name="empty_basin", ic_content=ic_content
    )
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_empty_dg",
            model_id="empty_dg",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "empty_dg", "gfs")
    assert row["verdict"] == "cold_start_no_ic"
    assert row["ic_qualified"] is False
    assert row["ic_qualification_source"] == "object_probe"
    assert row["ic_sha256"] is None


@pytest.mark.parametrize("obstruction", ["directory", "symlink"], ids=["directory_at_key", "symlink_at_key"])
def test_audit_undecidable_canonical_ic_stat_is_undetermined_not_no_ic(
    tmp_path: Path, obstruction: str
) -> None:
    """Tier-(b) symmetry with the manifest lane: a stat that cannot decide is undetermined.

    A canonical IC key that is a DIRECTORY (stat succeeds, not a regular file) or
    a SYMLINK (``stat_no_follow`` refuses by policy) is not evidence that the
    package ships no IC — the probe simply could not complete.  Collapsing either
    into ``exists=False`` makes the classifier emit
    ``packaged_initial_condition_object_missing`` and reports a CLEAN
    ``cold_start_no_ic`` for a basin whose IC was merely unreadable, which is the
    same fail-open shape #1164 exists to prevent.  Same treatment as the
    unreadable-manifest lane above.
    """
    fixture = _Fixture(tmp_path)
    fixture.add_direct_grid_variant_model(
        "obstructed_dg", shud_input_name="obstructed_basin", ic_content=None
    )
    ic_path = fixture.variant_ic_path("obstructed_dg", "obstructed_basin")
    ic_path.parent.mkdir(parents=True, exist_ok=True)
    if obstruction == "directory":
        ic_path.mkdir()
    else:
        ic_path.symlink_to(fixture.object_store_root / "elsewhere.cfg.ic")
    fixture.add_object_store_run(
        _run_manifest(
            run_id="fcst_gfs_2026070500_obstructed_dg",
            model_id="obstructed_dg",
            source_id="GFS",
            cycle_time="2026-07-05T00:00:00Z",
            quality="cold_start_no_state",
            init_mode=1,
        )
    )
    fixture.publish_registry()

    # Exit 0 also proves the receipt still validates against its schema: a
    # receipt that does not is raised as ``RECEIPT_INVALID`` and exits 1.
    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "obstructed_dg", "gfs")
    assert row["verdict"] == "undetermined"
    assert row["ic_qualified"] is None
    assert row["ic_status"] == "unreadable"
    assert row["ic_qualification_source"] == "object_probe"
    assert row["ic_sha256"] is None
    assert row["detail"]


def test_audit_receipt_declares_per_row_qualification_source_limits(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_model("dth_ls", _package_manifest("dth_ls"))
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    receipt = fixture.receipt()
    limits = receipt["limits"]
    assert limits["inventory_tier_package_objects_rehashed"] is False
    assert limits["probe_tier_max_object_bytes"] == audit.MAX_PACKAGED_IC_PROBE_BYTES
    note = str(limits["note"]).lower()
    assert "inventory" in note and "object_probe" in note
    assert _row(receipt, "dth_ls", "gfs")["ic_qualification_source"] == "inventory"


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
            first_run_evidence_complete=True,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("quality", "init_mode"),
    [("packaged_calibrated_state", 3), ("cold_start_no_state", 1), (None, None)],
)
def test_audit_verdict_table_refuses_an_incomplete_evidence_sweep(
    quality: str | None, init_mode: int | None
) -> None:
    """An incomplete sweep outranks every other row of the table."""
    assert (
        audit.classify_verdict(
            ic_qualified=True,
            first_run_quality=quality,
            first_run_init_mode=init_mode,
            first_run_evidence_complete=False,
        )
        == "undetermined"
    )


def test_audit_verdict_table_requires_an_explicit_completeness_claim() -> None:
    """No default: a caller must STATE whether its sweep was complete.

    A ``first_run_evidence_complete: bool = True`` default lets a future caller
    silently claim a complete sweep it never performed, which is the same
    collapse the flag exists to prevent.
    """
    with pytest.raises(TypeError):
        audit.classify_verdict(  # type: ignore[call-arg]
            ic_qualified=True,
            first_run_quality="cold_start_no_state",
            first_run_init_mode=1,
        )


# ---------------------------------------------------------------------------
# IC header content-shape (#1197): the malformed `23106\t6` delivery digested
# cleanly and was non-empty, so every pre-change qualification check passed it.
# ---------------------------------------------------------------------------

MALFORMED_IC = b"23106\t6\n1\t0.1\t0.2\t0.3\t0.4\n"
WELL_FORMED_IC = b"23106\t6\t29714400.000000\n1\t0.1\t0.2\t0.3\t0.4\n"
#: The same malformed header carried by a body many times the header bound
#: (~72 KiB here — 26 + 4096*18 bytes — against hundreds of KiB in production).
#: The tier-(a) sweep must reach its verdict from the first line alone, without
#: pulling the body across NFS.
LARGE_MALFORMED_IC = MALFORMED_IC + b"1\t0.1\t0.2\t0.3\t0.4\n" * 4096
#: A first line that never ends: 8192 bytes of numeric tokens with no newline at
#: all, so a bounded read can only ever hold a TRUNCATION of the header.
HEADERLESS_OVERLONG_IC = b"1 " * 4096


def test_audit_mirror_probe_fills_the_header_shape_verdict(tmp_path: Path) -> None:
    """The audit probe implementation itself, over real bytes on disk."""
    root = tmp_path / "store"
    (root / "models/x/package").mkdir(parents=True)
    (root / "models/x/package/x.cfg.ic").write_bytes(MALFORMED_IC)

    malformed = audit._canonical_ic_object_probe(
        "s3://nhms/models/x/package/x.cfg.ic",
        object_store_root=root,
        object_store_prefix="s3://nhms",
    )
    assert malformed.unreadable_detail == ""
    assert malformed.exists is True
    assert "2 numeric token(s)" in malformed.header_shape_invalid_reason

    (root / "models/x/package/x.cfg.ic").write_bytes(WELL_FORMED_IC)
    well_formed = audit._canonical_ic_object_probe(
        "s3://nhms/models/x/package/x.cfg.ic",
        object_store_root=root,
        object_store_prefix="s3://nhms",
    )
    assert well_formed.header_shape_invalid_reason == ""
    assert well_formed.sha256 == _sha256(WELL_FORMED_IC)

    # An object that cannot be read reports unreadability and NO shape verdict.
    absent = audit._canonical_ic_object_probe(
        "s3://nhms/models/x/package/missing.cfg.ic",
        object_store_root=root,
        object_store_prefix="s3://nhms",
    )
    assert absent.header_shape_invalid_reason == ""


def test_audit_variant_row_with_a_malformed_ic_header_is_disqualified(tmp_path: Path) -> None:
    """Tier (b) through the real audit entry point."""
    fixture = _Fixture(tmp_path)
    fixture.add_direct_grid_variant_model(
        "lh_gl_dg_gfs", shud_input_name="LH-GL", ic_content=MALFORMED_IC
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "lh_gl_dg_gfs", "gfs")
    assert row["ic_qualified"] is False
    assert row["ic_status"] == "unqualified"
    assert row["detail"] == "packaged_initial_condition_header_shape_invalid"
    assert row["ic_qualification_source"] == "object_probe"


def test_audit_sweeps_inventory_shaped_packages_with_its_own_content_probe(tmp_path: Path) -> None:
    """Tier (a) left-shift: the 51 already-published baseline packages get checked.

    The shared classification never probes for an inventory-shaped manifest (the
    production gate's zero-object-IO promise); this offline audit does it in its
    own layer, and says so through a dedicated qualification-source value.
    """
    fixture = _Fixture(tmp_path)
    fixture.add_baseline_package_model(
        "lh_gl", shud_input_name="LH-GL", ic_content=MALFORMED_IC
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "lh_gl", "gfs")
    assert row["ic_qualified"] is False
    assert row["ic_status"] == "unqualified"
    assert row["detail"] == "packaged_initial_condition_header_shape_invalid"
    assert row["ic_qualification_source"] == "inventory_content_probe"


def test_audit_inventory_sweep_leaves_a_well_formed_package_on_the_inventory_verdict(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path)
    fixture.add_baseline_package_model(
        "keliya", shud_input_name="keliya", ic_content=WELL_FORMED_IC
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "keliya", "gfs")
    assert row["ic_qualified"] is True
    assert row["ic_qualification_source"] == "inventory"


def test_audit_inventory_sweep_keeps_the_inventory_verdict_when_the_object_is_unreachable(
    tmp_path: Path,
) -> None:
    """Only a READ header may downgrade a row; a failed read manufactures nothing.

    The audit is a read-only sweep over an object store it does not own, so an
    object it cannot reach is not evidence that the package is malformed.
    """
    fixture = _Fixture(tmp_path)
    ic_path = fixture.add_baseline_package_model(
        "unreachable", shud_input_name="unreachable", ic_content=None
    )
    assert not ic_path.exists()
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "unreachable", "gfs")
    assert row["ic_qualified"] is True
    assert row["ic_qualification_source"] == "inventory"


def test_audit_inventory_sweep_keeps_the_inventory_verdict_when_the_header_outruns_the_bound(
    tmp_path: Path,
) -> None:
    """The other half of the bound: a truncated header may not manufacture a verdict.

    ``read_bytes_limited_no_follow`` returns one sentinel byte past its bound, so
    an IC object whose first line is longer than
    :data:`audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES` hands the probe a PREFIX of the
    header, not the header.  Counting tokens in that prefix would report a token
    count the file does not have (here: "2049 numeric token(s)" for a 4096-token
    line) and write that invented reason into a shipped receipt, so the probe
    returns ``None`` and the inventory verdict stands untouched.
    """
    fixture = _Fixture(tmp_path)
    ic_path = fixture.add_baseline_package_model(
        "overlong_header", shud_input_name="overlong", ic_content=HEADERLESS_OVERLONG_IC
    )
    assert b"\n" not in ic_path.read_bytes()
    assert ic_path.stat().st_size > audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    row = _row(fixture.receipt(), "overlong_header", "gfs")
    assert row["ic_qualified"] is True
    assert row["ic_qualification_source"] == "inventory"
    assert "numeric token(s)" not in str(row.get("detail") or "")


def test_audit_receipt_limits_declare_the_inventory_tier_header_probe(tmp_path: Path) -> None:
    """closure F-B: the receipt contract stays truthful about what was probed."""
    fixture = _Fixture(tmp_path)
    fixture.add_baseline_package_model(
        "keliya", shud_input_name="keliya", ic_content=WELL_FORMED_IC
    )
    fixture.publish_registry()

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    limits = fixture.receipt()["limits"]
    assert limits["inventory_tier_ic_header_probed"] is True
    # The sweep reads a bounded header prefix and hashes nothing (proved by
    # ``test_audit_inventory_sweep_reads_only_the_header_and_hashes_nothing``),
    # so the older const still holds.
    assert limits["inventory_tier_package_objects_rehashed"] is False
    note = str(limits["note"]).lower()
    assert "header" in note and "inventory_content_probe" in note


class _HashlibSpy:
    """Delegating stand-in for the audit module's ``hashlib`` that counts sha256."""

    def __init__(self) -> None:
        self.sha256_calls = 0

    def sha256(self, *args: Any, **kwargs: Any) -> Any:
        self.sha256_calls += 1
        return hashlib.sha256(*args, **kwargs)


def test_audit_inventory_sweep_reads_only_the_header_and_hashes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tier-(a) sweep's cost, asserted in BYTES READ rather than in prose.

    Production carries 51 inventory-shaped packages, ~16 MB of IC object bytes per
    full-probe pass (the figure :func:`audit._canonical_ic_header_shape_probe`'s own
    docstring quotes); a sweep that re-read (let alone re-hashed) them whole would
    burn that IO on every pass and would make the receipt's
    ``inventory_tier_package_objects_rehashed=false`` claim, the schema's
    ``inventory_tier_ic_header_probed`` description and this module's docstring
    untrue at once.  So this test instruments the audit's own reader and digest
    entry points: the canonical IC object is opened exactly once, bounded by
    :data:`audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES`, and no sha256 is computed at all.
    """
    fixture = _Fixture(tmp_path)
    ic_path = fixture.add_baseline_package_model(
        "lh_gl", shud_input_name="LH-GL", ic_content=LARGE_MALFORMED_IC
    )
    fixture.publish_registry()
    ic_size_bytes = ic_path.stat().st_size
    assert ic_size_bytes > 8 * audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES

    reads: list[tuple[Path, int, int]] = []
    real_read = audit.read_bytes_limited_no_follow

    def _recording_read(path: Path, *, max_bytes: int, containment_root: Path | None = None) -> bytes:
        content = real_read(path, max_bytes=max_bytes, containment_root=containment_root)
        reads.append((Path(path), max_bytes, len(content)))
        return content

    hashlib_spy = _HashlibSpy()
    monkeypatch.setattr(audit, "read_bytes_limited_no_follow", _recording_read)
    monkeypatch.setattr(audit, "hashlib", hashlib_spy)

    assert audit.main(fixture.argv("--sources", "gfs")) == 0

    # The bound did not cost the sweep its verdict: this package IS malformed.
    row = _row(fixture.receipt(), "lh_gl", "gfs")
    assert row["ic_qualified"] is False
    assert row["ic_qualification_source"] == "inventory_content_probe"

    ic_reads = [entry for entry in reads if entry[0] == ic_path]
    assert len(ic_reads) == 1, f"the canonical IC object was read {len(ic_reads)} times: {ic_reads}"
    _path, max_bytes, bytes_read = ic_reads[0]
    assert max_bytes == audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES
    # ``read_bytes_limited_no_follow`` returns at most one sentinel byte past its
    # bound, so that — not the bound itself — is the ceiling on bytes in hand.
    assert bytes_read <= audit.MAX_IC_HEADER_SHAPE_PROBE_BYTES + 1
    assert bytes_read < ic_size_bytes
    assert hashlib_spy.sha256_calls == 0
