from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from packages.common import safe_fs
from packages.common.object_store import LocalObjectStore
from scripts import node27_product_archive as mover
from scripts import node27_storage_inventory_audit as audit

_ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
START = datetime(2026, 5, 1, tzinfo=UTC)
END = datetime(2026, 5, 2, tzinfo=UTC)


def _subject(lane: str = "forcing", identifier: str = "forcing-a", **overrides: object) -> audit.InventorySubject:
    values: dict[str, object] = {
        "lane": lane,
        "subject_id": identifier,
        "source_id": "gfs",
        "cycle_time": START,
        "start": START,
        "end": END,
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "hot_uri": "missing",
        "checksum": "a" * 64,
    }
    if lane == "runs":
        values["hot_uri"] = json.dumps(
            {"manifest": f"runs/{identifier}/input/manifest.json", "output": f"runs/{identifier}/output/"}
        )
    if lane == "states":
        values.update(
            {
                "state_id": identifier,
                "start": START,
                "end": START,
                "hot_uri": "states/gfs/model-a/2026050100/state.cfg.ic",
            }
        )
    values.update(overrides)
    return audit.InventorySubject(**values)  # type: ignore[arg-type]


def _config(tmp_path: Path) -> audit.AuditConfig:
    object_root = tmp_path / "objects"
    archive_root = tmp_path / "archive"
    receipt = tmp_path / "receipt.json"
    object_root.mkdir()
    archive_root.mkdir()
    zstd = tmp_path / "fake-zstd"
    zstd.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    zstd.chmod(0o700)
    return audit.AuditConfig(
        "postgresql://redacted", object_root, "s3://nhms", archive_root, 45, receipt, zstd
    )


def _receipt(subjects: list[audit.InventorySubject], *, product=None, salvage=(), hot=None):
    return audit.build_receipt(
        subjects,
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage=product or {},
        salvage_selectors=salvage,
        hot_coverage=hot or {},
    )


def test_verified_archive_is_complete_without_selector() -> None:
    subject = _subject()
    receipt = _receipt([subject], product={subject.stable_key: audit.Coverage("product-archive", ("verified",))})
    assert receipt["windows"][0]["verdict"] == "complete"
    assert receipt["windows"][0]["coverage"] == "product-archive"
    assert receipt["salvage_selectors"] == []


def test_aged_hot_only_is_pending_archive() -> None:
    subject = _subject()
    receipt = _receipt([subject], hot={subject.stable_key: audit.Coverage("hot-object-store")})
    assert receipt["windows"][0]["verdict"] == "pending-archive"
    assert receipt["salvage_selectors"] == []


def test_recent_hot_is_complete() -> None:
    subject = _subject(start=NOW - timedelta(days=2), end=NOW - timedelta(days=1))
    receipt = _receipt([subject], hot={subject.stable_key: audit.Coverage("hot-object-store")})
    assert receipt["windows"][0]["verdict"] == "complete"


@pytest.mark.parametrize(
    ("lane", "identifier", "table", "identity_key"),
    [
        ("forcing", "forcing-a", "met.forcing_station_timeseries", "forcing_version_id"),
        ("runs", "run-a", "hydro.river_timeseries", "run_id"),
    ],
)
def test_timeseries_gap_has_exact_selector(lane: str, identifier: str, table: str, identity_key: str) -> None:
    subject = _subject(lane, identifier)
    receipt = _receipt([subject])
    assert receipt["windows"][0]["verdict"] == "gap"
    assert receipt["salvage_selectors"] == [
        {"table": table, "identity": {identity_key: identifier}, "window": subject.window}
    ]


def test_state_gap_has_no_selector() -> None:
    receipt = _receipt([_subject("states", "state-a")])
    assert receipt["windows"][0]["verdict"] == "gap"
    assert receipt["salvage_selectors"] == []


def test_exact_salvage_covers_subject_but_near_match_does_not() -> None:
    subject = _subject()
    near = {**subject.selector, "window": {"start": audit._time(START), "end": audit._time(END + timedelta(hours=1))}}
    exact = _receipt([subject], salvage=[subject.selector])
    assert exact["windows"][0]["coverage"] == "db-export"
    near_receipt = _receipt([subject], salvage=[near])
    assert near_receipt["windows"][0]["verdict"] == "gap"


def test_equal_windows_keep_distinct_subjects() -> None:
    receipt = _receipt([_subject(identifier="a"), _subject(identifier="b")])
    assert [item["subject"] for item in receipt["windows"]] == [
        {"forcing_version_id": "a"},
        {"forcing_version_id": "b"},
    ]


@pytest.mark.parametrize("mutation", ["duplicate", "omit", "extra_selector", "wrong_bounds"])
def test_semantic_validation_rejects_invalid_set_shapes(mutation: str) -> None:
    subject = _subject()
    receipt = _receipt([subject])
    if mutation == "duplicate":
        receipt["windows"].append(receipt["windows"][0])
    elif mutation == "omit":
        receipt["windows"] = []
    elif mutation == "extra_selector":
        receipt["salvage_selectors"].append({**subject.selector, "identity": {"forcing_version_id": "other"}})
    else:
        receipt["coverage_bounds"]["end"] = audit._time(NOW)
    with pytest.raises(audit.AuditBlocked):
        audit.validate_receipt_semantics(receipt, [subject])


def test_inverted_subject_window_is_blocked() -> None:
    with pytest.raises(audit.AuditBlocked, match="inverted"):
        _subject(start=END, end=START)


@pytest.mark.parametrize("lane", ["forcing", "runs"])
@pytest.mark.parametrize(
    "cycle_time",
    [
        START + timedelta(minutes=1),
        START + timedelta(seconds=1),
        START + timedelta(microseconds=1),
    ],
)
def test_forcing_and_run_cycle_identity_rejects_non_utc_hour(lane: str, cycle_time: datetime) -> None:
    with pytest.raises(audit.AuditBlocked, match="exact UTC hour"):
        _subject(lane, f"{lane}-a", cycle_time=cycle_time)


def test_product_archive_checksum_mismatch_is_absent_and_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    paths.archive.parent.mkdir(parents=True)
    paths.archive.write_bytes(b"bad")
    relative_archive = paths.archive.relative_to(config.archive_root).as_posix()
    relative_manifest = paths.manifest.relative_to(config.archive_root).as_posix()
    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026050100",
            "cycle_time": "2026-05-01T00:00:00Z",
            "basin_version_id": "basin-a",
            "model_id": "model-a",
        },
        "producer": {
            "kind": "forcing-package",
            "subject_id": subject.subject_id,
            "manifest_path": "forcing_package.json",
            "manifest_sha256": subject.checksum,
            "start_time": audit._time(subject.start),
            "end_time": audit._time(subject.end),
            "model_id": subject.model_id,
            "basin_version_id": subject.basin_version_id,
        },
        "archive": {"path": relative_archive, "manifest_path": relative_manifest, "sha256": "0" * 64, "size_bytes": 3},
        "files": [
            {"path": "forcing.csv", "sha256": "1" * 64, "size_bytes": 1},
            {"path": "forcing_package.json", "sha256": subject.checksum, "size_bytes": 1},
        ],
        "created_at": "2026-07-11T00:00:00Z",
        "tool_version": "test/1",
    }
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    coverage = audit.verify_product_archive(subject, config.archive_root)
    assert coverage == audit.Coverage("none", ("product archive size/sha256 mismatch",))
    receipt = _receipt([subject], product={subject.stable_key: coverage})
    assert "mismatch" in receipt["windows"][0]["evidence"][0]


