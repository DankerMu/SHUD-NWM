"""Unit + integration tests for the archive rebuild drill (issue #854 §5.1).

Test rows map 1:1 with ``openspec/changes/tier-node27-timeseries-storage/tasks.md:996-1009``:

- Row 1 (PASS w/ known contents) — ``test_row1_pass_fixture_cycle``.
- Row 2 (truncated/mutilated tarball → FAIL) —
  ``test_row2_truncated_tar_fails`` + ``test_row2_flipped_byte_fails``.
- Row 3 (salvage row count mismatch → FAIL) —
  ``test_row3_salvage_row_count_mismatch``.
- Row 4 (prod pre-seeded, parity judged only on staging + prod unchanged)
  — ``test_row4_prod_preseeded_parity_isolated_scaffold_only``
  (``@pytest.mark.integration``, scaffold; real end-to-end drill oracle is
  ``test_a1_run_drill_end_to_end_against_real_postgres``).
- Row 5 (prod chunks compressed → drill completes without touching prod)
  — ``test_row5_compressed_prod_chunks_untouched_scaffold_only``
  (``@pytest.mark.integration``, scaffold; real end-to-end drill oracle is
  ``test_a1_run_drill_end_to_end_against_real_postgres``).

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
    admin_dbname: str = "postgres",
    lock_path: Path | None = None,
) -> drill.DrillConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    receipt = tmp_path / "receipts" / "receipt.json"
    # Tests use a tmp-fenced lock path to avoid writing to `~/` during
    # unit-test runs; production default is `~/node27-archive-rebuild-drill-logs/drill.lock`.
    lock = lock_path if lock_path is not None else tmp_path / "drill.lock"
    return drill.DrillConfig(
        archive_root=tmp_path / "archive",
        workspace_root=workspace,
        receipt_path=receipt,
        prod_database_url_ro=f"postgresql://u:p@127.0.0.1:5432/{prod_dbname}",
        staging_database_url=f"postgresql://u:p@127.0.0.1:5432/{staging_dbname}",
        postgres_admin_url=f"postgresql://u:p@127.0.0.1:5432/{admin_dbname}",
        staging_instance_id="node27-primary-pg15",
        staging_run_label="archive_drill_20260711",
        zstd_path=zstd_path,
        archive_manifest_paths=tuple(archive_manifests),
        salvage_manifest_paths=tuple(salvage_manifests),
        lock_path=lock,
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
    assert drill.CODE_DRILL_UNCAUGHT_ERROR == "DRILL_UNCAUGHT_ERROR"
    assert drill.CODE_DRILL_CONCURRENT_INVOCATION == "DRILL_CONCURRENT_INVOCATION"
    assert drill.WIRE_CODES == frozenset(
        {
            "ARCHIVE_MANIFEST_MISMATCH",
            "ARCHIVE_TAR_CORRUPTED",
            "SALVAGE_SHA256_MISMATCH",
            "SALVAGE_ROW_COUNT_MISMATCH",
            "REGISTRY_CLOSURE_INCOMPLETE",
            "STAGING_COUNT_MISMATCH",
            "DRILL_UNCAUGHT_ERROR",
            "DRILL_CONCURRENT_INVOCATION",
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
# Round 1 fix regressions — one test per BLOCKING finding
# ---------------------------------------------------------------------------


def test_dsn_dbname_url_decodes(tmp_path: Path) -> None:
    """C-di-7: percent-encoded dbname decodes to canonical form."""
    assert drill._dsn_dbname("postgresql://u:p@127.0.0.1:5432/nhms%5Fdrill") == "nhms_drill"


def test_validate_isolation_refuses_admin_dbname_equal_to_prod(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """C-di-4: admin URL must not point at the prod dbname."""
    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        prod_dbname="nhms",
        staging_dbname="nhms_drill",
        admin_dbname="nhms",
    )
    with pytest.raises(drill.DrillConfigError) as info:
        drill.validate_isolation(config)
    assert "admin DSN dbname must not equal production dbname" in str(info.value)


def test_c_is_1_lift_and_ingest_commits_staging_before_runs_adapter(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """C-is-1: ``staging_conn.commit()`` runs BEFORE the ingest adapter opens
    its own connection — the lifted registry rows must be committed so
    ``OutputParser``'s second connection sees them (FK checks otherwise
    fail).
    """
    commit_calls: list[str] = []
    ingest_order: list[str] = []

    class _RecordingStagingConn:
        closed = False

        def commit(self) -> None:
            commit_calls.append("commit")
            ingest_order.append("commit")

        def rollback(self) -> None:  # pragma: no cover
            return None

        def close(self) -> None:
            self.closed = True

        def cursor(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    conn = _RecordingStagingConn()

    @contextmanager
    def _open_conn(_dsn: str) -> Iterator[Any]:
        yield conn

    manifest_path, manifest = _write_fixture_runs_archive(tmp_path)
    run_id = manifest["identity"]["run_id"]

    def _fake_ingest_runs(
        workspace: Path,
        manifest_arg: Mapping[str, Any],
        staging_database_url: str,
    ) -> Mapping[str, Any]:
        ingest_order.append("ingest")
        return {"run_id": manifest_arg["identity"]["run_id"], "rows_written": 3}

    def _fake_verify(dest_dir: Path, manifest_arg: Mapping[str, Any], _conn: Any) -> drill.ProductVerification:
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
    drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_open_conn,
        lifter_factory=lambda prod, staging: _FakeLifter(select_return=_closure_map_for_runs(run_id)),
        ingest_runs=_fake_ingest_runs,
        verify_product=_fake_verify,
    )
    # commit must happen at least once BEFORE the ingest adapter fires.
    assert ingest_order[:2] == ["commit", "ingest"], ingest_order
    assert len(commit_calls) >= 1


def test_c_mf_1_forcing_expected_row_count_accepts_bare_list(tmp_path: Path) -> None:
    """C-mf-1: bare-list payload matches canonical parser semantics."""
    dest = tmp_path / "extract"
    payloads = dest / "payloads"
    payloads.mkdir(parents=True)
    (payloads / "station_timeseries.json").write_text(
        json.dumps(
            [
                {"forcing_version_id": "fv", "station_id": "s1", "valid_time": "2026-05-28T00:00:00Z", "value": 1.0},
                {"forcing_version_id": "fv", "station_id": "s2", "valid_time": "2026-05-28T00:00:00Z", "value": 2.0},
            ]
        )
    )
    assert drill._forcing_expected_row_count(dest) == 2


def test_c_mf_1_forcing_expected_row_count_accepts_rows_wrapper(tmp_path: Path) -> None:
    """C-mf-1: ``{"rows": [...]}`` payload matches canonical parser semantics."""
    dest = tmp_path / "extract"
    payloads = dest / "payloads"
    payloads.mkdir(parents=True)
    (payloads / "station_timeseries.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "forcing_version_id": "fv",
                        "station_id": "s1",
                        "valid_time": "2026-05-28T00:00:00Z",
                        "value": 1.0,
                    },
                ]
            }
        )
    )
    assert drill._forcing_expected_row_count(dest) == 1


def test_c_mf_1_forcing_expected_row_count_rejects_missing_rows(tmp_path: Path) -> None:
    """Unknown-shape payloads still raise ArchiveManifestMismatchError."""
    dest = tmp_path / "extract"
    payloads = dest / "payloads"
    payloads.mkdir(parents=True)
    (payloads / "station_timeseries.json").write_text(json.dumps({"records": [1, 2]}))
    with pytest.raises(drill.ArchiveManifestMismatchError):
        drill._forcing_expected_row_count(dest)


def test_c_rs_1_forcing_closure_lifts_river_network_before_model_instance() -> None:
    """C-rs-1: forcing closure must SELECT + INSERT river_network_version +
    river_segment before core.model_instance so the model_instance FK holds.
    """
    forcing_version_id = "drill-forc-v1"
    model_id = "drill-model"
    basin_version_id = "drill-bv"
    river_network_version_id = "drill-rnv"
    mesh_version_id = "drill-mesh"
    basin_id = "drill-basin"

    select_map = {
        ("met.forcing_version", (("forcing_version_id", forcing_version_id),)): [
            {
                "forcing_version_id": forcing_version_id,
                "model_id": model_id,
                "source_id": "gfs",
                "cycle_time": datetime(2026, 6, 1, tzinfo=UTC),
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
            {"river_segment_id": "seg1", "river_network_version_id": river_network_version_id},
        ],
        ("core.mesh_version", (("mesh_version_id", mesh_version_id),)): [
            {"mesh_version_id": mesh_version_id}
        ],
        ("met.data_source", (("source_id", "gfs"),)): [{"source_id": "gfs"}],
        (
            "met.forecast_cycle",
            tuple(sorted({"source_id": "gfs", "cycle_time": datetime(2026, 6, 1, tzinfo=UTC)}.items())),
        ): [{"cycle_id": "gfs_2026060100", "source_id": "gfs", "cycle_time": datetime(2026, 6, 1, tzinfo=UTC)}],
    }
    lifter = _FakeLifter(select_return=select_map)
    drill._lift_registry_closure_forcing(lifter, forcing_version_id)

    inserted = [call[0] for call in lifter.insert_calls]
    # river_network_version + river_segment must precede core.model_instance.
    assert "core.river_network_version" in inserted
    assert "core.river_segment" in inserted
    assert inserted.index("core.river_network_version") < inserted.index("core.model_instance")
    assert inserted.index("core.river_segment") < inserted.index("core.model_instance")


def test_d1_forcing_closure_skips_forcing_version_insert() -> None:
    """D1 / C-rs-4: forcing_version is NEVER inserted by the lifter; the
    handoff helper writes it from the manifest package.
    """
    forcing_version_id = "drill-forc-v2"
    model_id = "drill-model-2"
    basin_version_id = "drill-bv-2"
    river_network_version_id = "drill-rnv-2"
    mesh_version_id = "drill-mesh-2"
    basin_id = "drill-basin-2"
    select_map = {
        ("met.forcing_version", (("forcing_version_id", forcing_version_id),)): [
            {
                "forcing_version_id": forcing_version_id,
                "model_id": model_id,
                "source_id": "gfs",
                "cycle_time": datetime(2026, 6, 1, tzinfo=UTC),
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
            {"river_segment_id": "seg1", "river_network_version_id": river_network_version_id},
        ],
        ("core.mesh_version", (("mesh_version_id", mesh_version_id),)): [
            {"mesh_version_id": mesh_version_id}
        ],
        ("met.data_source", (("source_id", "gfs"),)): [{"source_id": "gfs"}],
        (
            "met.forecast_cycle",
            tuple(sorted({"source_id": "gfs", "cycle_time": datetime(2026, 6, 1, tzinfo=UTC)}.items())),
        ): [{"cycle_id": "gfs_2026060100", "source_id": "gfs", "cycle_time": datetime(2026, 6, 1, tzinfo=UTC)}],
    }
    lifter = _FakeLifter(select_return=select_map)
    drill._lift_registry_closure_forcing(lifter, forcing_version_id)
    inserted_tables = [call[0] for call in lifter.insert_calls]
    assert "met.forcing_version" not in inserted_tables


class _RecordingCursor:
    """Cursor that records every execute call; used for JSONB wrap assertion."""

    def __init__(self) -> None:
        self.executed: list[tuple[Any, tuple[Any, ...]]] = []

    def execute(self, stmt: Any, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((stmt, tuple(params)))

    def fetchall(self) -> list[Any]:  # pragma: no cover
        return []

    def fetchone(self) -> Any | None:  # pragma: no cover
        return None

    def close(self) -> None:  # pragma: no cover
        return None

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _RecordingStagingConn:
    def __init__(self) -> None:
        self.cursors: list[_RecordingCursor] = []

    def cursor(self, *args: Any, **kwargs: Any) -> _RecordingCursor:
        c = _RecordingCursor()
        self.cursors.append(c)
        return c


def test_c_rs_2_insert_rows_wraps_jsonb_dict_with_json_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-rs-2 / A5: dict values bound for jsonb columns are wrapped with
    ``psycopg2.extras.Json`` before ``cursor.execute``.
    """
    from psycopg2.extras import Json

    conn = _RecordingStagingConn()
    lifter = drill.PsycopgRegistryLifterOps(prod_conn=object(), staging_conn=conn)
    # Bypass information_schema — inject the known staging column map.
    lifter._staging_columns[("core", "model_instance")] = {
        "model_id": "text",
        "basin_version_id": "text",
        "resource_profile": "jsonb",
    }
    row = {
        "model_id": "m1",
        "basin_version_id": "bv1",
        "resource_profile": {"partition": "test"},
    }
    lifter.insert_rows("core.model_instance", [row])
    assert conn.cursors, "no cursor opened"
    executed = conn.cursors[0].executed
    assert executed, "no execute call recorded"
    _stmt, params = executed[0]
    assert len(params) == 3
    # dict-typed staging value is wrapped with Json.
    assert isinstance(params[2], Json)
    # Text-typed values are NOT wrapped.
    assert params[0] == "m1"
    assert params[1] == "bv1"


