from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from packages.common.safe_fs import SafeFilesystemError

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("node27_product_archive", _ROOT / "scripts/node27_product_archive.py")
assert _SPEC and _SPEC.loader
archive = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = archive
_SPEC.loader.exec_module(archive)


def _mount_id(fd: int) -> int:
    return os.fstat(fd).st_dev


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    with pytest.raises(FileNotFoundError):
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def _tool(tmp_path: Path) -> Path:
    path = tmp_path / "fake-zstd"
    path.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  '-q -c'|'-q -d -c') cat\n"
        "  ;;\n"
        "  *) echo 'unexpected arguments' >&2; exit 64\n"
        "  ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _config(tmp_path: Path, *, enforce: bool, bound: int = 10) -> archive.MoverConfig:
    store = tmp_path / "object-store"
    store.mkdir(exist_ok=True)
    (tmp_path / "archive").mkdir(exist_ok=True)
    return archive.MoverConfig(
        object_store_root=store,
        archive_root=tmp_path / "archive",
        receipt_path=tmp_path / "logs" / "receipt.json",
        lock_path=tmp_path / "locks" / "archive.lock",
        zstd_path=_tool(tmp_path),
        minimum_age_days=45,
        per_tick_bound=bound,
        enforce=enforce,
    )


def test_compressor_protocol_uses_stdin_only_and_restores_input_offset(tmp_path: Path) -> None:
    payload = b"same-opened-inode\x00payload"
    source = tmp_path / "source.tar"
    output = tmp_path / "archive.tar.zst"
    source.write_bytes(payload)
    source_fd = os.open(source, os.O_RDONLY)
    output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.lseek(source_fd, 7, os.SEEK_SET)
        archive._run_tool(
            [str(_tool(tmp_path)), "-q", "-c"],
            input_fd=source_fd,
            stdout_fd=output_fd,
            max_output_bytes=len(payload),
        )
        assert os.lseek(source_fd, 0, os.SEEK_CUR) == 7
    finally:
        os.close(output_fd)
        os.close(source_fd)
    assert output.read_bytes() == payload


def _forcing(config: archive.MoverConfig, cycle: str = "2026010100") -> Path:
    leaf = config.object_store_root / f"forcing/gfs/{cycle}/basin-a/model-a"
    leaf.mkdir(parents=True)
    (leaf / "payload.csv").write_text("time,value\n1,2\n", encoding="utf-8")
    (leaf / "forcing_package.json").write_text(
        json.dumps(
            {
                "source_id": "gfs",
                "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "basin_version_id": "basin-a",
                "model_id": "model-a",
            }
        ),
        encoding="utf-8",
    )
    return leaf


def _run(config: archive.MoverConfig, run_id: str = "opaque-run") -> Path:
    leaf = config.object_store_root / f"runs/{run_id}"
    (leaf / "input").mkdir(parents=True)
    (leaf / "output").mkdir()
    (leaf / "output/result.nc").write_bytes(b"netcdf")
    (leaf / "input/manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "source_id": "ERA5",
                "cycle_time": "2026-01-02T00:00:00Z",
                "start_time": "2026-01-02T00:00:00Z",
                "end_time": "2026-01-03T00:00:00Z",
                "model": {"model_id": "model-b", "basin_version_id": "basin-b"},
                "outputs": {
                    "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
                    "output_uri": f"s3://nhms/runs/{run_id}/output",
                },
            }
        ),
        encoding="utf-8",
    )
    return leaf


def _state(config: archive.MoverConfig, *, provider: bool) -> Path:
    relative = "states/IFS/model-c/2026010300" if provider else "states/model-c/2026010300"
    leaf = config.object_store_root / relative
    leaf.mkdir(parents=True)
    (leaf / "state.cfg.ic").write_bytes(b"state")
    return leaf