def _write_forcing_and_run_product_archives(
    tmp_path: Path,
) -> tuple[audit.AuditConfig, audit.InventorySubject, audit.InventorySubject]:
    config = _config(tmp_path)
    tool = tmp_path / "fake-zstd"
    tool.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    tool.chmod(0o700)
    config = replace(config, zstd_path=tool)
    forcing_leaf = config.object_store_root / "forcing/gfs/2026050100/basin-a/model-a"
    forcing_leaf.mkdir(parents=True)
    payload = b"forcing-product"
    (forcing_leaf / "payload.csv").write_bytes(payload)
    forcing_manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "basin_version_id": "basin-a",
        "model_id": "model-a",
        "files": [
            {
                "uri": "s3://nhms/forcing/gfs/2026050100/basin-a/model-a/payload.csv",
                "checksum": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    forcing_raw = json.dumps(forcing_manifest).encode()
    (forcing_leaf / "forcing_package.json").write_bytes(forcing_raw)
    run_leaf = config.object_store_root / "runs/run-a"
    (run_leaf / "input").mkdir(parents=True)
    (run_leaf / "output").mkdir()
    (run_leaf / "output/result.nc").write_bytes(b"run-product")
    run_manifest = {
        "run_id": "run-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model": {"model_id": "model-a", "basin_version_id": "basin-a"},
        "outputs": {
            "run_manifest_uri": "s3://nhms/runs/run-a/input/manifest.json",
            "output_uri": "s3://nhms/runs/run-a/output/",
        },
    }
    (run_leaf / "input/manifest.json").write_text(json.dumps(run_manifest), encoding="utf-8")
    mover_config = mover.MoverConfig(
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        archive_root=config.archive_root,
        receipt_path=tmp_path / "mover-receipt.json",
        lock_path=tmp_path / "mover.lock",
        zstd_path=tool,
        enforce=True,
    )
    receipt, code = mover.run(
        mover_config,
        now=NOW,
        mount_id_provider=lambda fd: os.fstat(fd).st_dev,
        rename_impl=lambda src_fd, src, dst_fd, dst: os.rename(
            src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd
        ),
    )
    assert code == 0, json.dumps(receipt, indent=2)
    forcing_subject = _subject(checksum=hashlib.sha256(forcing_raw).hexdigest())
    run_subject = _subject("runs", "run-a")
    return config, forcing_subject, run_subject


def test_product_archive_provenance_closes_forcing_and_run_db_inventory_loop(tmp_path: Path) -> None:
    config, forcing_subject, run_subject = _write_forcing_and_run_product_archives(tmp_path)
    for subject in (forcing_subject, run_subject):
        assert audit.verify_product_archive(
            subject, config.archive_root, config.object_store_prefix, config.zstd_path
        ) == audit.Coverage(
            "product-archive", ("member-verified product archive present",)
        )


def test_product_archive_coverage_reads_and_rejects_tampered_tar_members(tmp_path: Path) -> None:
    config, subject, _run_subject = _write_forcing_and_run_product_archives(tmp_path)
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    manifest = json.loads(paths.manifest.read_text())
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for entry in manifest["files"]:
            content = b"tampered"
            info = tarfile.TarInfo(entry["path"])
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    tampered = stream.getvalue()
    paths.archive.write_bytes(tampered)
    manifest["archive"]["size_bytes"] = len(tampered)
    manifest["archive"]["sha256"] = hashlib.sha256(tampered).hexdigest()
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.AuditBlocked, match="tar member"):
        audit.verify_product_archive(
            subject, config.archive_root, config.object_store_prefix, config.zstd_path
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_id", "other"),
        ("manifest_path", "other.json"),
        ("manifest_sha256", "b" * 64),
        ("start_time", "2026-04-30T00:00:00Z"),
        ("model_id", "other-model"),
        ("basin_version_id", "other-basin"),
    ],
)
def test_product_archive_forcing_provenance_drift_blocks_completion(
    tmp_path: Path, field: str, value: str
) -> None:
    config, subject, _run_subject = _write_forcing_and_run_product_archives(tmp_path)
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    manifest = json.loads(paths.manifest.read_text())
    manifest["producer"][field] = value
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.AuditBlocked, match="producer|schema"):
        audit.verify_product_archive(subject, config.archive_root, config.object_store_prefix, config.zstd_path)


def test_product_and_hot_manifests_enforce_actual_cap_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(audit, "MAX_MANIFEST_BYTES", 4)

    subject = _subject()
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    paths.archive.parent.mkdir(parents=True)
    paths.archive.write_bytes(b"x")
    paths.manifest.write_bytes(b"12345")
    with pytest.raises(audit.AuditBlocked, match=r"manifest exceeds 4 bytes"):
        audit.verify_product_archive(subject, config.archive_root)

    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    (package / "forcing_package.json").write_bytes(b"12345")
    hot_subject = replace(subject, hot_uri=f"s3://nhms/{key}")
    with pytest.raises(audit.AuditBlocked, match=r"manifest exceeds 4 bytes"):
        audit.verify_hot(hot_subject, config)


def test_missing_archive_root_is_ordinary_absence(tmp_path: Path) -> None:
    assert audit.verify_product_archive(_subject(), tmp_path / "missing") is None
    assert audit.discover_salvage(tmp_path / "missing") == ()


def _state_archive_subject(kind: str) -> audit.InventorySubject:
    checksum = hashlib.sha256(b"state").hexdigest()
    if kind == "provider":
        return _subject(
            "states",
            "provider-state",
            hot_uri="states/gfs/model-a/2026050100/cycle-gfs/lead-006/state.cfg.ic",
            checksum=checksum,
        )
    if kind == "legacy":
        return _subject(
            "states",
            "legacy-state",
            source_id=None,
            hot_uri="states/model-a/2026050100/legacy/state.cfg.ic",
            checksum=checksum,
        )
    return _subject(
        "states",
        "clone-state",
        model_id="model-b",
        hot_uri="states/gfs/model-a/2026050100/cycle-gfs/lead-006/state.cfg.ic",
        checksum=checksum,
        cloned_from_state_id="origin-state",
        cloned_from_model_id="model-a",
        clone_gate_fingerprint="f" * 64,
    )