def test_c_rs_2_insert_rows_leaves_none_jsonb_as_none() -> None:
    """A5 corollary: ``None`` bound to a jsonb column stays ``None``."""
    conn = _RecordingStagingConn()
    lifter = drill.PsycopgRegistryLifterOps(prod_conn=object(), staging_conn=conn)
    lifter._staging_columns[("met", "forcing_version")] = {
        "forcing_version_id": "text",
        "lineage_json": "jsonb",
    }
    lifter.insert_rows(
        "met.forcing_version",
        [{"forcing_version_id": "f1", "lineage_json": None}],
    )
    _stmt, params = conn.cursors[0].executed[0]
    assert params[1] is None


def test_d2_insert_rows_refuses_prod_column_missing_from_staging() -> None:
    """D2 / C-rs-5: prod row column absent from staging → REGISTRY_CLOSURE_INCOMPLETE."""
    conn = _RecordingStagingConn()
    lifter = drill.PsycopgRegistryLifterOps(prod_conn=object(), staging_conn=conn)
    lifter._staging_columns[("core", "model_instance")] = {
        "model_id": "text",
        "basin_version_id": "text",
        # drift_column MISSING intentionally.
    }
    row = {
        "model_id": "m1",
        "basin_version_id": "bv1",
        "drift_column": "prod-only-value",
    }
    with pytest.raises(drill.RegistryClosureIncompleteError) as info:
        lifter.insert_rows("core.model_instance", [row])
    assert "drift_column" in str(info.value)


