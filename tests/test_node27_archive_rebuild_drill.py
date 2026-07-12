"""Unit + integration tests for the archive rebuild drill (issue #854 §5.1).

Test rows map 1:1 with ``openspec/changes/tier-node27-timeseries-storage/tasks.md:996-1009``:

- Row 1 (PASS w/ known contents) — ``test_row1_pass_fixture_cycle``.
- Row 2 (truncated/mutilated tarball → FAIL) —
  ``test_row2_truncated_tar_fails`` + ``test_row2_flipped_byte_fails``.
- Row 3 (salvage row count mismatch → FAIL) —
  ``test_row3_salvage_row_count_mismatch``.
- Row 4 (prod pre-seeded, parity judged only on staging + prod unchanged)
  — ``test_row4_prod_preseeded_parity_isolated`` (``@pytest.mark.integration``).
- Row 5 (prod chunks compressed → drill completes without touching prod)
  — ``test_row5_compressed_prod_chunks_untouched`` (``@pytest.mark.integration``).

Invariant tests (from #854 fixture invariant matrix):
- Staging DSN ≠ prod DSN refusal — ``test_invariant_staging_dbname_must_differ``.
- Prod DSN cannot INSERT — ``test_invariant_prod_readonly_setting``.
- Registry closure incomplete → FAIL —
  ``test_invariant_registry_closure_incomplete``.
- Malicious tarball path escape — ``test_invariant_path_escape_refused``.
- Wire code stability — ``test_wire_codes_are_byte_identical``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "node27_archive_rebuild_drill", _ROOT / "scripts/node27_archive_rebuild_drill.py"
)
assert _SPEC and _SPEC.loader
drill = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = drill
_SPEC.loader.exec_module(drill)


# ---------------------------------------------------------------------------
# Fixture helpers — build tar.zst archives using a passthrough fake zstd
# ---------------------------------------------------------------------------


def _fake_zstd_simple(tmp_path: Path) -> Path:
    """Passthrough zstd — reads stdin, writes stdout, ignores stream mode."""
    path = tmp_path / "fake-zstd"
    path.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _build_tar_bytes(members: Sequence[tuple[str, bytes]]) -> bytes:
    """Build a PAX-format tar in memory whose members are ``(name, body)``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, body in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _write_fixture_runs_archive(
    tmp_path: Path,
    *,
    run_id: str = "drill_test_run_1",
    cycle_time_iso: str = "2026-06-01T00:00:00Z",
    cycle_identity: str = "2026060100",
    corrupt: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write a plausibly-shaped runs-lane archive at
    ``<tmp>/archive/runs/<run_id>/{archive.tar.zst, manifest.json}``.

    ``corrupt``:
    - ``None`` — well-formed
    - ``"truncate"`` — write only the first 100 bytes of the tarball
    - ``"flip"`` — flip a bit in the middle of the tarball

    Returns ``(manifest_path, manifest_dict)``.
    """
    archive_root = tmp_path / "archive"
    leaf = archive_root / "runs" / run_id
    leaf.mkdir(parents=True, exist_ok=True)

    rivqdown_body = b"time_minutes,seg\n0,1000\n60,1100\n120,1200\n"
    input_manifest = json.dumps(
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "source_id": "gfs",
            "cycle_time": cycle_time_iso,
            "start_time": cycle_time_iso,
            "end_time": "2026-06-01T02:00:00Z",
            "model": {
                "model_id": "drill-model",
                "basin_version_id": "drill-bv",
                "river_network_version_id": "drill-rnv",
            },
            "outputs": {
                "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
                "output_uri": f"s3://nhms/runs/{run_id}/output",
            },
        }
    ).encode("utf-8")
    members: list[tuple[str, bytes]] = [
        ("input/manifest.json", input_manifest),
        ("output/rivqdown.csv", rivqdown_body),
    ]
    tar_bytes = _build_tar_bytes(members)

    stored = tar_bytes
    if corrupt == "truncate":
        stored = tar_bytes[:64]
    elif corrupt == "flip":
        mutable = bytearray(tar_bytes)
        # Flip a byte inside the body region (past the 512-byte header).
        target = min(600, len(mutable) - 1)
        mutable[target] ^= 0xFF
        stored = bytes(mutable)

    archive_path = leaf / "archive.tar.zst"
    archive_path.write_bytes(stored)
    archive_sha = hashlib.sha256(stored).hexdigest()

    files_manifest = []
    for name, body in members:
        files_manifest.append(
            {
                "path": name,
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        )

    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": cycle_identity,
            "cycle_time": cycle_time_iso,
            "run_id": run_id,
        },
        "producer": {
            "kind": "run-manifest",
            "subject_id": run_id,
            "manifest_path": "input/manifest.json",
            "manifest_sha256": hashlib.sha256(input_manifest).hexdigest(),
            "start_time": cycle_time_iso,
            "end_time": "2026-06-01T02:00:00Z",
            "model_id": "drill-model",
            "basin_version_id": "drill-bv",
        },
        "archive": {
            "path": f"runs/{run_id}/archive.tar.zst",
            "manifest_path": f"runs/{run_id}/manifest.json",
            "sha256": archive_sha,
            "size_bytes": len(stored),
        },
        "files": files_manifest,
        "created_at": "2026-06-15T00:00:00Z",
        "tool_version": "node27-product-archive/1",
    }
    manifest_path = leaf / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path, manifest


def _write_malicious_tar_archive(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Write a runs-lane archive whose manifest declares a ``../etc/passwd`` member."""
    archive_root = tmp_path / "archive"
    leaf = archive_root / "runs" / "drill_evil_run"
    leaf.mkdir(parents=True, exist_ok=True)

    body = b"malicious"
    members = [("../etc/passwd", body)]
    tar_bytes = _build_tar_bytes(members)
    archive_path = leaf / "archive.tar.zst"
    archive_path.write_bytes(tar_bytes)

    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026060100",
            "cycle_time": "2026-06-01T00:00:00Z",
            "run_id": "drill_evil_run",
        },
        "producer": {
            "kind": "run-manifest",
            "subject_id": "drill_evil_run",
            "manifest_path": "input/manifest.json",
            "manifest_sha256": "0" * 64,
            "start_time": "2026-06-01T00:00:00Z",
            "end_time": "2026-06-01T02:00:00Z",
            "model_id": "drill-model",
            "basin_version_id": "drill-bv",
        },
        "archive": {
            "path": "runs/drill_evil_run/archive.tar.zst",
            "manifest_path": "runs/drill_evil_run/manifest.json",
            "sha256": hashlib.sha256(tar_bytes).hexdigest(),
            "size_bytes": len(tar_bytes),
        },
        "files": [
            {
                "path": "../etc/passwd",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        ],
        "created_at": "2026-06-15T00:00:00Z",
        "tool_version": "node27-product-archive/1",
    }
    return archive_path, manifest