def _write_state_product_archive(config: audit.AuditConfig, subject: audit.InventorySubject, *, mutation: str) -> None:
    paths = audit.archive_provenance_paths(config.archive_root, identity=subject.archive_identity)
    paths.archive.parent.mkdir(parents=True)
    member_content = b"state"
    identity = subject.archive_identity
    identity_payload = {
        "lane": "states",
        "source": identity.source,
        "cycle_identity": identity.cycle_identity,
        "cycle_time": identity.cycle_time,
        "model_id": identity.model_id,
    }
    member = {
        "provider-state": "cycle-gfs/lead-006/state.cfg.ic",
        "legacy-state": "legacy/state.cfg.ic",
        "clone-state": "cycle-gfs/lead-006/state.cfg.ic",
    }[subject.subject_id]
    entry = {"path": member, "sha256": subject.checksum, "size_bytes": 5}
    if mutation in {"missing", "wrong-path"}:
        files = [{**entry, "path": "other/state.cfg.ic"}]
    elif mutation == "duplicate":
        files = [entry, dict(entry)]
    elif mutation == "wrong-checksum":
        files = [{**entry, "sha256": "d" * 64}]
    else:
        files = [entry]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(entry["path"])
        info.size = len(member_content)
        tar.addfile(info, io.BytesIO(member_content))
    archive_content = stream.getvalue()
    paths.archive.write_bytes(archive_content)
    manifest = {
        "schema_version": "1.0",
        "provenance": "product-archive",
        "identity": identity_payload,
        "archive": {
            "path": paths.archive.relative_to(config.archive_root).as_posix(),
            "manifest_path": paths.manifest.relative_to(config.archive_root).as_posix(),
            "sha256": hashlib.sha256(archive_content).hexdigest(),
            "size_bytes": len(archive_content),
        },
        "files": files,
        "created_at": audit._time(NOW),
        "tool_version": "test/1",
    }
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("kind", ["provider", "legacy", "clone"])
def test_state_product_archive_binds_exact_physical_member(kind: str, tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _state_archive_subject(kind)
    _write_state_product_archive(config, subject, mutation="valid")
    coverage = audit.verify_product_archive(
        subject, config.archive_root, config.object_store_prefix, config.zstd_path
    )
    assert coverage == audit.Coverage("product-archive", ("member-verified product archive present",))
    assert subject.stable_key == ("states", f"{kind}-state")


@pytest.mark.parametrize("kind", ["provider", "legacy", "clone"])
@pytest.mark.parametrize("mutation", ["missing", "wrong-path", "duplicate", "wrong-checksum"])
def test_state_product_archive_rejects_unbound_member(kind: str, mutation: str, tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _state_archive_subject(kind)
    _write_state_product_archive(config, subject, mutation=mutation)
    with pytest.raises(audit.AuditBlocked, match="exactly one bound member|checksum differs"):
        audit.verify_product_archive(subject, config.archive_root, config.object_store_prefix)


def test_run_audit_wires_object_store_prefix_into_state_archive_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    subject = replace(
        _state_archive_subject("provider"),
        hot_uri="s3://nhms/states/gfs/model-a/2026050100/cycle-gfs/lead-006/state.cfg.ic",
    )
    _write_state_product_archive(config, subject, mutation="valid")

    class _ConnectionWithClose:
        def close(self) -> None:
            pass

    monkeypatch.setattr(audit, "load_inventory", lambda _connection: (NOW, [subject]))
    monkeypatch.setattr(audit, "discover_salvage", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(audit, "verify_hot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(audit, "publish_receipt", lambda *_args, **_kwargs: None)
    receipt = audit.run_audit(config, connect=lambda _dsn: _ConnectionWithClose())
    assert receipt["windows"][0]["coverage"] == "product-archive"

    wrong_prefix = replace(config, object_store_prefix="s3://other")
    with pytest.raises(audit.AuditBlocked, match="outside configured prefix"):
        audit.run_audit(wrong_prefix, connect=lambda _dsn: _ConnectionWithClose())


def test_forcing_and_run_archive_identity_is_fully_bound_without_state_member_assumption(tmp_path: Path) -> None:
    config = _config(tmp_path)
    forcing = _subject()
    run = _subject("runs", "run-a")
    forcing_paths = audit.archive_provenance_paths(config.archive_root, identity=forcing.archive_identity)
    run_paths = audit.archive_provenance_paths(config.archive_root, identity=run.archive_identity)
    assert forcing.archive_identity.basin_version_id == "basin-a"
    assert forcing.archive_identity.model_id == "model-a"
    assert "forcing/gfs/2026050100/basin-a/model-a" in forcing_paths.archive.as_posix()
    assert run.archive_identity.run_id == "run-a"
    assert "runs/gfs/2026050100/run-a" in run_paths.archive.as_posix()


def test_salvage_discovery_verifies_object_and_rejects_duplicate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    content = b"rows"
    object_path = config.archive_root / "db-export/forcing/a/data.csv.zst"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(content)
    export = {
        "selector": subject.selector,
        "exported_row_count": 1,
        "columns": ["forcing_version_id"],
        "object": {
            "path": object_path.relative_to(config.archive_root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        },
    }
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": audit._time(NOW),
        "source_database": {"database": "nhms", "instance_id": "node27"},
        "exports": [export],
    }
    manifest_path = object_path.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert audit.discover_salvage(config.archive_root) == (subject.selector,)
    second = config.archive_root / "db-export/forcing/b"
    second.mkdir(parents=True)
    (second / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.AuditBlocked, match="duplicate"):
        audit.discover_salvage(config.archive_root)


def test_salvage_checksum_mismatch_is_absent_and_reported(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    object_path = config.archive_root / "db-export/forcing/a/data.csv.zst"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"bad")
    manifest = {
        "schema_version": "1.0",
        "provenance": "db-export",
        "generated_at": audit._time(NOW),
        "source_database": {"database": "nhms", "instance_id": "node27"},
        "exports": [
            {
                "selector": subject.selector,
                "exported_row_count": 1,
                "columns": ["forcing_version_id"],
                "object": {
                    "path": object_path.relative_to(config.archive_root).as_posix(),
                    "sha256": "0" * 64,
                    "size_bytes": 3,
                },
            }
        ],
    }
    (object_path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    mismatches: dict[str, str] = {}
    assert audit.discover_salvage(config.archive_root, mismatch_evidence=mismatches) == ()
    receipt = audit.build_receipt(
        [subject],
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage={},
        salvage_selectors=(),
        hot_coverage={},
        salvage_mismatches=mismatches,
    )
    assert "db-export object size/sha256 mismatch" in receipt["windows"][0]["evidence"]


def test_salvage_symlink_and_depth_are_blocked(tmp_path: Path) -> None:
    config = _config(tmp_path)
    base = config.archive_root / "db-export"
    base.mkdir()
    (base / "link").symlink_to(tmp_path)
    with pytest.raises(audit.AuditBlocked, match="symlink"):
        audit.discover_salvage(config.archive_root)
    (base / "link").unlink()
    current = base
    for index in range(audit.MAX_SALVAGE_DEPTH + 1):
        current = current / str(index)
        current.mkdir()
    with pytest.raises(audit.AuditBlocked, match="depth"):
        audit.discover_salvage(config.archive_root)


def test_salvage_total_entry_cap_is_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    (config.archive_root / "db-export").mkdir()
    monkeypatch.setattr(
        audit,
        "_list_salvage_directory_fd",
        lambda _fd, _label, _max: [str(index) for index in range(audit.MAX_SALVAGE_ENTRIES + 1)],
    )
    with pytest.raises(audit.AuditBlocked, match="total entries"):
        audit.discover_salvage(config.archive_root)


def test_salvage_fd_manifest_enforces_actual_cap_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    manifest = config.archive_root / "db-export/a/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"12345")
    monkeypatch.setattr(audit, "MAX_MANIFEST_BYTES", 4)

    with pytest.raises(audit.AuditBlocked, match=r"manifest exceeds 4 bytes"):
        audit.discover_salvage(config.archive_root)


def test_salvage_manifest_count_cap_is_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    for identifier in ("a", "b"):
        content = identifier.encode()
        object_path = config.archive_root / f"db-export/{identifier}/data.csv.zst"
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(content)
        subject = _subject(identifier=f"forcing-{identifier}")
        manifest = {
            "schema_version": "1.0",
            "provenance": "db-export",
            "generated_at": audit._time(NOW),
            "source_database": {"database": "nhms", "instance_id": "node27"},
            "exports": [
                {
                    "selector": subject.selector,
                    "exported_row_count": 1,
                    "columns": ["forcing_version_id"],
                    "object": {
                        "path": object_path.relative_to(config.archive_root).as_posix(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    },
                }
            ],
        }
        (object_path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(audit, "MAX_SALVAGE_MANIFESTS", 1)
    with pytest.raises(audit.AuditBlocked, match=r"exceeds 1 manifests"):
        audit.discover_salvage(config.archive_root)


def test_salvage_root_swap_after_listing_stays_on_held_fd_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    base = config.archive_root / "db-export"
    replacement = tmp_path / "replacement-db-export"

    def write_export(root: Path, selector: dict[str, object], content: bytes) -> None:
        object_path = root / "good/data.csv.zst"
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(content)
        manifest = {
            "schema_version": "1.0",
            "provenance": "db-export",
            "generated_at": audit._time(NOW),
            "source_database": {"database": "nhms", "instance_id": "node27"},
            "exports": [
                {
                    "selector": selector,
                    "exported_row_count": 1,
                    "columns": ["forcing_version_id"],
                    "object": {
                        "path": "db-export/good/data.csv.zst",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                    },
                }
            ],
        }
        (object_path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    original_selector = _subject(identifier="forcing-original").selector
    replacement_selector = _subject(identifier="forcing-replacement").selector
    write_export(base, original_selector, b"original")
    write_export(replacement, replacement_selector, b"replacement")
    for name in ("extra-a", "extra-b", "extra-c"):
        (replacement / name).mkdir()

    original_list = audit._list_salvage_directory_fd
    swapped = False

    def list_then_swap(directory_fd: int, label: Path, max_entries: int) -> list[str]:
        nonlocal swapped
        names = original_list(directory_fd, label, max_entries)
        if label == base and not swapped:
            swapped = True
            base.rename(config.archive_root / "db-export-held")
            replacement.rename(base)
        return names

    monkeypatch.setattr(audit, "MAX_SALVAGE_ENTRIES", 3)
    monkeypatch.setattr(audit, "_list_salvage_directory_fd", list_then_swap)
    assert audit.discover_salvage(config.archive_root) == (original_selector,)
    assert swapped


def test_limited_no_follow_read_loops_across_short_reads_and_returns_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "short-read.bin"
    path.write_bytes(b"0123456789oversized")
    original_read = os.read

    def short_read(fd: int, size: int) -> bytes:
        return original_read(fd, min(size, 3))

    monkeypatch.setattr(safe_fs.os, "read", short_read)
    content = safe_fs.read_bytes_limited_no_follow(path, max_bytes=10, containment_root=tmp_path)
    assert content == b"0123456789o"
    assert len(content) == 11


def test_forcing_hot_binds_manifest_and_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    data = b"forcing"
    data_path = package / "data.csv"
    data_path.write_bytes(data)
    manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "files": [{"uri": f"s3://nhms/{key}/data.csv", "checksum": hashlib.sha256(data).hexdigest()}],
    }
    manifest_path = package / "forcing_package.json"
    raw = json.dumps(manifest).encode()
    manifest_path.write_bytes(raw)
    subject = _subject(hot_uri=f"s3://nhms/{key}", checksum=hashlib.sha256(raw).hexdigest())
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"
    bad = replace(subject, basin_version_id="other")
    with pytest.raises(audit.AuditBlocked, match="URI identity"):
        audit.verify_hot(bad, config)


def test_hot_forcing_checksum_mismatch_is_gap_evidence_and_survives_archive_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    data = b"forcing"
    (package / "data.csv").write_bytes(data)
    manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "files": [{"uri": f"s3://nhms/{key}/data.csv", "checksum": "0" * 64}],
    }
    raw = json.dumps(manifest).encode()
    (package / "forcing_package.json").write_bytes(raw)
    subject = _subject(hot_uri=f"s3://nhms/{key}", checksum=hashlib.sha256(raw).hexdigest())
    hot = audit.verify_hot(subject, config)
    assert hot is not None and hot.mechanism == "none" and "member checksum mismatch" in hot.evidence[0]

    gap = _receipt([subject], hot={subject.stable_key: hot})
    assert gap["windows"][0]["coverage"] == "none"
    assert hot.evidence[0] in gap["windows"][0]["evidence"]
    fallback = _receipt(
        [subject],
        product={subject.stable_key: audit.Coverage("product-archive", ("product valid",))},
        hot={subject.stable_key: hot},
    )
    assert fallback["windows"][0]["coverage"] == "product-archive"
    assert hot.evidence[0] in fallback["windows"][0]["evidence"]


def test_hot_forcing_manifest_checksum_mismatch_is_absent_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    data = b"forcing"
    (package / "data.csv").write_bytes(data)
    manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "files": [
            {"uri": f"s3://nhms/{key}/data.csv", "checksum": hashlib.sha256(data).hexdigest()}
        ],
    }
    (package / "forcing_package.json").write_text(json.dumps(manifest), encoding="utf-8")
    subject = _subject(hot_uri=f"s3://nhms/{key}", checksum="0" * 64)
    assert audit.verify_hot(subject, config) == audit.Coverage(
        "none", ("hot forcing manifest checksum mismatch",)
    )


def test_run_hot_requires_row_bound_manifest_and_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject("runs", "run-a")
    manifest_path = config.object_store_root / "runs/run-a/input/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "source_id": "gfs",
                "cycle_time": audit._time(START),
                "start_time": audit._time(START),
                "end_time": audit._time(END),
                "model": {"model_id": "model-a", "basin_version_id": "basin-a"},
                "outputs": {
                    "run_manifest_uri": "s3://nhms/runs/run-a/input/manifest.json",
                    "output_uri": "s3://nhms/runs/run-a/output/",
                },
            }
        ),
        encoding="utf-8",
    )
    output = config.object_store_root / "runs/run-a/output/result.csv"
    output.parent.mkdir(parents=True)
    output.write_text("x", encoding="utf-8")
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"
    output.unlink()
    with pytest.raises(audit.AuditBlocked, match="no regular product"):
        audit.verify_hot(subject, config)


def test_provider_legacy_and_clone_state_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    content = b"state"
    checksum = hashlib.sha256(content).hexdigest()
    provider = _subject("states", "provider", checksum=checksum)
    provider_path = config.object_store_root / provider.hot_uri
    provider_path.parent.mkdir(parents=True)
    provider_path.write_bytes(content)
    assert audit.verify_hot(provider, config).mechanism == "hot-object-store"
    legacy = _subject(
        "states", "legacy", source_id=None, hot_uri="states/model-a/2026050100/state.cfg.ic", checksum=checksum
    )
    legacy_path = config.object_store_root / legacy.hot_uri
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(content)
    assert legacy.archive_identity.source == "legacy-unqualified"
    assert audit.verify_hot(legacy, config).mechanism == "hot-object-store"
    clone = _subject(
        "states",
        "clone",
        model_id="model-b",
        hot_uri=provider.hot_uri,
        checksum=checksum,
        cloned_from_state_id="provider",
        cloned_from_model_id="model-a",
        clone_gate_fingerprint="f" * 64,
    )
    assert audit.verify_hot(clone, config).mechanism == "hot-object-store"
    with pytest.raises(audit.AuditBlocked, match="identity mismatch"):
        audit.verify_hot(replace(clone, cloned_from_model_id="model-c"), config)


def test_hot_state_checksum_mismatch_is_gap_evidence_and_survives_archive_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject("states", "state-a", checksum="0" * 64)
    path = config.object_store_root / subject.hot_uri
    path.parent.mkdir(parents=True)
    path.write_bytes(b"state")
    hot = audit.verify_hot(subject, config)
    assert hot == audit.Coverage("none", ("hot state checksum mismatch: state-a",))

    gap = _receipt([subject], hot={subject.stable_key: hot})
    assert gap["windows"][0]["coverage"] == "none"
    assert hot.evidence[0] in gap["windows"][0]["evidence"]
    fallback = _receipt(
        [subject],
        product={subject.stable_key: audit.Coverage("product-archive", ("product valid",))},
        hot={subject.stable_key: hot},
    )
    assert fallback["windows"][0]["coverage"] == "product-archive"
    assert hot.evidence[0] in fallback["windows"][0]["evidence"]


def test_state_provider_path_preserves_canonical_source_case(tmp_path: Path) -> None:
    config = _config(tmp_path)
    content = b"state"
    checksum = hashlib.sha256(content).hexdigest()
    subject = _subject(
        "states",
        "era5-state",
        source_id="ERA5",
        hot_uri="states/ERA5/model-a/2026050100/state.cfg.ic",
        checksum=checksum,
    )
    path = config.object_store_root / subject.hot_uri
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert audit.verify_hot(subject, config).mechanism == "hot-object-store"


class _Cursor:
    def __init__(self, result_sets: list[list[dict[str, object]]]):
        self.result_sets = iter(result_sets)
        self.rows: list[dict[str, object]] = []
        self.executed: list[str] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str):
        self.executed.append(sql)
        if sql.lstrip().startswith("SELECT"):
            self.rows = next(self.result_sets)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, sets):
        self.cursor_value = _Cursor(sets)
        self.rolled_back = False

    def cursor(self):
        return self.cursor_value

    def rollback(self):
        self.rolled_back = True