def test_d2_insert_rows_only_uses_column_intersection() -> None:
    """D2 corollary: staging columns absent from the prod row are left to the
    staging default — we don't try to bind NULL for them.
    """
    conn = _RecordingStagingConn()
    lifter = drill.PsycopgRegistryLifterOps(prod_conn=object(), staging_conn=conn)
    lifter._staging_columns[("core", "basin")] = {
        "basin_id": "text",
        "basin_name": "text",
        "extra_staging_default_column": "text",
    }
    lifter.insert_rows("core.basin", [{"basin_id": "b1", "basin_name": "X"}])
    _stmt, params = conn.cursors[0].executed[0]
    assert len(params) == 2, params  # only the two prod-row columns bound


def test_c_is_2_workspace_cleaned_on_pass(tmp_path: Path, zstd_bin: Path) -> None:
    """C1 / C-is-2: workspace removed on PASS."""
    manifest_path, manifest = _write_fixture_runs_archive(tmp_path)
    run_id = manifest["identity"]["run_id"]
    lifter = _FakeLifter(select_return=_closure_map_for_runs(run_id))

    def _fake_ingest_runs(
        workspace: Path,
        manifest_arg: Mapping[str, Any],
        staging_database_url: str,
    ) -> Mapping[str, Any]:
        return {"run_id": manifest_arg["identity"]["run_id"], "rows_written": 3}

    def _fake_verify(dest_dir: Path, manifest_arg: Mapping[str, Any], conn: Any) -> drill.ProductVerification:
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
    _, outcome = drill.run_drill(
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
    assert not config.workspace_root.exists(), (
        f"workspace not cleaned: {config.workspace_root}"
    )


def test_c_is_2_workspace_cleaned_on_fail(tmp_path: Path, zstd_bin: Path) -> None:
    """C1 / C-is-2: workspace removed even when the drill FAILs."""
    manifest_path, _ = _write_fixture_runs_archive(tmp_path, corrupt="truncate")
    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    _, outcome = drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
    )
    assert outcome.verdict == "FAIL"
    assert not config.workspace_root.exists()