def test_enforce_archives_three_physical_lanes_and_retires_sources(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    sources = [_forcing(config), _run(config), _state(config, provider=True), _state(config, provider=False)]
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["outcome"] == "success"
    assert [item["status"] for item in receipt["terminals"]] == ["archived"] * 4
    assert not any(path.exists() for path in sources)
    assert {item["identity"]["source"] for item in receipt["terminals"]} == {
        "gfs",
        "ERA5",
        "IFS",
        "legacy-unqualified",
    }
    for candidate in receipt["candidates"]:
        leaf = config.archive_root / Path(candidate["archive_path"]).parent
        archive.verify_archive_pair(
            leaf,
            config.archive_root,
            zstd_path=config.zstd_path,
            mount_id_provider=_mount_id,
        )
    assert config.receipt_path.stat().st_mode & 0o777 == 0o600


def test_dry_run_is_bounded_and_does_not_mutate_products(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False, bound=1)
    first = _forcing(config, "2026010100")
    second = _forcing(config, "2026010200")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert len(receipt["selected"]) == 1
    assert len(receipt["deferred"]) == 1
    assert receipt["terminals"][0]["status"] == "planned"
    assert first.exists() and second.exists()
    assert list(config.archive_root.iterdir()) == []


def test_cutoff_equality_is_not_eligible(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config, "2026052700")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert failures == []


def test_cutoff_comparison_preserves_captured_now_microseconds(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config, "2026052700")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, 0, 0, 0, 1, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert len(candidates) == 1
    assert failures == []


def test_malformed_sibling_is_failure_but_valid_leaf_remains_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    bad = config.object_store_root / "forcing/gfs/2026010100/basin-a/model-b"
    bad.mkdir(parents=True)
    (bad / "forcing_package.json").write_text("{}", encoding="utf-8")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert len(receipt["candidates"]) == 1
    assert receipt["discovery_failures"][0]["locator"].endswith("model-b")


def test_run_duplicate_identity_drift_is_discovery_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    manifest_path = leaf / "input/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["identity"] = {"run_id": "different"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["candidates"] == []
    assert "duplicated identity drift" in receipt["discovery_failures"][0]["reason"]


@pytest.mark.parametrize("field", ["source", "source_id"])
def test_run_duplicate_source_alias_drift_is_discovery_failure(tmp_path: Path, field: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    manifest_path = leaf / "input/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["identity"] = {field: "IFS"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["candidates"] == []
    assert f"duplicated identity drift: {field}" in receipt["discovery_failures"][0]["reason"]


def test_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config)
    (leaf / "unsafe").symlink_to(leaf / "payload.csv")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert "unsupported product entry type" in failures[0].reason
    (leaf / "unsafe").unlink()
    os.link(leaf / "payload.csv", leaf / "hardlink")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert "hard-linked" in failures[0].reason


def test_mount_id_mismatch_is_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    calls = 0

    def mismatch(fd: int) -> int:
        nonlocal calls
        calls += 1
        return os.fstat(fd).st_dev + (1 if calls > 2 else 0)

    _candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=mismatch,
    )
    assert failures
    assert "mount" in failures[0].reason