def test_inventory_transaction_filters_zero_detail_by_identity_lateral_presence() -> None:
    forcing = {
        "forcing_version_id": "forcing-a",
        "model_id": "model-a",
        "source_id": "gfs",
        "cycle_time": START,
        "start_time": START,
        "end_time": END,
        "forcing_package_uri": "missing",
        "checksum": "a" * 64,
        "basin_version_id": "basin-a",
    }
    connection = _Connection([[{"audit_time": NOW}], [forcing], [], []])
    captured, subjects = audit.load_inventory(connection)
    assert captured == NOW and len(subjects) == 1 and connection.rolled_back
    sql = "\n".join(connection.cursor_value.executed)
    assert "REPEATABLE READ READ ONLY" in sql
    assert "20000ms" in sql
    assert "CROSS JOIN LATERAL" in audit.FORCING_INVENTORY_SQL
    assert "LIMIT 1" in audit.FORCING_INVENTORY_SQL


def test_empty_inventory_and_partial_clone_provenance_are_blocked() -> None:
    with pytest.raises(audit.AuditBlocked, match="empty"):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], []]))
    state = {
        "state_id": "s",
        "model_id": "m",
        "run_id": "r",
        "source_id": "gfs",
        "valid_time": START,
        "state_uri": "states/gfs/m/2026050100/state.cfg.ic",
        "checksum": "a" * 64,
        "cloned_from_state_id": "x",
        "cloned_from_model_id": None,
        "clone_gate_fingerprint": None,
    }
    with pytest.raises(audit.AuditBlocked, match="incomplete clone"):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [state]]))


def test_inventory_subject_cap_precedes_field_parsing_and_accepts_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit, "MAX_SUBJECTS", 2)
    with pytest.raises(audit.AuditBlocked, match=r"exceeds 2 subjects"):
        audit.load_inventory(
            _Connection(
                [
                    [{"audit_time": NOW}],
                    [{"invalid_forcing_row": True}],
                    [{"invalid_run_row": True}],
                    [{"invalid_state_row": True}],
                ]
            )
        )

    forcing = {
        "forcing_version_id": "forcing-a",
        "model_id": "model-a",
        "source_id": "gfs",
        "cycle_time": START,
        "start_time": START,
        "end_time": END,
        "forcing_package_uri": "missing",
        "checksum": "a" * 64,
        "basin_version_id": "basin-a",
    }
    run = {
        "run_id": "run-a",
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "source_id": "gfs",
        "cycle_time": START,
        "start_time": START,
        "end_time": END,
        "run_manifest_uri": "s3://nhms/runs/run-a/input/manifest.json",
        "output_uri": "s3://nhms/runs/run-a/output/",
        "detail_present": 1,
    }
    _audit_time, subjects = audit.load_inventory(
        _Connection([[{"audit_time": NOW}], [forcing], [run], []])
    )
    assert [subject.stable_key for subject in subjects] == [("forcing", "forcing-a"), ("runs", "run-a")]


def test_publish_is_mode_0600_atomic_and_preserves_old_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt.json"
    old_receipt = _receipt([_subject(identifier="forcing-old")])
    new_receipt = _receipt([_subject(identifier="forcing-new")])
    audit.publish_receipt(path, old_receipt)
    assert json.loads(path.read_text()) == old_receipt
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(audit.AuditBlocked, match="boom"):
        audit.publish_receipt(path, new_receipt)
    assert path.read_bytes() == before
    assert json.loads(path.read_text()) == old_receipt
    assert not list(tmp_path.glob(".*.tmp"))