def test_c_is_2_keep_workspace_flag_preserves_tree(tmp_path: Path, zstd_bin: Path) -> None:
    """``keep_workspace=True`` retains the workspace for operator triage."""
    manifest_path, _ = _write_fixture_runs_archive(tmp_path, corrupt="truncate")
    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    drill.run_drill(
        config,
        provision_staging=_stub_provisioner,
        teardown_staging=_stub_teardown,
        open_prod=_stub_open_prod,
        open_staging_conn=_stub_open_staging,
        lifter_factory=lambda prod, staging: _FakeLifter(),
        keep_workspace=True,
    )
    assert config.workspace_root.exists()


def test_c_is_5_provision_failure_still_tears_down_staging(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """C3 / C-is-5 / C-di-3: provision inside try/finally — a raise
    from provision_staging leaves teardown_staging still called.
    """
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    teardown_calls: list[tuple[str, str]] = []

    def _bad_provision(_admin: str, _staging: str) -> None:
        raise RuntimeError("simulated CREATE DATABASE crash")

    def _teardown(admin: str, staging: str) -> None:
        teardown_calls.append((admin, staging))

    config = _config(tmp_path, zstd_path=zstd_bin, archive_manifests=[manifest_path])
    # run_drill raises the underlying RuntimeError because provision_staging
    # is inside the try block; but the finally still runs teardown.
    with pytest.raises(RuntimeError, match="simulated CREATE DATABASE crash"):
        drill.run_drill(
            config,
            provision_staging=_bad_provision,
            teardown_staging=_teardown,
            open_prod=_stub_open_prod,
            open_staging_conn=_stub_open_staging,
            lifter_factory=lambda prod, staging: _FakeLifter(),
        )
    assert teardown_calls, "teardown must run even when provision raised"


def test_b1_main_emits_fail_receipt_on_uncaught_error(
    tmp_path: Path, zstd_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1 / C-is-4: any uncaught downstream exception lands as a FAIL
    receipt with wire code DRILL_UNCAUGHT_ERROR — not a raw stack trace.
    """
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    receipt_path = tmp_path / "receipts" / "receipt.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NHMS_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE", str(workspace))
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH", str(receipt_path))
    monkeypatch.setenv("PROD_DATABASE_URL_RO", "postgresql://u:p@127.0.0.1:5432/nhms_prod")
    monkeypatch.setenv(
        "STAGING_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/nhms_drill"
    )
    monkeypatch.setenv("POSTGRES_ADMIN_URL", "postgresql://u:p@127.0.0.1:5432/postgres")
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID", "node27-primary-pg15")
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_RUN_LABEL", "archive_drill_test")
    monkeypatch.setenv("NHMS_ZSTD_BIN", str(zstd_bin))
    # Fence the single-instance lock inside tmp_path so the test does not
    # create files under `~/` (the runbook default).
    monkeypatch.setenv(
        "NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH", str(tmp_path / "drill.lock")
    )

    def _raising_run_drill(_config: Any) -> Any:
        raise RuntimeError("simulated psycopg2 failure")

    monkeypatch.setattr(drill, "run_drill", _raising_run_drill)
    exit_code = drill.main(["--archive-manifest", str(manifest_path)])
    assert exit_code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "FAIL"
    codes = {diff["expected"]["code"] for diff in receipt["differences"]}
    assert drill.CODE_DRILL_UNCAUGHT_ERROR in codes
    # NEW-3 (R2): FAIL receipts must carry the concrete exception class name
    # and message so operators can distinguish infra faults (OSError,
    # psycopg2.*) from logic faults (KeyError, ...) without a stack trace.
    diff = next(
        d for d in receipt["differences"]
        if d["expected"]["code"] == drill.CODE_DRILL_UNCAUGHT_ERROR
    )
    assert diff["actual"]["cause_type"] == "RuntimeError"
    assert diff["actual"]["error"] == "simulated psycopg2 failure"


def test_c_is_3_flock_refuses_concurrent_invocation(tmp_path: Path) -> None:
    """C2 / C-is-3: an already-held flock file blocks a second drill start."""
    lock_path = tmp_path / "drill.lock"
    with drill._single_instance_lock(lock_path):
        with pytest.raises(drill.DrillConcurrentInvocationError) as info:
            with drill._single_instance_lock(lock_path):
                pass  # pragma: no cover — never reached
    assert info.value.code == drill.CODE_DRILL_CONCURRENT_INVOCATION


def test_c_is_3_main_emits_fail_receipt_on_concurrent_invocation(
    tmp_path: Path, zstd_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-3 (R2): the ``main()``-level ``DRILL_CONCURRENT_INVOCATION``
    branch must publish a schema-valid FAIL receipt carrying
    ``cause_type == "DrillConcurrentInvocationError"``, symmetric with the
    uncaught-error path (B1 / C-is-4). Operators consume the receipt
    file, not stderr, so cause_type is the wire signal.
    """
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    receipt_path = tmp_path / "receipts" / "receipt.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = tmp_path / "drill.lock"

    monkeypatch.setenv("NHMS_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE", str(workspace))
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH", str(receipt_path))
    monkeypatch.setenv("PROD_DATABASE_URL_RO", "postgresql://u:p@127.0.0.1:5432/nhms_prod")
    monkeypatch.setenv(
        "STAGING_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/nhms_drill"
    )
    monkeypatch.setenv("POSTGRES_ADMIN_URL", "postgresql://u:p@127.0.0.1:5432/postgres")
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID", "node27-primary-pg15")
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_RUN_LABEL", "archive_drill_test")
    monkeypatch.setenv("NHMS_ZSTD_BIN", str(zstd_bin))
    monkeypatch.setenv("NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH", str(lock_path))

    # Hold the flock in the outer context so main() (same process, new fd)
    # is refused non-blockingly — the exact race the wire code exists to name.
    with drill._single_instance_lock(lock_path):
        exit_code = drill.main(["--archive-manifest", str(manifest_path)])
    assert exit_code == 2
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "FAIL"
    diff = next(
        d for d in receipt["differences"]
        if d["expected"]["code"] == drill.CODE_DRILL_CONCURRENT_INVOCATION
    )
    assert diff["actual"]["cause_type"] == "DrillConcurrentInvocationError"
    assert str(lock_path) in diff["actual"]["error"]


def test_b2_fail_receipt_with_empty_coverage_passes_schema() -> None:
    """B2 / C-sc-1: schema permits ``coverage: []`` on FAIL after the fix;
    the fictitious 1970-01-01 stub is no longer emitted.
    """
    receipt = drill.build_receipt(
        verdict="FAIL",
        staging_database={
            "database": "nhms_drill",
            "schema": "run",
            "instance_id": "node27-primary-pg15",
        },
        coverage=[],
        differences=[
            {
                "item": "drill",
                "expected": {"code": drill.CODE_DRILL_UNCAUGHT_ERROR},
                "actual": {"error": "boom"},
            }
        ],
    )
    assert receipt["coverage"] == []
    schema = json.loads(drill._DRILL_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(receipt, schema)


def test_b2_pass_receipt_still_requires_nonempty_coverage() -> None:
    """B2 assurance: PASS with empty coverage is still rejected by the schema."""
    receipt = drill.build_receipt(
        verdict="PASS",
        staging_database={
            "database": "nhms_drill",
            "schema": "run",
            "instance_id": "node27-primary-pg15",
        },
        coverage=[
            {"source": "runs", "window": {"start": "2026-05-31T00:00:00Z", "end": "2026-06-01T00:00:00Z"}},
        ],
        comparisons={
            "cycles": ["c1"],
            "selectors": [],
            "counts": [{"item": "c1", "expected": 1, "actual": 1}],
        },
    )
    # PASS with zero-coverage is refused by build_receipt via schema oneOf.
    receipt_empty = dict(receipt)
    receipt_empty["coverage"] = []
    schema = json.loads(drill._DRILL_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt_empty, schema)


# ---------------------------------------------------------------------------
# Integration tests (row 4 + row 5) — real Postgres + TimescaleDB
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_row4_prod_preseeded_parity_isolated_scaffold_only(
    integration_database_url: str, tmp_path: Path
) -> None:
    """Row 4 SCAFFOLD-ONLY: this test asserts the seed harness produces
    ``hydro.river_timeseries`` rows for the seeded run_id and confirms the
    prod row count is stable under a no-op observer. It does NOT invoke
    ``drill.run_drill``; the real end-to-end drill integration lives in
    ``test_a1_run_drill_end_to_end_against_real_postgres`` below.

    Row 4's live oracle (§5.2 node-27 PASS receipt covering pre-seeded
    prod chunks) supersedes the invariants shown here.

    Locally skipped without ``NHMS_RUN_INTEGRATION=1``.
    """
    import psycopg2

    from tests.integration_helpers import (
        FORECAST_RUN_ID,
        apply_migrations_from_zero,
        seed_issue_126_data,
    )

    apply_migrations_from_zero(integration_database_url)
    seed_issue_126_data(integration_database_url, object_root=tmp_path / "object_store")

    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_pre = cursor.fetchone()[0]
    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_post = cursor.fetchone()[0]
    assert prod_pre == prod_post > 0


@pytest.mark.integration
def test_row5_compressed_prod_chunks_untouched_scaffold_only(
    integration_database_url: str,
) -> None:
    """Row 5 SCAFFOLD-ONLY: asserts the migrations declare
    ``hydro.river_timeseries`` as a TimescaleDB hypertable so §5.2 can
    later compress a chunk and prove the drill leaves it untouched. Does
    NOT invoke ``drill.run_drill`` or force a compressed chunk. Live
    oracle: node-27 §5.2 PASS receipt.

    Locally skipped without ``NHMS_RUN_INTEGRATION=1``.
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
    assert hypertable_present


# ---------------------------------------------------------------------------
# A1: real Postgres end-to-end drill integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a1_run_drill_end_to_end_against_real_postgres(
    integration_database_url: str, tmp_path: Path
) -> None:
    """A1: exercise ``drill.run_drill`` end-to-end against real Postgres
    (the pattern-level fix that closes 4 BLOCKING at once — C-is-1,
    C-mf-1, C-rs-1, C-rs-2 — by proving the interactions the unit-level
    Fake* stubs cannot).

    Scope:

    - Prod DSN = ``integration_database_url`` (seeded via ``seed_issue_126_data``).
    - Staging DSN = a distinct dbname on the same cluster, provisioned by
      ``drill.provision_staging_database`` (which invokes
      ``apply_migrations_from_zero`` — same helper conftest uses).
    - Registry lifter = real ``PsycopgRegistryLifterOps`` (proves JSONB
      wrap + drift guard + column ordering against real staging schema).
    - Product ingest: stubbed via injected ``ingest_runs`` because
      bootstrapping a valid rivqdown+run manifest matching the seeded IDs
      is a substantial subsystem test not tackled here (the drill sequence
      itself — extract → lift → commit → verify — is the invariant we're
      proving; ingest correctness is proven by OutputParser's own tests).
    - Verify product: stubbed to return a matching count so PASS is
      structurally reachable.

    Skip conditions: ``NHMS_RUN_INTEGRATION=1`` required (conftest gate);
    additionally skipped if the admin URL cannot be derived from the
    integration fixture.
    """
    import os
    from urllib.parse import urlsplit, urlunsplit

    import psycopg2

    from tests.integration_helpers import (
        FORECAST_RUN_ID,
        apply_migrations_from_zero,
        seed_issue_126_data,
    )

    # Materialize prod schema + seed.
    apply_migrations_from_zero(integration_database_url)
    seed_issue_126_data(integration_database_url, object_root=tmp_path / "object_store")

    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_pre = cursor.fetchone()[0]

    # Derive admin URL (dbname=postgres on same cluster) + a distinct
    # staging dbname. This mirrors what an operator config would set.
    parsed = urlsplit(integration_database_url)
    admin_url = urlunsplit((parsed.scheme, parsed.netloc, "/postgres", parsed.query, parsed.fragment))
    staging_dbname = f"nhms_archive_drill_a1_{os.getpid()}"
    staging_url = urlunsplit((parsed.scheme, parsed.netloc, f"/{staging_dbname}", parsed.query, parsed.fragment))

    # Build a runs archive fixture bearing the seeded IDs so the real
    # lifter can find its ancestors on prod.
    archive_root = tmp_path / "archive"
    workspace = tmp_path / "workspace"
    receipt_path = tmp_path / "receipts" / "receipt.json"
    zstd_bin = _fake_zstd_simple(tmp_path)

    leaf = archive_root / "runs" / FORECAST_RUN_ID
    leaf.mkdir(parents=True, exist_ok=True)
    rivqdown_body = b"time_minutes,seg\n0,100\n60,110\n"
    tar_bytes = _build_tar_bytes(
        [
            ("input/manifest.json", b"{}"),
            ("output/rivqdown.csv", rivqdown_body),
        ]
    )
    (leaf / "archive.tar.zst").write_bytes(tar_bytes)
    files_manifest = [
        {"path": name, "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body)}
        for name, body in [("input/manifest.json", b"{}"), ("output/rivqdown.csv", rivqdown_body)]
    ]
    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026050300",
            "cycle_time": "2026-05-03T00:00:00Z",
            "run_id": FORECAST_RUN_ID,
        },
        "producer": {
            "kind": "run-manifest",
            "subject_id": FORECAST_RUN_ID,
            "manifest_path": "input/manifest.json",
            "manifest_sha256": hashlib.sha256(b"{}").hexdigest(),
            "start_time": "2026-05-03T01:00:00Z",
            "end_time": "2026-05-03T02:00:00Z",
            "model_id": "it126_model",
            "basin_version_id": "it126_basin_v1",
        },
        "archive": {
            "path": f"runs/{FORECAST_RUN_ID}/archive.tar.zst",
            "manifest_path": f"runs/{FORECAST_RUN_ID}/manifest.json",
            "sha256": hashlib.sha256(tar_bytes).hexdigest(),
            "size_bytes": len(tar_bytes),
        },
        "files": files_manifest,
        "created_at": "2026-06-15T00:00:00Z",
        "tool_version": "node27-product-archive/1",
    }
    manifest_path = leaf / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    workspace.mkdir(parents=True, exist_ok=True)
    config = drill.DrillConfig(
        archive_root=archive_root,
        workspace_root=workspace,
        receipt_path=receipt_path,
        prod_database_url_ro=integration_database_url,
        staging_database_url=staging_url,
        postgres_admin_url=admin_url,
        staging_instance_id="node27-primary-pg15",
        staging_run_label="archive_drill_a1_test",
        zstd_path=zstd_bin,
        archive_manifest_paths=(manifest_path,),
        salvage_manifest_paths=(),
        lock_path=tmp_path / "drill.lock",
    )

    # Track prod read-only setting inspected inside the drill's connection.
    read_only_captured: list[str] = []

    @contextmanager
    def _wrapped_open_prod(dsn: str) -> Iterator[Any]:
        with drill.open_prod_readonly(dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW default_transaction_read_only")
                row = cursor.fetchone()
                read_only_captured.append(str(row[0]).lower())
            yield conn

    # Stub ingest + verify since bootstrapping a valid rivqdown that
    # matches the seeded segments is out of scope for A1 (the OutputParser
    # unit tests already cover that surface).
    ingest_calls: list[str] = []

    def _fake_ingest_runs(
        workspace_arg: Path,
        manifest_arg: Mapping[str, Any],
        staging_database_url: str,
    ) -> Mapping[str, Any]:
        ingest_calls.append(manifest_arg["identity"]["run_id"])
        return {"run_id": manifest_arg["identity"]["run_id"], "rows_written": 0}

    def _fake_verify(dest_dir: Path, manifest_arg: Mapping[str, Any], staging_conn: Any) -> drill.ProductVerification:
        return drill.ProductVerification(
            cycle_label=manifest_arg["identity"]["run_id"],
            expected_row_count=0,
            staging_row_count=0,
            coverage={
                "source": "runs",
                "window": {
                    "start": manifest_arg["producer"]["start_time"],
                    "end": manifest_arg["producer"]["end_time"],
                },
            },
        )

    receipt, outcome = drill.run_drill(
        config,
        open_prod=_wrapped_open_prod,
        ingest_runs=_fake_ingest_runs,
        verify_product=_fake_verify,
    )
    drill.write_receipt(config.receipt_path, receipt)

    assert outcome.verdict == "PASS", receipt.get("differences")
    assert receipt["staging_database"]["database"] == staging_dbname
    assert receipt_path.is_file()
    # Prod session was pinned to read-only.
    assert read_only_captured == ["on"], read_only_captured
    # Ingest was invoked (proves _lift_and_ingest reached the adapter after
    # the real staging_conn.commit(), i.e. A2 works against real Postgres).
    assert ingest_calls == [FORECAST_RUN_ID]

    # Prod row counts unchanged pre/post drill.
    with psycopg2.connect(integration_database_url) as prod_conn:
        with prod_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s",
                (FORECAST_RUN_ID,),
            )
            prod_post = cursor.fetchone()[0]
    assert prod_pre == prod_post

    # Staging DB was torn down (the drill's finally: teardown_staging).
    with psycopg2.connect(admin_url) as admin_conn:
        admin_conn.autocommit = True
        with admin_conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (staging_dbname,),
            )
            remaining = cursor.fetchone()[0]
    assert remaining == 0, f"staging DB {staging_dbname} not dropped"


# ---------------------------------------------------------------------------
# Round 2 fix regressions — one test per finding
# ---------------------------------------------------------------------------


def test_default_lock_path_matches_runbook_string() -> None:
    """NEW-1 (R2): ``_default_lock_path()`` must be byte-identical with
    ``docs/runbooks/tier-node27-timeseries-storage.md`` §7.2 + §7.6 so the
    documented ``rm -f`` recovery is not a no-op.

    Both the runbook and the drill code MUST cite exactly
    ``~/node27-archive-rebuild-drill-logs/drill.lock``.
    """
    expected = Path("~/node27-archive-rebuild-drill-logs/drill.lock").expanduser()
    assert drill._default_lock_path() == expected
    # Signature parity: takes no argument (was `_default_lock_path(receipt_path)`
    # in R1; now a constant so the receipt directory cannot silently drift the
    # lock file location).
    import inspect

    sig = inspect.signature(drill._default_lock_path)
    assert list(sig.parameters) == []


def test_config_from_env_lock_path_defaults_to_runbook_path(
    tmp_path: Path, zstd_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-1 (R2): with ``NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH`` unset,
    ``_config_from_env`` MUST yield ``config.lock_path`` byte-identical
    with the runbook default.
    """
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    receipt_path = tmp_path / "receipts" / "receipt.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env = {
        "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE": str(workspace),
        "NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH": str(receipt_path),
        "PROD_DATABASE_URL_RO": "postgresql://u:p@127.0.0.1:5432/nhms_prod",
        "STAGING_DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/nhms_drill",
        "POSTGRES_ADMIN_URL": "postgresql://u:p@127.0.0.1:5432/postgres",
        "NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID": "node27-primary-pg15",
        "NHMS_ZSTD_BIN": str(zstd_bin),
    }
    config = drill._config_from_env(
        env, ["--archive-manifest", str(manifest_path)]
    )
    assert config.lock_path == Path(
        "~/node27-archive-rebuild-drill-logs/drill.lock"
    ).expanduser()


def test_config_from_env_lock_path_env_override(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """NEW-1 (R2): ``NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH`` MUST override
    the default. Non-absolute values MUST be refused fail-closed so the
    boot-time surface catches operator typos, not a mid-run flock failure.
    """
    manifest_path, _ = _write_fixture_runs_archive(tmp_path)
    receipt_path = tmp_path / "receipts" / "receipt.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    override = tmp_path / "custom" / "drill.lock"
    base_env = {
        "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE": str(workspace),
        "NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH": str(receipt_path),
        "PROD_DATABASE_URL_RO": "postgresql://u:p@127.0.0.1:5432/nhms_prod",
        "STAGING_DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/nhms_drill",
        "POSTGRES_ADMIN_URL": "postgresql://u:p@127.0.0.1:5432/postgres",
        "NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID": "node27-primary-pg15",
        "NHMS_ZSTD_BIN": str(zstd_bin),
    }
    env_ok = {**base_env, "NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH": str(override)}
    config = drill._config_from_env(
        env_ok, ["--archive-manifest", str(manifest_path)]
    )
    assert config.lock_path == override

    env_bad = {**base_env, "NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH": "relative/drill.lock"}
    with pytest.raises(drill.DrillConfigError) as info:
        drill._config_from_env(env_bad, ["--archive-manifest", str(manifest_path)])
    assert "NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH" in str(info.value)


def test_n_mf_1_coverage_attribution_only_for_matched_product_cycles(
    tmp_path: Path, zstd_bin: Path
) -> None:
    """N-mf-1 (R2): when a product cycle's staging count mismatches, its
    coverage window MUST be excluded from ``receipt.coverage[]`` —
    symmetric with the salvage path (``:494-579``) which ``continue``s
    before ``coverage.append`` on any mismatch. Emitting coverage for a
    FAIL cycle would falsely claim the drill covered that window.
    """
    manifest_path_ok, manifest_ok = _write_fixture_runs_archive(
        tmp_path,
        run_id="cycle_match",
        cycle_time_iso="2026-06-01T00:00:00Z",
        cycle_identity="2026060100",
    )
    manifest_path_bad, manifest_bad = _write_fixture_runs_archive(
        tmp_path,
        # Distinct cycle_time_iso so the coverage window (which sources
        # `producer.start_time` = ``cycle_time_iso``) differs from the
        # match cycle — required to prove the FAIL window is dropped, not
        # merely deduped.
        run_id="cycle_mismatch",
        cycle_time_iso="2026-06-02T00:00:00Z",
        cycle_identity="2026060200",
    )
    lifter = _FakeLifter(
        select_return={
            **_closure_map_for_runs(manifest_ok["identity"]["run_id"]),
            **_closure_map_for_runs(manifest_bad["identity"]["run_id"]),
        }
    )

    def _fake_ingest_runs(
        workspace: Path,
        manifest_arg: Mapping[str, Any],
        staging_database_url: str,
    ) -> Mapping[str, Any]:
        return {"run_id": manifest_arg["identity"]["run_id"], "rows_written": 6}

    def _fake_verify(
        dest_dir: Path, manifest_arg: Mapping[str, Any], conn: Any
    ) -> drill.ProductVerification:
        run_id = manifest_arg["identity"]["run_id"]
        expected = 3
        actual = 3 if run_id == "cycle_match" else 2  # mismatch on the second cycle
        return drill.ProductVerification(
            cycle_label=run_id,
            expected_row_count=expected,
            staging_row_count=actual,
            coverage={
                "source": "runs",
                "window": {
                    "start": manifest_arg["producer"]["start_time"],
                    "end": manifest_arg["producer"]["end_time"],
                },
            },
        )

    config = _config(
        tmp_path,
        zstd_path=zstd_bin,
        archive_manifests=[manifest_path_ok, manifest_path_bad],
    )
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
    assert outcome.verdict == "FAIL"
    # Both cycles ran through lift+ingest so both appear in cycles + counts;
    # only the matching cycle's window is attributable to coverage.
    assert "cycle_match" in outcome.cycles
    assert "cycle_mismatch" in outcome.cycles
    coverage_windows = [entry["window"] for entry in receipt["coverage"]]
    match_window = {
        "start": manifest_ok["producer"]["start_time"],
        "end": manifest_ok["producer"]["end_time"],
    }
    mismatch_window = {
        "start": manifest_bad["producer"]["start_time"],
        "end": manifest_bad["producer"]["end_time"],
    }
    assert match_window in coverage_windows
    assert mismatch_window not in coverage_windows
    # STAGING_COUNT_MISMATCH difference is emitted for the mismatched cycle.
    mismatch_codes = {
        d["expected"]["code"]
        for d in receipt["differences"]
        if d.get("item") == "cycle_mismatch"
    }
    assert drill.CODE_STAGING_COUNT_MISMATCH in mismatch_codes