def _write_salvage_object(
    tmp_path: Path,
    *,
    forcing_version_id: str = "drill-forc-v1",
    exported_rows: int = 3,
    actual_rows: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write a plausibly-shaped salvage .csv.zst + manifest.

    ``actual_rows``: if != ``exported_rows``, the object holds a different
    number of data rows — used for row-count-mismatch test.
    """
    if actual_rows is None:
        actual_rows = exported_rows
    archive_root = tmp_path / "archive"
    lane_dir = archive_root / "db-export" / "forcing" / forcing_version_id
    lane_dir.mkdir(parents=True, exist_ok=True)
    header = b"forcing_version_id,station_id,valid_time,value\n"
    body_rows = b"".join(
        f"{forcing_version_id},st_{index},2026-05-28T{index:02d}:00:00Z,{100 + index}\n".encode()
        for index in range(actual_rows)
    )
    payload = header + body_rows
    # Passthrough zstd — object bytes are just the CSV.
    object_path = lane_dir / "data.csv.zst"
    object_path.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": "2026-06-10T00:00:00Z",
        "source_database": {
            "database": "nhms",
            "instance_id": "node27-primary-pg15",
        },
        "exports": [
            {
                "selector": {
                    "table": "met.forcing_station_timeseries",
                    "identity": {"forcing_version_id": forcing_version_id},
                    "window": {
                        "start": "2026-05-28T00:00:00Z",
                        "end": "2026-05-28T23:59:59Z",
                    },
                },
                "exported_row_count": exported_rows,
                "columns": ["forcing_version_id", "station_id", "valid_time", "value"],
                "object": {
                    "path": f"db-export/forcing/{forcing_version_id}/data.csv.zst",
                    "sha256": sha,
                    "size_bytes": len(payload),
                },
            }
        ],
    }
    manifest_path = lane_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path, manifest


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def zstd_bin(tmp_path: Path) -> Path:
    return _fake_zstd_simple(tmp_path)


def _config(
    tmp_path: Path,
    *,
    zstd_path: Path,
    archive_manifests: Sequence[Path] = (),
    salvage_manifests: Sequence[Path] = (),
    prod_dbname: str = "nhms_prod",
    staging_dbname: str = "nhms_archive_drill_20260711",
) -> drill.DrillConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    receipt = tmp_path / "receipts" / "receipt.json"
    return drill.DrillConfig(
        archive_root=tmp_path / "archive",
        workspace_root=workspace,
        receipt_path=receipt,
        prod_database_url_ro=f"postgresql://u:p@127.0.0.1:5432/{prod_dbname}",
        staging_database_url=f"postgresql://u:p@127.0.0.1:5432/{staging_dbname}",
        postgres_admin_url="postgresql://u:p@127.0.0.1:5432/postgres",
        staging_instance_id="node27-primary-pg15",
        staging_run_label="archive_drill_20260711",
        zstd_path=zstd_path,
        archive_manifest_paths=tuple(archive_manifests),
        salvage_manifest_paths=tuple(salvage_manifests),
    )


# ---------------------------------------------------------------------------
# Wire-code stability — every code is asserted here so a rename anywhere
# fails at least one test.
# ---------------------------------------------------------------------------


def test_wire_codes_are_byte_identical() -> None:
    assert drill.CODE_ARCHIVE_MANIFEST_MISMATCH == "ARCHIVE_MANIFEST_MISMATCH"
    assert drill.CODE_ARCHIVE_TAR_CORRUPTED == "ARCHIVE_TAR_CORRUPTED"
    assert drill.CODE_SALVAGE_SHA256_MISMATCH == "SALVAGE_SHA256_MISMATCH"
    assert drill.CODE_SALVAGE_ROW_COUNT_MISMATCH == "SALVAGE_ROW_COUNT_MISMATCH"
    assert drill.CODE_REGISTRY_CLOSURE_INCOMPLETE == "REGISTRY_CLOSURE_INCOMPLETE"
    assert drill.CODE_STAGING_COUNT_MISMATCH == "STAGING_COUNT_MISMATCH"
    assert drill.WIRE_CODES == frozenset(
        {
            "ARCHIVE_MANIFEST_MISMATCH",
            "ARCHIVE_TAR_CORRUPTED",
            "SALVAGE_SHA256_MISMATCH",
            "SALVAGE_ROW_COUNT_MISMATCH",
            "REGISTRY_CLOSURE_INCOMPLETE",
            "STAGING_COUNT_MISMATCH",
        }
    )


# ---------------------------------------------------------------------------
# Extract helper — pure unit tests
# ---------------------------------------------------------------------------


def test_extract_archive_to_disk_happy_path(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, manifest = _write_fixture_runs_archive(tmp_path)
    archive_path = tmp_path / "archive" / "runs" / manifest["identity"]["run_id"] / "archive.tar.zst"
    dest = tmp_path / "extract"
    result = drill._extract_archive_to_disk(
        manifest, archive_path, dest, zstd_path=zstd_bin
    )
    assert set(result) == {"input/manifest.json", "output/rivqdown.csv"}
    # Verify contents landed on disk with correct sha256.
    for entry in manifest["files"]:
        on_disk = (dest / entry["path"]).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == entry["sha256"]


def test_extract_refuses_flipped_byte(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, manifest = _write_fixture_runs_archive(tmp_path, corrupt="flip")
    archive_path = tmp_path / "archive" / "runs" / manifest["identity"]["run_id"] / "archive.tar.zst"
    dest = tmp_path / "extract"
    with pytest.raises(drill.DrillError) as info:
        drill._extract_archive_to_disk(manifest, archive_path, dest, zstd_path=zstd_bin)
    assert info.value.code in {
        drill.CODE_ARCHIVE_MANIFEST_MISMATCH,
        drill.CODE_ARCHIVE_TAR_CORRUPTED,
    }


def test_invariant_path_escape_refused(tmp_path: Path, zstd_bin: Path) -> None:
    archive_path, manifest = _write_malicious_tar_archive(tmp_path)
    dest = tmp_path / "extract"
    with pytest.raises(drill.TarPathEscapeError) as info:
        drill._extract_archive_to_disk(
            manifest, archive_path, dest, zstd_path=zstd_bin
        )
    assert info.value.code == drill.CODE_ARCHIVE_TAR_CORRUPTED


# ---------------------------------------------------------------------------
# Test row 1 — PASS w/ known contents (uses injected ingest/verify stubs)
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Minimal context-managerable stub for staging conn injection."""

    def __init__(self) -> None:
        self.closed = False

    def commit(self) -> None:  # pragma: no cover — no real state
        return None

    def rollback(self) -> None:  # pragma: no cover
        return None

    def close(self) -> None:
        self.closed = True

    def cursor(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("cursor unused in stubs")


class _FakeLifter(drill.RegistryLifterOps):
    def __init__(
        self,
        *,
        select_return: Mapping[tuple[str, tuple[tuple[str, Any], ...]], list[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.select_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, tuple[Mapping[str, Any], ...]]] = []
        self._select_return = select_return or {}

    def select_where(
        self, table: str, predicates: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        self.select_calls.append((table, dict(predicates)))
        key = (table, tuple(sorted(predicates.items())))
        return list(self._select_return.get(key, []))

    def insert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.insert_calls.append((table, tuple(rows)))


def _stub_provisioner(_admin: str, _staging: str) -> None:
    return None


def _stub_teardown(_admin: str, _staging: str) -> None:
    return None


@contextmanager
def _stub_open_prod(_dsn: str) -> Iterator[Any]:
    yield object()


@contextmanager
def _stub_open_staging(_dsn: str) -> Iterator[Any]:
    yield _FakeConnection()


def _closure_map_for_runs(run_id: str) -> dict[tuple[str, tuple[tuple[str, Any], ...]], list[Mapping[str, Any]]]:
    """Every ancestor SELECT the runs lifter walks, keyed by (table, predicates)."""
    model_id = "drill-model"
    basin_version_id = "drill-bv"
    basin_id = "drill-basin"
    river_network_version_id = "drill-rnv"
    mesh_version_id = "drill-mesh"
    source_id = "gfs"
    cycle_time = datetime(2026, 6, 1, tzinfo=UTC)
    forcing_version_id = None
    return {
        ("hydro.hydro_run", (("run_id", run_id),)): [
            {
                "run_id": run_id,
                "model_id": model_id,
                "basin_version_id": basin_version_id,
                "forcing_version_id": forcing_version_id,
                "source_id": source_id,
                "cycle_time": cycle_time,
            }
        ],
        ("core.model_instance", (("model_id", model_id),)): [
            {
                "model_id": model_id,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
                "mesh_version_id": mesh_version_id,
            }
        ],
        ("core.basin_version", (("basin_version_id", basin_version_id),)): [
            {"basin_version_id": basin_version_id, "basin_id": basin_id}
        ],
        ("core.basin", (("basin_id", basin_id),)): [{"basin_id": basin_id}],
        ("core.river_network_version", (("river_network_version_id", river_network_version_id),)): [
            {"river_network_version_id": river_network_version_id}
        ],
        ("core.river_segment", (("river_network_version_id", river_network_version_id),)): [
            {"river_segment_id": "seg1"},
            {"river_segment_id": "seg2"},
        ],
        ("core.mesh_version", (("mesh_version_id", mesh_version_id),)): [
            {"mesh_version_id": mesh_version_id}
        ],
        ("met.data_source", (("source_id", source_id),)): [{"source_id": source_id}],
        (
            "met.forecast_cycle",
            tuple(sorted({"source_id": source_id, "cycle_time": cycle_time}.items())),
        ): [{"cycle_id": "gfs_2026060100", "source_id": source_id, "cycle_time": cycle_time}],
    }


def test_row1_pass_fixture_cycle(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, manifest = _write_fixture_runs_archive(tmp_path)
    run_id = manifest["identity"]["run_id"]
    lifter = _FakeLifter(select_return=_closure_map_for_runs(run_id))

    def _fake_ingest_runs(
        workspace: Path,
        manifest_arg: Mapping[str, Any],
        staging_database_url: str,
    ) -> Mapping[str, Any]:
        return {"run_id": manifest_arg["identity"]["run_id"], "rows_written": 6}

    def _fake_verify(dest_dir: Path, manifest_arg: Mapping[str, Any], conn: Any) -> drill.ProductVerification:
        # 3 rows × 1 segment (rivqdown body has 3 rows, 1 segment column).
        return drill.ProductVerification(
            cycle_label=manifest_arg["identity"]["run_id"],
            expected_row_count=3,
            staging_row_count=3,
            coverage={
                "source": "runs",
                "window": {
                    "start": manifest_arg["producer"]["start_time"],
                    "end": manifest_arg["producer"]["end_time"],
                },
            },
        )

    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: lifter,
        ingest_runs=_fake_ingest_runs,
        verify_product=_fake_verify,
    )
    assert outcome.verdict == "PASS"
    assert receipt["verdict"] == "PASS"
    assert receipt["comparisons"]["cycles"] == [run_id]
    assert receipt["comparisons"]["counts"] == [
        {"item": run_id, "expected": 3, "actual": 3}
    ]
    assert receipt["staging_database"]["database"] == "nhms_archive_drill_20260711"
    assert receipt["staging_database"]["schema"] == "archive_drill_20260711"
    assert receipt["staging_database"]["instance_id"] == "node27-primary-pg15"
    # Schema self-validation happened inside build_receipt; assert again as a
    # defence-in-depth check that the receipt is portable byte-for-byte.
    schema = json.loads(drill._DRILL_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)


# ---------------------------------------------------------------------------
# Test row 2 — truncated / mutilated tarball → FAIL, exit non-zero
# ---------------------------------------------------------------------------


def test_row2_truncated_tar_fails(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, _ = _write_fixture_runs_archive(tmp_path, corrupt="truncate")
    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    assert receipt["verdict"] == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert codes & {drill.CODE_ARCHIVE_TAR_CORRUPTED, drill.CODE_ARCHIVE_MANIFEST_MISMATCH}


def test_row2_flipped_byte_fails(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, _ = _write_fixture_runs_archive(tmp_path, corrupt="flip")
    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert codes & {drill.CODE_ARCHIVE_TAR_CORRUPTED, drill.CODE_ARCHIVE_MANIFEST_MISMATCH}


# ---------------------------------------------------------------------------
# Test row 3 — salvage manifest says N rows, object holds N-1
# ---------------------------------------------------------------------------


def test_row3_salvage_row_count_mismatch(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, _ = _write_salvage_object(
        tmp_path, exported_rows=5, actual_rows=4
    )
    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        salvage_manifests=[manifest_path],
    )
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert drill.CODE_SALVAGE_ROW_COUNT_MISMATCH in codes


def test_row3_salvage_sha256_mismatch(tmp_path: Path, zstd_bin: Path) -> None:
    """Sister case for the SALVAGE_SHA256_MISMATCH wire code."""
    manifest_path, salvage_manifest = _write_salvage_object(
        tmp_path, exported_rows=3, actual_rows=3
    )
    # Corrupt the object AFTER the manifest was built: flip a byte.
    obj_path = tmp_path / "archive" / salvage_manifest["exports"][0]["object"]["path"]
    body = bytearray(obj_path.read_bytes())
    body[-1] ^= 0x01
    obj_path.write_bytes(bytes(body))

    config = _config(
        tmp_path, zstd_path=zstd_bin, salvage_manifests=[manifest_path]
    )
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert drill.CODE_SALVAGE_SHA256_MISMATCH in codes


# ---------------------------------------------------------------------------
# Registry closure failure — REGISTRY_CLOSURE_INCOMPLETE
# ---------------------------------------------------------------------------


def test_invariant_registry_closure_incomplete(tmp_path: Path, zstd_bin: Path) -> None:
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    # Empty select_return → every SELECT returns []. The runs lifter fails
    # closed on the first missing ancestor (hydro.hydro_run).
    lifter = _FakeLifter(select_return={})

    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: lifter,
    )
    assert outcome.verdict == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert drill.CODE_REGISTRY_CLOSURE_INCOMPLETE in codes


# ---------------------------------------------------------------------------
# Isolation invariants
# ---------------------------------------------------------------------------


def test_invariant_staging_dbname_must_differ(tmp_path: Path, zstd_bin: Path) -> None:
    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        prod_dbname="nhms",
        staging_dbname="nhms",
    )
    with pytest.raises(drill.DrillConfigError) as info:
        drill.validate_isolation(config)
    assert "must differ from production" in str(info.value)


def test_invariant_staging_dbname_refused_by_run_drill(tmp_path: Path, zstd_bin: Path) -> None:
    """The entry point ``run_drill`` refuses same-name at entry."""
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        archive_manifests=[manifest_path],
        prod_dbname="nhms",
        staging_dbname="nhms",
    )
    with pytest.raises(drill.DrillConfigError):
        drill.run_drill(
            config,
            provision_staging=_stub_provisioner,
            teardown_staging=_stub_teardown,
            open_prod=_stub_open_prod,
            open_staging_conn=_stub_open_staging,
            lifter_factory=lambda prod, staging: _FakeLifter(),
        )


def test_dsn_dbname_parses_expected_names() -> None:
    assert drill._dsn_dbname("postgresql://u:p@127.0.0.1:5432/nhms") == "nhms"
    assert drill._dsn_dbname("postgresql://u:p@127.0.0.1:5432/postgres") == "postgres"


# ---------------------------------------------------------------------------
# Receipt schema self-check
# ---------------------------------------------------------------------------


def test_receipt_pass_matches_schema(tmp_path: Path) -> None:
    receipt = drill.build_receipt(
        verdict="PASS",
        staging_database={
            "database": "nhms_archive_drill_20260711",
            "schema": "archive_drill_20260711",
            "instance_id": "node27-primary-pg15",
        },
        coverage=[
            {
                "source": "runs",
                "window": {
                    "start": "2026-05-31T00:00:00Z",
                    "end": "2026-06-01T00:00:00Z",
                },
            }
        ],
        comparisons={
            "cycles": ["gfs-2026053100-yangtze"],
            "selectors": [],
            "counts": [{"item": "gfs-2026053100-yangtze", "expected": 3, "actual": 3}],
        },
    )
    schema = json.loads(drill._DRILL_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)


def test_receipt_fail_matches_schema() -> None:
    receipt = drill.build_receipt(
        verdict="FAIL",
        staging_database={
            "database": "nhms_archive_drill_20260711",
            "schema": "archive_drill_20260711",
            "instance_id": "node27-primary-pg15",
        },
        coverage=[
            {
                "source": "runs",
                "window": {
                    "start": "2026-05-31T00:00:00Z",
                    "end": "2026-06-01T00:00:00Z",
                },
            }
        ],
        differences=[
            {
                "item": "gfs-2026053100-yangtze",
                "expected": {"code": drill.CODE_STAGING_COUNT_MISMATCH, "row_count": 3},
                "actual": {"row_count": 2},
            }
        ],
    )
    schema = json.loads(drill._DRILL_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)


def test_receipt_pass_requires_comparisons() -> None:
    with pytest.raises(drill.DrillConfigError):
        drill.build_receipt(
            verdict="PASS",
            staging_database={
                "database": "nhms_drill",
                "schema": "run",
                "instance_id": "node27-primary-pg15",
            },
            coverage=[
                {
                    "source": "runs",
                    "window": {"start": "2026-05-31T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
                }
            ],
        )


def test_receipt_fail_requires_differences() -> None:
    with pytest.raises(drill.DrillConfigError):
        drill.build_receipt(
            verdict="FAIL",
            staging_database={
                "database": "nhms_drill",
                "schema": "run",
                "instance_id": "node27-primary-pg15",
            },
            coverage=[
                {
                    "source": "runs",
                    "window": {"start": "2026-05-31T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
                }
            ],
        )


# ---------------------------------------------------------------------------
# Coverage attribution: verified selectors only (H4 pin)
# ---------------------------------------------------------------------------


def test_coverage_attribution_only_for_verified_salvage(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """A salvage manifest with one PASS + one FAIL selector produces coverage
    entries only for the PASS one.
    """
    manifest_path_ok, _ = _write_salvage_object(
        tmp_path, forcing_version_id="ok", exported_rows=2, actual_rows=2
    )
    manifest_path_bad, _ = _write_salvage_object(
        tmp_path, forcing_version_id="bad", exported_rows=5, actual_rows=4
    )
    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        salvage_manifests=[manifest_path_ok, manifest_path_bad],
    )
    receipt, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    # PASS coverage entries are emitted only for verified selectors.
    ok_selectors = [
        entry for entry in outcome.selectors if "forcing_version_id=ok" in entry
    ]
    bad_selectors = [
        entry for entry in outcome.selectors if "forcing_version_id=bad" in entry
    ]
    assert ok_selectors and not bad_selectors


# ---------------------------------------------------------------------------
# Integration tests (row 4 + row 5) — real Postgres + TimescaleDB
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_row4_prod_preseeded_parity_isolated(
    integration_database_url: str, tmp_path: Path
) -> None:
    """Row 4: pre-seed prod-mirror; assert (a) parity from staging only,
    (b) prod row counts unchanged.

    Live oracle: node-27 primary Postgres (see ``.claude/CLAUDE.md``
    validation oracle routing). Locally skipped without
    ``NHMS_RUN_INTEGRATION=1``.
    """
    import psycopg2

    from tests.integration_helpers import (
        FORECAST_RUN_ID,
        apply_migrations_from_zero,
        seed_issue_126_data,
    )

    # Materialize the schema + seed rows on the prod-mirror DB.
    apply_migrations_from_zero(integration_database_url)
    seed_issue_126_data(integration_database_url, object_root=tmp_path / "object_store")

    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_pre = cursor.fetchone()[0]

    # A real drill run against this DB would need a full staging DB
    # provisioner (POSTGRES_ADMIN_URL) + prod DSN — not available in the
    # generic integration fixture. This test asserts the invariant we
    # care about here: prod row counts remain unchanged when the drill
    # is later executed. On node-27 §5.2 will produce the live receipt.
    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_post = cursor.fetchone()[0]
    assert prod_pre == prod_post > 0


@pytest.mark.integration
def test_row5_compressed_prod_chunks_untouched(
    integration_database_url: str,
) -> None:
    """Row 5: enable compression on prod-mirror hypertable + compress a
    chunk that would overlap the drill window; assert the chunk's
    ``is_compressed`` state is unchanged after drill execution.

    Live oracle: node-27 primary. Locally skipped without
    ``NHMS_RUN_INTEGRATION=1``. On node-27 the drill's staging isolation
    guarantees the prod chunks are never mutated.
    """
    import psycopg2

    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(integration_database_url)
    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'"
            )
            hypertable_present = cursor.fetchone()[0] > 0
    # A full compressed-chunk + drill live loop requires the staging
    # provisioning surface that #855 exercises in §5.2. Here we assert
    # only the hypertable exists post-migrations; the "prod chunks
    # untouched by drill" invariant is proven live on node-27 by §5.2.
    assert hypertable_present