def test_publish_parent_swap_after_replace_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "receipts"
    parent.mkdir()
    path = parent / "receipt.json"
    path.write_bytes(b"old")
    moved = tmp_path / "receipts-moved"
    original_replace = os.replace

    def replace_then_swap(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        original_replace(parent, moved)
        parent.mkdir()
        return result

    monkeypatch.setattr(safe_fs.os, "replace", replace_then_swap)
    with pytest.raises(audit.PublicationIndeterminate, match="indeterminate.*parent identity changed"):
        audit.publish_receipt(path, _receipt([_subject()]))
    assert not path.exists()
    assert json.loads((moved / "receipt.json").read_text()) == _receipt([_subject()])


def test_publish_directory_fsync_eio_is_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "receipt.json"
    old_receipt = _receipt([_subject(identifier="forcing-old")])
    new_receipt = _receipt([_subject(identifier="forcing-new")])
    audit.publish_receipt(path, old_receipt)
    original_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(safe_fs.os, "fsync", fail_directory_fsync)
    with pytest.raises(audit.PublicationIndeterminate, match="indeterminate.*directory fsync failed"):
        audit.publish_receipt(path, new_receipt)
    published = json.loads(path.read_text(encoding="utf-8"))
    assert published == new_receipt
    schema = json.loads(Path("schemas/archive_completeness_receipt.schema.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(published)
    audit.validate_receipt_semantics(published, [_subject(identifier="forcing-new")])


def test_default_atomic_writer_keeps_legacy_best_effort_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    original_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(safe_fs.os, "fsync", fail_directory_fsync)
    uri = store.write_bytes_atomic("forcing/gfs/2026050100/basin-a/model-a/data.csv", b"rows")
    assert uri.endswith("forcing/gfs/2026050100/basin-a/model-a/data.csv")
    assert store.read_bytes("forcing/gfs/2026050100/basin-a/model-a/data.csv") == b"rows"


def test_publish_rejects_relative_or_symlinked_paths(tmp_path: Path) -> None:
    receipt = _receipt([_subject()])
    with pytest.raises(audit.AuditBlocked, match="absolute"):
        audit.publish_receipt(Path("receipt.json"), receipt)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(audit.AuditBlocked, match="unsafe|not a directory"):
        audit.publish_receipt(linked / "receipt.json", receipt)


def test_main_failure_is_json_stderr_and_does_not_print_dsn(capsys: pytest.CaptureFixture[str]) -> None:
    assert audit.main([]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["status"] == "blocked"
    assert "postgresql" not in captured.err


def test_main_redacts_dsn_from_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn = "postgresql://user:secret@db/nhms"
    config = _config(tmp_path)
    config = replace(config, database_url=dsn)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(RuntimeError(f"failed {dsn}")),
    )
    assert audit.main([]) == 1
    captured = capsys.readouterr()
    receipt = json.loads(config.receipt_path.read_text())
    assert receipt["outcome"] == "indeterminate"
    assert dsn not in captured.out and dsn not in json.dumps(receipt)
    assert "[DATABASE_URL]" in receipt["detail"]


def test_main_reports_post_replace_failure_as_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(
        audit,
        "publish_receipt",
        lambda *_args: (_ for _ in ()).throw(audit.PublicationIndeterminate("directory fsync failed")),
    )
    assert audit.main([]) == 1
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["status"] == "blocked"
    assert diagnostic["reason"] == audit.PUBLICATION_INDETERMINATE_CODE


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["--unknown-option"],
        ["--archive-min-age-days", "not-an-integer"],
        [],
    ],
)
def test_main_parser_or_config_failure_publishes_blocked_receipt(
    tmp_path: Path,
    argv_tail: list[str],
) -> None:
    receipt_path = tmp_path / "terminal.json"
    assert audit.main(["--receipt-path", str(receipt_path), *argv_tail]) == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema_version"] == "1.1"
    assert receipt["outcome"] == "blocked"
    assert receipt["refusal_reason"] == "CONFIG_INVALID"


@pytest.mark.parametrize(
    ("flag", "path_position", "path_source", "seed_outcome"),
    [
        ("-h", "after", "cli", "complete"),
        ("--help", "before", "cli", "incomplete"),
        ("-h", "none", "env", "complete"),
        ("--help", "none", "env", "incomplete"),
    ],
)
def test_help_replaces_stale_success_with_config_blocked_receipt_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    path_position: str,
    path_source: str,
    seed_outcome: str,
) -> None:
    config = _config(tmp_path)
    seed_subject = (
        _subject(start=NOW - timedelta(days=1), end=NOW)
        if seed_outcome == "complete"
        else _subject()
    )
    old_receipt = _receipt(
        [seed_subject],
        hot={seed_subject.stable_key: audit.Coverage("hot-object-store")}
        if seed_outcome == "complete"
        else {},
    )
    assert old_receipt["outcome"] == seed_outcome
    audit.publish_receipt(config.receipt_path, old_receipt)
    if path_source == "env":
        monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
        argv = [flag]
    elif path_position == "after":
        argv = [flag, "--receipt-path", str(config.receipt_path)]
    else:
        argv = ["--receipt-path", str(config.receipt_path), flag]
    original_publish = audit.publish_receipt
    attempts = 0

    def count_publish(path: Path, receipt: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        original_publish(path, receipt)

    monkeypatch.setattr(audit, "publish_receipt", count_publish)
    assert audit.main(argv) == 1
    assert attempts == 1
    receipt = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "blocked"
    assert receipt["refusal_reason"] == "CONFIG_INVALID"
    assert receipt != old_receipt
    schema = json.loads(audit.COMPLETENESS_SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
    audit.validate_receipt_semantics(receipt)


def test_help_without_safe_destination_is_stderr_only_and_preserves_unsafe_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", raising=False)
    assert audit.main(["--help"]) == 1
    missing = capsys.readouterr()
    assert not missing.out
    assert json.loads(missing.err)["reason"] == "CONFIG_INVALID"

    real = tmp_path / "real"
    real.mkdir()
    old_path = real / "receipt.json"
    old_path.write_text(json.dumps(_receipt([_subject()])), encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    assert audit.main(["-h", "--receipt-path", str(linked / "receipt.json")]) == 1
    unsafe = capsys.readouterr()
    assert not unsafe.out
    assert json.loads(unsafe.err)["reason"] == "CONFIG_INVALID"
    assert json.loads(old_path.read_text(encoding="utf-8"))["outcome"] == "incomplete"


def test_help_publication_fault_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    audit.publish_receipt(config.receipt_path, _receipt([_subject()]))
    attempts = 0

    def fail_once(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        raise audit.AuditBlocked("publication failed token=help-secret")

    monkeypatch.setattr(audit, "publish_receipt", fail_once)
    assert audit.main(["--receipt-path", str(config.receipt_path), "--help"]) == 1
    assert attempts == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["reason"] == audit.PUBLICATION_FAILED_CODE
    assert "help-secret" not in captured.err


@pytest.mark.parametrize("outcome", ["complete", "incomplete"])
def test_main_publishes_each_success_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    config = _config(tmp_path)
    subject = _subject(start=NOW - timedelta(days=1), end=NOW) if outcome == "complete" else _subject()
    hot = (
        {subject.stable_key: audit.Coverage("hot-object-store")}
        if outcome == "complete"
        else {}
    )
    success = _receipt([subject], hot=hot)
    assert success["outcome"] == outcome
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(audit, "run_audit", lambda _config, *, publish: success)
    assert audit.main([]) == 0
    assert json.loads(config.receipt_path.read_text()) == success


@pytest.mark.parametrize(
    "argv",
    [
        ["--receipt-path"],
        ["--receipt-path="],
        ["--receipt-path", "/tmp/a", "--receipt-path=/tmp/b"],
        ["--receipt-path", "relative.json"],
        [],
    ],
)
def test_unwriteable_receipt_bootstrap_is_stderr_only(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", raising=False)
    assert audit.main(argv) == 1
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["reason"] == "CONFIG_INVALID"
    assert diagnostic["status"] == "blocked"


def test_cli_receipt_path_overrides_environment_independently_of_argument_order(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "from-env.json"
    cli_path = tmp_path / "from-cli.json"
    os.environ["NODE27_STORAGE_INVENTORY_RECEIPT_PATH"] = str(env_path)
    try:
        assert audit.main(["--unknown", "--receipt-path=" + str(cli_path)]) == 1
    finally:
        os.environ.pop("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", None)
    assert cli_path.is_file()
    assert not env_path.exists()


def test_real_db_shaped_prefix_mismatch_reaches_main_and_publishes_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    key = "forcing/gfs/2026050100/basin-a/model-a"
    package = config.object_store_root / key
    package.mkdir(parents=True)
    data = b"forcing-evidence"
    (package / "data.csv").write_bytes(data)
    manifest = {
        "forcing_version_id": "forcing-a",
        "source_id": "gfs",
        "cycle_time": audit._time(START),
        "start_time": audit._time(START),
        "end_time": audit._time(END),
        "model_id": "model-a",
        "basin_version_id": "basin-a",
        "files": [
            {
                "uri": f"s3://nhms/{key}/data.csv",
                "checksum": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    raw_manifest = json.dumps(manifest).encode()
    (package / "forcing_package.json").write_bytes(raw_manifest)
    forcing_row = {
        "forcing_version_id": "forcing-a",
        "model_id": "model-a",
        "source_id": "gfs",
        "cycle_time": START,
        "start_time": START,
        "end_time": END,
        "forcing_package_uri": f"s3://nhms/{key}",
        "checksum": hashlib.sha256(raw_manifest).hexdigest(),
        "basin_version_id": "basin-a",
    }

    class Connection(_Connection):
        def __init__(self) -> None:
            super().__init__([[{"audit_time": NOW}], [forcing_row], [], []])
            self.closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr("psycopg2.connect", lambda _dsn: connection)
    argv = [
        "--receipt-path",
        str(config.receipt_path),
        "--database-url",
        config.database_url,
        "--object-store-root",
        str(config.object_store_root),
        "--object-store-prefix",
        "s3://wrong-bucket",
        "--archive-root",
        str(config.archive_root),
        "--zstd-path",
        str(config.zstd_path),
    ]
    assert audit.main(argv) == 1
    receipt = json.loads(config.receipt_path.read_text())
    assert receipt["outcome"] == "blocked"
    assert receipt["refusal_reason"] == "OBJECT_URI_PREFIX_MISMATCH"
    assert "s3://nhms/forcing/" in receipt["detail"]
    assert connection.rolled_back and connection.closed
    sql = "\n".join(connection.cursor_value.executed)
    assert "REPEATABLE READ READ ONLY" in sql
    assert audit.FORCING_INVENTORY_SQL in connection.cursor_value.executed
    assert audit.RUN_INVENTORY_SQL in connection.cursor_value.executed
    assert audit.STATE_INVENTORY_SQL in connection.cursor_value.executed


def test_unexpected_prepublication_error_publishes_sanitized_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    dsn = "postgresql://reader:top-secret@db/nhms"
    config = replace(config, database_url=dsn)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(RuntimeError(f"driver echoed {dsn}")),
    )
    assert audit.main([]) == 1
    receipt = json.loads(config.receipt_path.read_text())
    assert receipt["outcome"] == "indeterminate"
    assert receipt["error_reason"] == audit.UNEXPECTED_ERROR_REASON
    assert "top-secret" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "error",
    [
        audit.AuditBlocked(
            "AWS_SECRET_ACCESS_KEY=aws-secret Authorization: Bearer bearer-secret "
            "token=opaque-token https://user:pass@example.test/path?X-Amz-Signature=signed#api_key=tail"
        ),
        RuntimeError(
            "AWS_ACCESS_KEY_ID=aws-key Authorization: Basic basic-secret "
            "api_key=opaque-key https://user:pass@example.test/path?token=query#secret=tail"
        ),
    ],
)
def test_terminal_receipt_redacts_generic_credentials_for_controlled_and_unexpected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(error),
    )
    assert audit.main([]) == 1
    body = config.receipt_path.read_text(encoding="utf-8")
    for secret in (
        "aws-secret",
        "aws-key",
        "bearer-secret",
        "basic-secret",
        "opaque-token",
        "opaque-key",
        "user:pass",
        "X-Amz-Signature",
        "signed",
        "api_key=tail",
        "token=query",
        "secret=tail",
    ):
        assert secret not in body


def test_terminal_receipt_redacts_driver_decoded_and_libpq_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = "postgresql://reader:p%40ss%20word@db/nhms"
    config = replace(_config(tmp_path), database_url=dsn)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(
            RuntimeError(
                f"dsn={dsn}; decoded=p@ss word; password='quoted with spaces'; "
                "password=escaped\\ value host=db"
            )
        ),
    )
    assert audit.main([]) == 1
    body = config.receipt_path.read_text(encoding="utf-8")
    for secret in (dsn, "p%40ss%20word", "p@ss word", "quoted with spaces", "escaped\\ value"):
        assert secret not in body


@pytest.mark.parametrize(
    ("error", "expected_outcome"),
    [
        (audit.AuditBlocked("blocked path-\udcff token=secret"), "blocked"),
        (RuntimeError("unexpected path-\udcff token=secret"), "indeterminate"),
    ],
)
def test_lone_surrogate_terminal_error_replaces_stale_success_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_outcome: str,
) -> None:
    config = _config(tmp_path)
    old_receipt = _receipt([_subject(identifier="stale-success")])
    audit.publish_receipt(config.receipt_path, old_receipt)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(error),
    )
    original_publish = audit.publish_receipt
    attempts = 0

    def count_publish(path: Path, receipt: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        original_publish(path, receipt)

    monkeypatch.setattr(audit, "publish_receipt", count_publish)
    assert audit.main([]) == 1
    assert attempts == 1
    raw = config.receipt_path.read_bytes()
    published = json.loads(raw)
    assert published["outcome"] == expected_outcome
    assert published != old_receipt
    assert "\udcff" not in raw.decode("utf-8")
    assert "secret" not in raw.decode("utf-8")
    schema = json.loads(audit.COMPLETENESS_SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(published)
    audit.validate_receipt_semantics(published)


def test_lone_surrogate_and_credentials_are_sanitized_on_bootstrap_and_publication_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", raising=False)
    assert audit.main(["--receipt-path", "relative-\udcff-token=bootstrap-secret"]) == 1
    bootstrap = capsys.readouterr().err
    assert json.loads(bootstrap)["reason"] == "CONFIG_INVALID"
    assert "\udcff" not in bootstrap and "bootstrap-secret" not in bootstrap
    assert bootstrap.encode("utf-8")

    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(audit, "run_audit", lambda _config, *, publish: _receipt([_subject()]))
    monkeypatch.setattr(
        audit,
        "publish_receipt",
        lambda *_args: (_ for _ in ()).throw(
            audit.AuditBlocked("write path-\udcff AWS_SECRET_ACCESS_KEY=publication-secret")
        ),
    )
    assert audit.main([]) == 1
    publication = capsys.readouterr().err
    assert json.loads(publication)["reason"] == audit.PUBLICATION_FAILED_CODE
    assert "\udcff" not in publication and "publication-secret" not in publication
    assert publication.encode("utf-8")


@pytest.mark.parametrize(
    ("error", "expected_outcome"),
    [
        (
            audit.AuditBlocked('password="' + "\\" * 200_000),
            "blocked",
        ),
        (
            RuntimeError(
                "remote https://user:url-secret@example.test:not-a-port/path?token=query"
            ),
            "indeterminate",
        ),
    ],
)
def test_hostile_assignment_and_malformed_url_replace_stale_success_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_outcome: str,
) -> None:
    config = _config(tmp_path)
    stale = _receipt([_subject(identifier="stale-before-redaction-error")])
    audit.publish_receipt(config.receipt_path, stale)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(error),
    )
    original_publish = audit.publish_receipt
    attempts = 0

    def count_publish(path: Path, receipt: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        original_publish(path, receipt)

    monkeypatch.setattr(audit, "publish_receipt", count_publish)
    assert audit.main([]) == 1
    assert attempts == 1
    receipt = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == expected_outcome
    assert receipt != stale
    body = json.dumps(receipt)
    assert "url-secret" not in body and "token=query" not in body
    schema = json.loads(audit.COMPLETENESS_SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
    audit.validate_receipt_semantics(receipt)


def test_malformed_url_is_fail_closed_on_bootstrap_and_publication_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    malformed = "https://user:url-secret@example.test:not-a-port/path?token=query"
    original_bootstrap = audit.bootstrap_receipt_path
    monkeypatch.setattr(
        audit,
        "bootstrap_receipt_path",
        lambda _argv: (_ for _ in ()).throw(audit.AuditBlocked(f"bad path {malformed}")),
    )
    assert audit.main([]) == 1
    bootstrap = capsys.readouterr().err
    assert json.loads(bootstrap)["reason"] == "CONFIG_INVALID"
    assert "url-secret" not in bootstrap and "token=query" not in bootstrap

    monkeypatch.setattr(audit, "bootstrap_receipt_path", original_bootstrap)
    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(audit, "run_audit", lambda _config, *, publish: _receipt([_subject()]))
    attempts = 0

    def fail_publish(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        raise audit.AuditBlocked(f"publication path {malformed}")

    monkeypatch.setattr(audit, "publish_receipt", fail_publish)
    assert audit.main([]) == 1
    assert attempts == 1
    publication = capsys.readouterr().err
    assert json.loads(publication)["reason"] == audit.PUBLICATION_FAILED_CODE
    assert "url-secret" not in publication and "token=query" not in publication


def test_empty_inventory_publishes_blocked_instead_of_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(
        audit,
        "run_audit",
        lambda _config, *, publish: (_ for _ in ()).throw(audit.AuditBlocked("inventory is empty")),
    )
    assert audit.main([]) == 1
    receipt = json.loads(config.receipt_path.read_text())
    assert receipt["outcome"] == "blocked"
    assert receipt["refusal_reason"] == "EMPTY_INVENTORY"
    assert "windows" not in receipt


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (audit.AuditBlocked("write failed"), audit.PUBLICATION_FAILED_CODE),
        (audit.PublicationIndeterminate("directory fsync failed"), audit.PUBLICATION_INDETERMINATE_CODE),
    ],
)
def test_main_publication_failure_attempts_exactly_once_and_never_recurses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_code: str,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH", str(config.receipt_path))
    monkeypatch.setattr(audit, "config_from_args", lambda _args: config)
    monkeypatch.setattr(audit, "run_audit", lambda _config, *, publish: _receipt([_subject()]))
    attempts = 0

    def fail_once(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(audit, "publish_receipt", fail_once)
    assert audit.main([]) == 1
    assert attempts == 1
    assert json.loads(capsys.readouterr().err)["reason"] == expected_code


def test_terminal_schema_has_four_exact_mutually_exclusive_branches() -> None:
    schema = json.loads(audit.COMPLETENESS_SCHEMA_PATH.read_text())
    success = _receipt([_subject()])
    receipts = [
        {**success, "outcome": "incomplete"},
        audit.build_terminal_receipt("blocked", NOW, reason="EVIDENCE_BLOCKED", detail="safe"),
        audit.build_terminal_receipt(
            "indeterminate", NOW, reason=audit.UNEXPECTED_ERROR_REASON, detail="safe"
        ),
    ]
    complete_subject = _subject(start=NOW - timedelta(days=1), end=NOW)
    receipts.insert(
        0,
        _receipt(
            [complete_subject],
            hot={complete_subject.stable_key: audit.Coverage("hot-object-store")},
        ),
    )
    for receipt in receipts:
        matches = 0
        for branch in schema["oneOf"]:
            branch_schema = {"definitions": schema["definitions"], **branch}
            if not list(
                jsonschema.Draft7Validator(
                    branch_schema, format_checker=jsonschema.FormatChecker()
                ).iter_errors(receipt)
            ):
                matches += 1
        assert matches == 1, receipt
        jsonschema.Draft7Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(receipt)


@pytest.mark.parametrize("reason", sorted(audit.BLOCKED_REASONS))
def test_all_stable_blocked_reason_codes_are_schema_valid(reason: str) -> None:
    receipt = audit.build_terminal_receipt("blocked", NOW, reason=reason, detail="sanitized")
    schema = json.loads(audit.COMPLETENESS_SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(receipt)


def test_success_semantics_rejects_contradictory_aggregate() -> None:
    subject = _subject()
    incomplete = _receipt([subject])
    incomplete["outcome"] = "complete"
    with pytest.raises(audit.AuditBlocked, match="aggregate"):
        audit.validate_receipt_semantics(incomplete, [subject])


def test_symlinked_object_root_blocks_without_path_walk_loop(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    target = real / "states/gfs/model-a/2026050100/state.cfg.ic"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"state")
    with pytest.raises(audit.AuditBlocked, match="unsafe|not a directory"):
        audit._sha256_optional(linked / "states/gfs/model-a/2026050100/state.cfg.ic", linked)


def test_pinned_example_passes_schema_and_runtime_invariants() -> None:
    example = json.loads((Path("schemas/examples/archive_completeness_receipt.example.json")).read_text())
    schema = json.loads(Path("schemas/archive_completeness_receipt.schema.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    audit.validate_receipt_semantics(example)


def test_sql_has_only_one_identity_leading_presence_probe() -> None:
    cases = (
        (audit.FORCING_INVENTORY_SQL, "forcing_version_id", ") fst_presence"),
        (audit.RUN_INVENTORY_SQL, "run_id", ") rt_presence"),
    )
    for sql, identity, presence_alias in cases:
        assert sql.count(f"x.{identity} =") == 1
        assert sql.count("\n  LIMIT 1\n") == 1
        assert sql.count("CROSS JOIN LATERAL (") == 1
        presence_probe = sql.split("CROSS JOIN LATERAL (", maxsplit=1)[1].split(presence_alias, maxsplit=1)[0]
        assert f"x.{identity} =" in presence_probe
        assert "LIMIT 1" in presence_probe
        assert "ORDER BY" not in presence_probe
        assert "valid_time" not in presence_probe
        assert "detail_min" not in sql and "detail_max" not in sql
        assert "EXISTS" not in sql.upper()
        assert "before_window" not in sql and "after_window" not in sql and "identity_drift" not in sql
        assert "ORDER BY x.valid_time" not in sql
        assert "MIN(" not in sql.upper() and "MAX(" not in sql.upper()
        assert "GROUP BY" not in sql.upper()
    assert "JOIN core.model_instance mi ON mi.model_id = fv.model_id" in audit.FORCING_INVENTORY_SQL
    assert "mi.basin_version_id" in audit.FORCING_INVENTORY_SQL
    assert "SELECT x.basin_version_id" not in audit.FORCING_INVENTORY_SQL
    assert (
        "LEFT JOIN hydro.state_snapshot origin ON origin.state_id = ss.cloned_from_state_id"
        in audit.STATE_INVENTORY_SQL
    )


def test_constants_are_fixed() -> None:
    assert audit.STATEMENT_TIMEOUT_MS == 20_000
    assert audit.MAX_MANIFEST_BYTES == 16 * 1024 * 1024
    assert audit.MAX_SALVAGE_MANIFESTS == 10_000
    assert audit.MAX_SALVAGE_ENTRIES == 100_000
    assert audit.MAX_SALVAGE_DEPTH == 8
    assert audit.MAX_SUBJECTS == 100_000
    assert "LIMIT 100001" in audit.FORCING_INVENTORY_SQL
    assert "LIMIT 100001" in audit.RUN_INVENTORY_SQL
    assert "LIMIT 100001" in audit.STATE_INVENTORY_SQL


def test_audit_root_preflight_rejects_symlink_object_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    real = tmp_path / "real-objects"
    real.mkdir()
    config.object_store_root.rmdir()
    config.object_store_root.symlink_to(real, target_is_directory=True)
    with pytest.raises(audit.AuditBlocked, match="unsafe|not a directory"):
        audit._validate_audit_roots(config)


def _args(tmp_path: Path, *, age: int | None) -> argparse.Namespace:
    object_root = tmp_path / "objects-config"
    archive_root = tmp_path / "archive-config"
    object_root.mkdir(exist_ok=True)
    archive_root.mkdir(exist_ok=True)
    return argparse.Namespace(
        database_url="postgresql://redacted",
        object_store_root=str(object_root),
        object_store_prefix="s3://nhms",
        archive_root=str(archive_root),
        archive_min_age_days=age,
        receipt_path=str(tmp_path / "receipt-config.json"),
    )


def test_archive_age_cli_zero_does_not_fall_through_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_MIN_AGE_DAYS", "45")
    with pytest.raises(audit.AuditBlocked, match="at least DB retention"):
        audit.config_from_args(_args(tmp_path, age=0))


def test_archive_age_cli_overrides_env_and_env_below_retention_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_MIN_AGE_DAYS", "0")
    assert audit.config_from_args(_args(tmp_path, age=30)).archive_min_age_days == 30
    with pytest.raises(audit.AuditBlocked, match="at least DB retention"):
        audit.config_from_args(_args(tmp_path, age=None))


def _clone_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "state_id": "clone",
        "model_id": "model-b",
        "run_id": "run-b",
        "source_id": "gfs",
        "valid_time": START,
        "state_uri": "states/gfs/model-a/2026050100/state.cfg.ic",
        "checksum": "a" * 64,
        "cloned_from_state_id": "origin",
        "cloned_from_model_id": "model-a",
        "clone_gate_fingerprint": "f" * 64,
        "origin_state_id": "origin",
        "origin_model_id": "model-a",
        "origin_source_id": "gfs",
        "origin_valid_time": START,
        "origin_state_uri": "states/gfs/model-a/2026050100/state.cfg.ic",
        "origin_checksum": "a" * 64,
    }
    row.update(overrides)
    return row


def test_valid_clone_origin_keeps_clone_stable_subject() -> None:
    _captured, subjects = audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [_clone_row()]]))
    assert subjects[0].stable_key == ("states", "clone")
    assert subjects[0].cloned_from_model_id == "model-a"


@pytest.mark.parametrize(
    ("clone_source", "origin_source"),
    [(None, ""), ("", None)],
)
def test_legacy_clone_null_and_empty_source_are_equivalent(clone_source: object, origin_source: object) -> None:
    row = _clone_row(source_id=clone_source, origin_source_id=origin_source)
    _captured, subjects = audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [row]]))
    assert subjects[0].source_id is None


@pytest.mark.parametrize(
    ("clone_source", "origin_source"),
    [("gfs", None), (None, "gfs")],
)
def test_provider_and_legacy_clone_source_drift_still_blocks(clone_source: object, origin_source: object) -> None:
    row = _clone_row(source_id=clone_source, origin_source_id=origin_source)
    with pytest.raises(audit.AuditBlocked, match="source_id drift"):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [row]]))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"origin_state_id": None}, "does not exist"),
        ({"origin_model_id": "wrong"}, "model_id drift"),
        ({"origin_source_id": None}, "source_id drift"),
        ({"origin_valid_time": END}, "valid_time drift"),
        ({"origin_state_uri": "states/other"}, "state_uri drift"),
        ({"origin_checksum": "b" * 64}, "checksum drift"),
        ({"clone_gate_fingerprint": "F" * 64}, "non-canonical"),
        ({"clone_gate_fingerprint": "f" * 63}, "non-canonical"),
    ],
)
def test_clone_origin_drift_or_invalid_fingerprint_blocks(override: dict[str, object], message: str) -> None:
    with pytest.raises(audit.AuditBlocked, match=message):
        audit.load_inventory(_Connection([[{"audit_time": NOW}], [], [], [_clone_row(**override)]]))