def test_existing_verified_archive_is_idempotently_retired(tmp_path: Path) -> None:
    enforce = _config(tmp_path, enforce=True)
    source = _forcing(enforce)
    receipt, code = archive.run(
        enforce,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    # Restore an exact source copy from fixture content, leaving verified final in place.
    source.mkdir(parents=True)
    (source / "payload.csv").write_text("time,value\n1,2\n", encoding="utf-8")
    manifest_member = next(item for item in receipt["candidates"][0:1])
    del manifest_member  # receipt identity is not used to reconstruct content
    (source / "forcing_package.json").write_text(
        json.dumps(
            {
                "source_id": "gfs",
                "cycle_time": "2026-01-01T00:00:00Z",
                "basin_version_id": "basin-a",
                "model_id": "model-a",
            }
        ),
        encoding="utf-8",
    )
    second, code = archive.run(
        enforce,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert second["terminals"][0]["status"] == "retired-from-existing"
    assert not source.exists()


def test_existing_verified_archive_is_not_quarantined_when_source_retirement_fails(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    source.mkdir(parents=True)
    (source / "payload.csv").write_text("time,value\n1,2\n", encoding="utf-8")
    (source / "forcing_package.json").write_text(
        json.dumps(
            {
                "source_id": "gfs",
                "cycle_time": "2026-01-01T00:00:00Z",
                "basin_version_id": "basin-a",
                "model_id": "model-a",
            }
        ),
        encoding="utf-8",
    )

    def mutate_at_retirement(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        if dst.startswith(".archive-delete-"):
            leaf_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
            try:
                file_fd = os.open("payload.csv", os.O_WRONLY | os.O_APPEND, dir_fd=leaf_fd)
                try:
                    os.write(file_fd, b"late")
                finally:
                    os.close(file_fd)
            finally:
                os.close(leaf_fd)
        _rename_noreplace(src_fd, src, dst_fd, dst)

    second, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=mutate_at_retirement,
    )
    assert code == 1
    assert second["terminals"][0]["status"] == "failed"
    assert all(event["kind"] != "quarantined" for event in second["events"])
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    archive.verify_archive_pair(
        final,
        config.archive_root,
        zstd_path=config.zstd_path,
        mount_id_provider=_mount_id,
    )


def test_corrupt_final_is_planned_for_quarantine_in_dry_run(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
    final.mkdir(parents=True)
    (final / "archive.tar.zst").write_bytes(b"bad")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["events"][0]["kind"] == "would-quarantine"
    assert (final / "archive.tar.zst").read_bytes() == b"bad"


def test_corrupt_final_is_quarantined_then_replaced_in_enforce(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
    final.mkdir(parents=True)
    (final / "unexpected").write_bytes(b"bad")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["events"][0]["kind"] == "quarantined"
    assert receipt["terminals"][0]["status"] == "archived"
    assert not source.exists()
    archive.verify_archive_pair(
        final,
        config.archive_root,
        zstd_path=config.zstd_path,
        mount_id_provider=_mount_id,
    )


def test_symlink_final_is_not_quarantined_or_followed(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep", encoding="utf-8")
    final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
    final.parent.mkdir(parents=True)
    final.symlink_to(outside, target_is_directory=True)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "failed"
    assert receipt["events"] == []
    assert source.exists()
    assert final.is_symlink()
    assert (outside / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unsafe", "nonregular", "checksum"])
def test_internal_tar_verification_rejects_invalid_member_even_with_matching_outer_checksum(
    tmp_path: Path, fault: str
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    manifest_path = final / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["files"]
    payloads = {entry["path"]: b"x" * entry["size_bytes"] for entry in entries}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        if fault != "missing":
            for index, entry in enumerate(entries):
                name = "../escape" if fault == "unsafe" and index == 0 else entry["path"]
                info = tarfile.TarInfo(name)
                if fault == "nonregular" and index == 0:
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    tar.addfile(info)
                    continue
                content = payloads[entry["path"]]
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
                if fault == "duplicate" and index == 0:
                    tar.addfile(info, io.BytesIO(content))
    archive_raw = buffer.getvalue()
    (final / "archive.tar.zst").write_bytes(archive_raw)
    manifest["archive"]["size_bytes"] = len(archive_raw)
    manifest["archive"]["sha256"] = hashlib.sha256(archive_raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    expected = "tar member differs" if fault == "checksum" else "tar member set differs"
    if fault in {"duplicate", "unsafe", "nonregular"}:
        expected = "unsafe/duplicate/non-regular"
    with pytest.raises(archive.ArchiveMoverError, match=expected):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            mount_id_provider=_mount_id,
        )


def test_archive_leaf_mount_id_mismatch_fails_verification(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    root_inode = config.archive_root.stat().st_ino

    def mismatch(fd: int) -> int:
        info = os.fstat(fd)
        return info.st_dev if info.st_ino == root_inode else info.st_dev + 1

    with pytest.raises(archive.ArchiveMoverError, match="different device/mount"):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            mount_id_provider=mismatch,
        )


def test_existing_archive_manifest_resource_bounds_are_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    monkeypatch.setattr(archive, "MAX_TREE_ENTRIES", 1)
    with pytest.raises(archive.ArchiveMoverError, match="manifest exceeds 1 file entries"):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            mount_id_provider=_mount_id,
        )


def test_tombstone_recheck_preserves_late_write_and_reports_residue(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)

    def mutate_at_source_rename(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        if src == "model-a" and dst.startswith(".archive-delete-"):
            leaf_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
            try:
                file_fd = os.open(
                    "payload.csv",
                    os.O_WRONLY | os.O_APPEND,
                    dir_fd=leaf_fd,
                )
                try:
                    os.write(file_fd, b"late")
                finally:
                    os.close(file_fd)
            finally:
                os.close(leaf_fd)
        _rename_noreplace(src_fd, src, dst_fd, dst)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=mutate_at_source_rename,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "failed"
    assert terminal["residue"] and ".archive-delete-" in terminal["residue"][0]
    assert not source.exists()
    assert (config.object_store_root / terminal["residue"][0]).exists()


def test_tombstone_removal_refuses_cross_mount_descendant(tmp_path: Path) -> None:
    root = tmp_path / "tombstone"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "payload").write_bytes(b"keep")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_info = os.fstat(root_fd)

        def mismatch(fd: int) -> int:
            info = os.fstat(fd)
            return info.st_dev if info.st_ino == root_info.st_ino else info.st_dev + 1

        with pytest.raises(archive.ArchiveMoverError, match="cross-mount directory rejected"):
            archive._remove_tree_contents_fd(
                root_fd,
                "tombstone",
                device=root_info.st_dev,
                mount_id=root_info.st_dev,
                mount_id_provider=mismatch,
            )
    finally:
        os.close(root_fd)
    assert (child / "payload").read_bytes() == b"keep"


def test_raced_publish_destination_is_not_overwritten_and_source_survives(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)

    def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        if src != "model-a":
            raise OSError(17, "destination appeared")
        _rename_noreplace(src_fd, src, dst_fd, dst)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=race,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "failed"
    assert source.exists()


@pytest.mark.parametrize(
    ("constant", "limit", "reason"),
    [
        ("MAX_TREE_ENTRIES", 1, "tree exceeds"),
        ("MAX_MANIFEST_BYTES", 8, "manifest exceeds"),
        ("MAX_SOURCE_BYTES", 8, "source bytes"),
    ],
)
def test_source_resource_caps_become_locator_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    reason: str,
) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    monkeypatch.setattr(archive, constant, limit)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["candidates"] == []
    assert reason in receipt["discovery_failures"][0]["reason"]


def test_discovery_cap_stops_additional_valid_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, enforce=False, bound=1)
    _forcing(config, "2026010100")
    _forcing(config, "2026010200")
    monkeypatch.setattr(archive, "MAX_DISCOVERY", 1)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["candidates"] == []
    assert "exceeds" in receipt["discovery_failures"][0]["reason"]


def test_global_discovery_cap_failure_defers_known_candidates_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert len(candidates) == 1 and failures == []
    monkeypatch.setattr(
        archive,
        "discover_candidates",
        lambda *_args, **_kwargs: (
            candidates,
            [archive.DiscoveryFailure("forcing", "forcing", "discovery exceeds 100000 candidates/failures")],
        ),
    )
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["selected"] == []
    assert receipt["deferred"] == receipt["candidates"]
    assert receipt["terminals"] == []
    assert source.exists()
    assert list(config.archive_root.iterdir()) == []


def test_oversized_sparse_source_file_is_rejected_without_reading_payload(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config)
    with (leaf / "payload.csv").open("r+b") as stream:
        stream.truncate(archive.MAX_FILE_BYTES + 1)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert "product file exceeds" in receipt["discovery_failures"][0]["reason"]


def test_relative_zstd_is_preflight_blocker(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    config = archive.MoverConfig(**{**config.__dict__, "zstd_path": Path("zstd")})
    with pytest.raises(archive.ArchiveMoverError, match="must be absolute"):
        archive.run(config, mount_id_provider=_mount_id, rename_impl=_rename_noreplace)


def test_receipt_pre_replace_failure_preserves_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    config.receipt_path.parent.mkdir()
    config.receipt_path.write_text("old\n", encoding="utf-8")

    def fail_before_replace(*_args, **_kwargs):
        raise SafeFilesystemError("injected", kind="io")

    monkeypatch.setattr(archive, "atomic_write_bytes_no_follow", fail_before_replace)
    with pytest.raises(archive.ArchiveMoverError, match="publication failed"):
        archive.run(
            config,
            now=datetime(2026, 7, 11, tzinfo=UTC),
            mount_id_provider=_mount_id,
            rename_impl=_rename_noreplace,
        )
    assert config.receipt_path.read_text() == "old\n"


def test_lock_contention_does_not_touch_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock = tmp_path / "lock"
    first = archive.acquire_lock(lock)
    assert first is not None
    try:
        assert archive.acquire_lock(lock) is None
    finally:
        os.close(first)
    assert not (tmp_path / "receipt.json").exists()
    assert capsys.readouterr().err == ""


def test_main_lock_contender_emits_one_diagnostic_and_preserves_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path, enforce=False)
    config.receipt_path.parent.mkdir()
    config.receipt_path.write_text("old\n", encoding="utf-8")
    holder = archive.acquire_lock(config.lock_path)
    assert holder is not None
    try:
        code = archive.main(
            [
                "--object-store-root",
                str(config.object_store_root),
                "--archive-root",
                str(config.archive_root),
                "--receipt",
                str(config.receipt_path),
                "--lock-file",
                str(config.lock_path),
                "--zstd",
                str(config.zstd_path),
            ]
        )
    finally:
        os.close(holder)
    assert code == 0
    assert json.loads(capsys.readouterr().err) == {
        "status": "skipped",
        "reason": "lock-contended",
    }
    assert config.receipt_path.read_text() == "old\n"


@pytest.mark.parametrize("age", [0, 20, 29])
def test_invalid_minimum_age_never_falls_back(tmp_path: Path, age: int) -> None:
    config = _config(tmp_path, enforce=False)
    config = archive.MoverConfig(**{**config.__dict__, "minimum_age_days": age})
    with pytest.raises(archive.ArchiveMoverError, match="at least 30"):
        archive.run(config, mount_id_provider=_mount_id, rename_impl=_rename_noreplace)


def test_receipt_schema_positive_and_negative() -> None:
    schema = json.loads((_ROOT / "schemas/product_archive_receipt.schema.json").read_text())
    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    broken = dict(example)
    broken.pop("terminals")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(schema).validate(broken)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"skipped": []}),
        lambda value: value["bytes"].update({"source": -1}),
        lambda value: value.update({"outcome": "unknown"}),
        lambda value: value["discovery_failures"].append({"lane_hint": "runs", "locator": "../runs", "reason": "bad"}),
        lambda value: value["candidates"][0]["identity"].update({"cycle_time": "2026-99-99T00:00:00Z"}),
        lambda value: value["terminals"][0].update({"reason": ""}),
        lambda value: value["events"][0].update({"detail": ""}),
    ],
)
def test_receipt_schema_rejects_legacy_arrays_negative_bytes_and_unsafe_locator(mutate) -> None:
    schema = json.loads((_ROOT / "schemas/product_archive_receipt.schema.json").read_text())
    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    mutate(example)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)