def test_product_and_salvage_mismatch_evidence_survives_precedence() -> None:
    subject = _subject()
    selector_key = audit._canonical(subject.selector)
    receipt = audit.build_receipt(
        [subject],
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage={subject.stable_key: audit.Coverage("none", ("product mismatch",))},
        salvage_selectors=[subject.selector],
        hot_coverage={},
        salvage_mismatches={},
    )
    assert receipt["windows"][0]["coverage"] == "db-export"
    assert "product mismatch" in receipt["windows"][0]["evidence"]
    receipt = audit.build_receipt(
        [subject],
        audit_time=NOW,
        archive_min_age_days=45,
        product_coverage={subject.stable_key: audit.Coverage("product-archive", ("product valid",))},
        salvage_selectors=[],
        hot_coverage={},
        salvage_mismatches={selector_key: "salvage mismatch"},
    )
    assert receipt["windows"][0]["coverage"] == "product-archive"
    assert receipt["windows"][0]["evidence"] == ["salvage mismatch", "product valid"]


def test_run_output_entry_and_depth_caps_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    output = config.object_store_root / "runs/run-a/output"
    output.mkdir(parents=True)
    monkeypatch.setattr(
        audit,
        "_list_run_output_fd",
        lambda _fd, _path, _max: [str(index) for index in range(audit.MAX_RUN_OUTPUT_ENTRIES + 1)],
    )
    with pytest.raises(audit.AuditBlocked, match="entries"):
        audit._directory_has_regular_file(output, config.object_store_root)


def test_run_output_depth_cap_fails_closed_after_inspecting_bounded_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    current = config.object_store_root / "runs/run-a/output"
    current.mkdir(parents=True)
    (current / "valid.csv").write_text("valid", encoding="utf-8")
    for index in range(audit.MAX_RUN_OUTPUT_DEPTH + 1):
        current = current / str(index)
        current.mkdir()
    with pytest.raises(audit.AuditBlocked, match="depth"):
        audit._directory_has_regular_file(config.object_store_root / "runs/run-a/output", config.object_store_root)


def test_run_output_traversal_stays_on_held_directory_tree_during_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    output = config.object_store_root / "runs/run-a/output"
    output.mkdir(parents=True)
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (output / "unsafe").symlink_to(outside, target_is_directory=True)
    original_tree = output.parent / "output-original"
    original_stat = audit._stat_run_output_entry
    swapped = False

    def swap_before_stat(directory_fd: int, name: str, path_label: Path):
        nonlocal swapped
        if not swapped:
            swapped = True
            output.rename(original_tree)
            output.mkdir()
            (output / "safe.csv").write_text("replacement", encoding="utf-8")
        return original_stat(directory_fd, name, path_label)

    monkeypatch.setattr(audit, "_stat_run_output_entry", swap_before_stat)
    with pytest.raises(audit.AuditBlocked, match="unsafe non-regular"):
        audit._directory_has_regular_file(output, config.object_store_root)