def test_receipt_semantic_partition_and_terminal_bijection() -> None:
    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    archive.validate_receipt_semantics(example)
    candidate = {
        "identity": {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026010100",
            "cycle_time": "2026-01-01T00:00:00Z",
            "run_id": "r",
        },
        "source_path": "runs/r",
        "archive_path": "runs/gfs/2026010100/r/archive.tar.zst",
        "source_bytes": 1,
    }
    example["candidates"] = [candidate]
    with pytest.raises(archive.ArchiveMoverError, match="partition"):
        archive.validate_receipt_semantics(example)


def test_manifest_identity_read_is_bound_to_scanned_inode_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config)
    original = archive.scan_tree_snapshot
    changed = False

    def mutate_before_snapshot(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            manifest_path = leaf / "forcing_package.json"
            value = json.loads(manifest_path.read_text())
            value["model_id"] = "model-b"
            manifest_path.write_text(json.dumps(value), encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(archive, "scan_tree_snapshot", mutate_before_snapshot)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1
    assert "manifest changed between identity read and tree snapshot" in failures[0].reason


def test_noncanonical_provider_state_segment_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = config.object_store_root / "states/GFS/model-c/2026010300"
    leaf.mkdir(parents=True)
    (leaf / "state.cfg.ic").write_bytes(b"state")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1
    assert "canonical source ID" in failures[0].reason


def test_selected_source_disappearance_becomes_terminal_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert failures == [] and len(candidates) == 1
    for child in source.iterdir():
        child.unlink()
    source.rmdir()
    terminal, events = archive.process_candidate(
        candidates[0],
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert terminal["status"] == "failed"
    assert events == []


def test_receipt_target_is_preflighted_before_enforce_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    config.receipt_path.parent.mkdir()
    target = tmp_path / "outside-receipt"
    target.write_text("old\n", encoding="utf-8")
    config.receipt_path.symlink_to(target)
    with pytest.raises(archive.ArchiveMoverError, match="receipt target preflight"):
        archive.run(
            config,
            now=datetime(2026, 7, 11, tzinfo=UTC),
            mount_id_provider=_mount_id,
            rename_impl=_rename_noreplace,
        )
    assert source.exists()
    assert target.read_text() == "old\n"
    assert list(config.archive_root.iterdir()) == []


def test_leaf_rename_detects_source_replacement_race(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "source"
    destination = root / "destination"
    source.mkdir(parents=True)
    (source / "original").write_text("original", encoding="utf-8")

    def swap_then_rename(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        os.rename(src, "displaced", src_dir_fd=src_fd, dst_dir_fd=src_fd)
        os.mkdir(src, dir_fd=src_fd)
        replacement_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
        try:
            file_fd = os.open("replacement", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=replacement_fd)
            os.close(file_fd)
        finally:
            os.close(replacement_fd)
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)

    with pytest.raises(archive.ArchiveMoverError, match="destination identity is indeterminate") as caught:
        archive._rename_leaf(source, destination, root, swap_then_rename, _mount_id)
    assert caught.value.indeterminate
    assert (root / "displaced/original").read_text() == "original"


def test_receipt_semantics_bind_exact_source_and_unique_failure_locator() -> None:
    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    candidate = {
        "identity": {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026010100",
            "cycle_time": "2026-01-01T00:00:00Z",
            "run_id": "r",
        },
        "source_path": "runs/not-r",
        "archive_path": "runs/gfs/2026010100/r/archive.tar.zst",
        "source_bytes": 1,
    }
    example.update(
        {
                "candidates": [candidate],
                "selected": [candidate],
                "deferred": [],
            "terminals": [
                {
                    "identity": candidate["identity"],
                    "status": "planned",
                    "reason": "planned",
                    "source_bytes": 1,
                    "archive_bytes": 0,
                    "residue": [],
                }
                ],
                "events": [],
                "discovery_failures": [],
                "outcome": "success",
                "bytes": {"source": 1, "archived": 0},
        }
    )
    with pytest.raises(archive.ArchiveMoverError, match="source path does not bind"):
        archive.validate_receipt_semantics(example)

    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    duplicate = {"lane_hint": "runs", "locator": "runs/bad", "reason": "first"}
    example["discovery_failures"] = [duplicate, {**duplicate, "reason": "second"}]
    example["outcome"] = "failed"
    with pytest.raises(archive.ArchiveMoverError, match="unique by lane/locator"):
        archive.validate_receipt_semantics(example)