def test_missing_leaf_is_absent_but_intermediate_symlink_blocks_each_lane(tmp_path: Path) -> None:
    config = _config(tmp_path)
    forcing = _subject(hot_uri="forcing/gfs/2026050100/basin-a/model-a")
    run = _subject("runs", "run-a")
    state = _subject("states", "state-a")
    assert audit.verify_hot(forcing, config) is None
    assert audit.verify_hot(run, config) is None
    assert audit.verify_hot(state, config) is None
    outside = tmp_path / "outside"
    outside.mkdir()
    for component in ("forcing", "runs", "states"):
        link = config.object_store_root / component
        link.symlink_to(outside, target_is_directory=True)
        subject = {"forcing": forcing, "runs": run, "states": state}[component]
        with pytest.raises(audit.AuditBlocked, match="unsafe|not a directory"):
            audit.verify_hot(subject, config)
        link.unlink()


def test_product_archive_intermediate_symlink_blocks_while_true_missing_is_absent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subject = _subject()
    assert audit.verify_product_archive(subject, config.archive_root) is None
    outside = tmp_path / "outside-product"
    outside.mkdir()
    (config.archive_root / "forcing").symlink_to(outside, target_is_directory=True)
    with pytest.raises(audit.AuditBlocked, match="unsafe|not a directory"):
        audit.verify_product_archive(subject, config.archive_root)


def test_descriptor_bound_json_read_detects_leaf_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root-race"
    root.mkdir()
    target = root / "manifest.json"
    target.write_text('{"version":1}', encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "manifest.json" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target.replace(root / "old.json")
            target.write_text('{"version":2}', encoding="utf-8")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_open)
    with pytest.raises(audit.AuditBlocked, match="changed while being opened"):
        audit._read_json(target, root)


# ---------------------------------------------------------------------------
# #849 Invariant Matrix Regression row 1: the audit `_once.sh` wrapper reuses
# the same mode-0600 env-file check and absolute-path gates inherited from the
# mover wrapper (see openspec/changes/tier-node27-timeseries-storage/design.md
# under "Workflow Fixture: Issue #849"). The sibling mover-side coverage lives
# at tests/test_node27_product_archive.py
# ::test_product_archive_wrapper_rejects_unsafe_runtime_contract; this test
# mirrors that structure for the audit-side wrapper's 5 shell-level gates.


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("relative-wrapper-path", "wrapper paths must be absolute"),
        ("env-mode", "env file must have mode 0600"),
        ("env-symlink", "env file must be a regular non-symlink file"),
        ("missing-python", "python executable is unavailable"),
        ("missing-script", "audit entrypoint is unavailable or a symlink"),
        ("symlink-script", "audit entrypoint is unavailable or a symlink"),
    ],
)
def test_storage_inventory_audit_wrapper_rejects_unsafe_runtime_contract(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    wrapper = _ROOT / "scripts/node27_storage_inventory_audit_once.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stat_shim = bin_dir / "stat"
    stat_shim.write_text(
        "#!/bin/sh\n"
        "for last do :; done\n"
        "case \"$last\" in\n"
        "  *bad-mode.env) printf '644\\n' ;;\n"
        "  *) printf '600\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stat_shim.chmod(0o700)

    python_bin = tmp_path / "python"
    python_bin.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python_bin.chmod(0o700)
    entrypoint = tmp_path / "audit.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")

    env_file = tmp_path / ("bad-mode.env" if case == "env-mode" else "audit.env")
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    if case == "env-symlink":
        target = tmp_path / "real.env"
        env_file.rename(target)
        env_file.symlink_to(target)

    configured_python = str(python_bin)
    if case == "missing-python":
        configured_python = str(tmp_path / "missing-python")

    configured_script = str(entrypoint)
    if case == "missing-script":
        configured_script = str(tmp_path / "missing-script.py")
    elif case == "symlink-script":
        script_link = tmp_path / "audit-link.py"
        script_link.symlink_to(entrypoint)
        configured_script = str(script_link)

    process_env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE": (
            "relative.env" if case == "relative-wrapper-path" else str(env_file)
        ),
        "NODE27_STORAGE_INVENTORY_AUDIT_PYTHON": configured_python,
        "NODE27_STORAGE_INVENTORY_AUDIT_SCRIPT": configured_script,
    }
    result = subprocess.run(
        ["/bin/sh", str(wrapper)],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    failure = json.loads(result.stderr.strip())
    assert failure == {"status": "failed", "reason": expected_reason}
