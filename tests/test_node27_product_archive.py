from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from packages.common.safe_fs import SafeFilesystemError

_ROOT = Path(__file__).resolve().parents[1]
_LIVE_SHAPE = _ROOT / "tests/fixtures/node27_product_archive/live-shape/object-store"
_LIVE_SHAPE_PROVENANCE = _LIVE_SHAPE.parent / "provenance.json"
_LIVE_SHAPE_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_SPEC = importlib.util.spec_from_file_location("node27_product_archive", _ROOT / "scripts/node27_product_archive.py")
assert _SPEC and _SPEC.loader
archive = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = archive
_SPEC.loader.exec_module(archive)

_MISSING = object()


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
        object_store_prefix="s3://nhms",
        archive_root=tmp_path / "archive",
        receipt_path=tmp_path / "logs" / "receipt.json",
        lock_path=tmp_path / "locks" / "archive.lock",
        zstd_path=_tool(tmp_path),
        minimum_age_days=45,
        per_tick_bound=bound,
        enforce=enforce,
    )


def _live_shape_config(
    tmp_path: Path,
    *,
    enforce: bool = False,
    object_store_prefix: str = "s3://nhms",
) -> archive.MoverConfig:
    store = tmp_path / "object-store"
    shutil.copytree(_LIVE_SHAPE, store)
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    return archive.MoverConfig(
        object_store_root=store,
        object_store_prefix=object_store_prefix,
        archive_root=archive_root,
        receipt_path=tmp_path / "logs" / "receipt.json",
        lock_path=tmp_path / "locks" / "archive.lock",
        zstd_path=_tool(tmp_path),
        minimum_age_days=45,
        per_tick_bound=10,
        enforce=enforce,
    )


def _tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        kind = "directory" if path.is_dir() else "file"
        snapshot.append(
            (
                path.relative_to(root).as_posix(),
                kind,
                path.read_bytes() if kind == "file" else None,
            )
        )
    return snapshot


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


@pytest.mark.parametrize(
    ("case", "body", "reason"),
    [
        ("timeout", "exec sleep 5\n", "timed out"),
        ("output", "printf '0123456789abcdef'\n", "output exceeds"),
        ("stderr", "printf '0123456789abcdef' >&2\n", "stderr exceeds"),
    ],
)
def test_compressor_resource_failures_reap_process_and_restore_input_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    body: str,
    reason: str,
) -> None:
    tool = tmp_path / f"compressor-{case}"
    tool.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    tool.chmod(0o700)
    source = tmp_path / "source.tar"
    output = tmp_path / "archive.tar.zst"
    source.write_bytes(b"input-data")
    source_fd = os.open(source, os.O_RDONLY)
    output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(archive, "TOOL_TIMEOUT_SECONDS", 0.1 if case == "timeout" else 2)
    monkeypatch.setattr(archive, "MAX_STDERR_BYTES", 8)
    try:
        os.lseek(source_fd, 3, os.SEEK_SET)
        with pytest.raises(archive.ArchiveMoverError, match=reason):
            archive._run_tool(
                [str(tool)],
                input_fd=source_fd,
                stdout_fd=output_fd,
                max_output_bytes=8,
            )
        assert os.lseek(source_fd, 0, os.SEEK_CUR) == 3
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        os.close(output_fd)
        os.close(source_fd)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("timeout", "timed out"),
        ("stderr", "stderr exceeds"),
        ("tar-cap", "decompressed tar exceeds"),
    ],
)
def test_decompressor_resource_failures_reap_process_and_restore_archive_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    tar_path = tmp_path / "payload.tar"
    with tarfile.open(tar_path, mode="w") as payload:
        info = tarfile.TarInfo("payload.bin")
        info.size = 32
        payload.addfile(info, io.BytesIO(b"x" * info.size))
    tool = tmp_path / f"decompressor-{case}"
    if case == "timeout":
        body = "exec sleep 5\n"
    elif case == "stderr":
        body = f"printf '0123456789abcdef' >&2\nexec /bin/cat {shlex.quote(str(tar_path))}\n"
    else:
        body = f"exec /bin/cat {shlex.quote(str(tar_path))}\n"
    tool.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    tool.chmod(0o700)
    compressed = tmp_path / "archive.tar.zst"
    compressed.write_bytes(b"input")
    archive_fd = os.open(compressed, os.O_RDONLY)
    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(archive, "TOOL_TIMEOUT_SECONDS", 0.1 if case == "timeout" else 2)
    monkeypatch.setattr(archive, "MAX_STDERR_BYTES", 8)
    if case == "tar-cap":
        monkeypatch.setattr(archive, "MAX_TAR_BYTES", 512)
    try:
        os.lseek(archive_fd, 2, os.SEEK_SET)
        expected = archive.ArchiveOperationalError if case != "tar-cap" else archive.ArchiveMoverError
        with pytest.raises(expected, match=reason):
            with archive._TarStreamContext(archive_fd, tool) as payload:
                list(payload)
        assert os.lseek(archive_fd, 0, os.SEEK_CUR) == 2
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        os.close(archive_fd)


@pytest.mark.parametrize("evidence", ["missing", "invalid", "unavailable"])
def test_fd_mount_id_failures_are_operational(
    monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    if evidence == "missing":
        def fail_read(self, **kwargs):
            raise OSError("fdinfo unavailable")

        monkeypatch.setattr(Path, "read_text", fail_read)
    elif evidence == "invalid":
        monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "mnt_id:not-a-number\n")
    else:
        monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "pos: 0\n")
    with pytest.raises(archive.ArchiveOperationalError):
        archive.fd_mount_id(123)


def test_tar_constructor_parser_failure_kills_live_decompressor_and_restores_offset(tmp_path: Path) -> None:
    tool = tmp_path / "bad-zstd"
    tool.write_text(
        "#!/bin/sh\nwhile :; do printf 'not-a-valid-tar-block-not-a-valid-tar-block'; done\n",
        encoding="utf-8",
    )
    tool.chmod(0o700)
    source = tmp_path / "archive.zst"
    source.write_bytes(b"input")
    fd = os.open(source, os.O_RDONLY)
    try:
        os.lseek(fd, 2, os.SEEK_SET)
        started = time.monotonic()
        with pytest.raises(archive.ArchiveMoverError, match="invalid size encoding"):
            archive._TarStreamContext(fd, tool)
        assert time.monotonic() - started < 2
        assert os.lseek(fd, 0, os.SEEK_CUR) == 2
    finally:
        os.close(fd)


def _forcing(config: archive.MoverConfig, cycle: str = "2026010100") -> Path:
    leaf = config.object_store_root / f"forcing/gfs/{cycle}/basin-a/model-a"
    leaf.mkdir(parents=True)
    payload = b"time,value\n1,2\n"
    (leaf / "payload.csv").write_bytes(payload)
    (leaf / "forcing_package.json").write_text(
        json.dumps(
            {
                "forcing_version_id": f"forc_gfs_{cycle}_model-a",
                "source_id": "gfs",
                "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "start_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "end_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "basin_version_id": "basin-a",
                "model_id": "model-a",
                "files": [
                    {
                        "role": "shud_forcing_csv",
                        "relative_path": "payload.csv",
                        "uri": f"s3://nhms/forcing/gfs/{cycle}/basin-a/model-a/payload.csv",
                        "checksum": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return leaf


def _forcing_with_domain_bundle(config: archive.MoverConfig, *, top_level_basin: object = _MISSING) -> Path:
    cycle = "2026010100"
    leaf = config.object_store_root / f"forcing/ifs/{cycle}/basin-a/model-a"
    (leaf / "payloads").mkdir(parents=True)
    product = b"time,value\n1,2\n"
    (leaf / "payload.csv").write_bytes(product)
    forcing = {
        "forcing_version_id": "forc_ifs_2026010100_model-a",
        "source_id": "IFS",
        "cycle_time": "2026-01-01T00:00:00Z",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-02T00:00:00Z",
        "basin_version_id": "basin-a",
        "model_id": "model-a",
        "files": [
            {
                "uri": "s3://nhms/forcing/ifs/2026010100/basin-a/model-a/payload.csv",
                "checksum": hashlib.sha256(product).hexdigest(),
            }
        ],
    }
    forcing_raw = json.dumps(forcing).encode()
    (leaf / "forcing_package.json").write_bytes(forcing_raw)
    payload_specs = {
        "station_inventory": ("station_inventory.json", "met.met_station"),
        "station_timeseries": ("station_timeseries.json", "met.forcing_station_timeseries"),
        "interpolation_weights": ("interp_weights.json", "met.interp_weight"),
    }
    payloads = {}
    for role, (name, table) in payload_specs.items():
        raw = json.dumps([{"id": role}]).encode()
        (leaf / "payloads" / name).write_bytes(raw)
        payloads[role] = {
            "uri": f"s3://nhms/forcing/ifs/{cycle}/basin-a/model-a/payloads/{name}",
            "checksum_sha256": hashlib.sha256(raw).hexdigest(),
            "table": table,
            "row_count": 1,
            "content_type": "application/json",
        }
    domain = {
        "schema_version": "1.0",
        "contract_id": "nhms.forcing_domain_handoff.package.v1",
        "run_id": "fcst_ifs_2026010100_model-a",
        "source_id": "IFS",
        "source": "ifs",
        "cycle_time": "2026-01-01T00:00:00Z",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T21:00:00Z",
        "model_id": "model-a",
        "basin_id": "basin-a",
        "basin_version_id": "basin-a",
        "forcing_version_id": forcing["forcing_version_id"],
        "station_count": 1,
        "payloads": payloads,
        "table_row_counts": {
            "met.forcing_version": 1,
            "met.met_station": 1,
            "met.forcing_station_timeseries": 1,
            "met.interp_weight": 1,
        },
    }
    (leaf / "forcing_domain_package.json").write_text(json.dumps(domain), encoding="utf-8")
    version = {
        **{field: forcing[field] for field in (
            "forcing_version_id", "source_id", "cycle_time", "start_time", "end_time", "model_id"
        )},
        "forcing_package_uri": "s3://nhms/forcing/ifs/2026010100/basin-a/model-a",
        "checksum": hashlib.sha256(forcing_raw).hexdigest(),
        "lineage_json": {
            "basin_version_id": forcing["basin_version_id"],
            "forcing_package_manifest_uri": (
                "s3://nhms/forcing/ifs/2026010100/basin-a/model-a/forcing_package.json"
            ),
            "forcing_package_manifest_checksum": hashlib.sha256(forcing_raw).hexdigest(),
        },
    }
    if top_level_basin is not _MISSING:
        version["basin_version_id"] = top_level_basin
    (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    return leaf


@pytest.mark.parametrize("top_level_basin", [_MISSING, None], ids=["top-basin-absent", "top-basin-null"])
def test_complete_forcing_domain_bundle_accepts_uppercase_ifs_and_shorter_domain_window(
    tmp_path: Path, top_level_basin: object
) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing_with_domain_bundle(config, top_level_basin=top_level_basin)
    candidates, failures = archive.discover_candidates(
        config, now=datetime(2026, 7, 11, tzinfo=UTC), mount_id_provider=_mount_id
    )
    assert failures == []
    assert len(candidates) == 1
    assert candidates[0].identity.source == "IFS"
    assert candidates[0].eligibility_end == datetime(2026, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "extra",
        "payload-checksum",
        "version-lineage",
        "version-lineage-checksum",
        "version-lineage-basin",
        "version-top-basin",
        "version-top-package-uri",
        "version-top-package-checksum",
        "identity",
    ],
)
def test_forcing_domain_bundle_drift_fails_discovery(tmp_path: Path, mutation: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing_with_domain_bundle(config)
    if mutation == "partial":
        (leaf / "forcing_version_record.json").unlink()
    elif mutation == "extra":
        (leaf / "unexpected.json").write_text("{}", encoding="utf-8")
    elif mutation == "payload-checksum":
        package = json.loads((leaf / "forcing_domain_package.json").read_text())
        package["payloads"]["station_inventory"]["checksum_sha256"] = "0" * 64
        (leaf / "forcing_domain_package.json").write_text(json.dumps(package), encoding="utf-8")
    elif mutation == "version-lineage":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["lineage_json"]["forcing_package_manifest_uri"] = "s3://nhms/other.json"
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    elif mutation == "version-lineage-checksum":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["lineage_json"]["forcing_package_manifest_checksum"] = "0" * 64
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    elif mutation == "version-lineage-basin":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["lineage_json"]["basin_version_id"] = "other"
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    elif mutation == "version-top-basin":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["basin_version_id"] = "other"
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    elif mutation == "version-top-package-uri":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["forcing_package_uri"] = "s3://nhms/forcing/ifs/other"
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    elif mutation == "version-top-package-checksum":
        version = json.loads((leaf / "forcing_version_record.json").read_text())
        version["checksum"] = "0" * 64
        (leaf / "forcing_version_record.json").write_text(json.dumps(version), encoding="utf-8")
    else:
        package = json.loads((leaf / "forcing_domain_package.json").read_text())
        package["model_id"] = "other"
        (leaf / "forcing_domain_package.json").write_text(json.dumps(package), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config, now=datetime(2026, 7, 11, tzinfo=UTC), mount_id_provider=_mount_id
    )
    assert candidates == []
    assert len(failures) == 1


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
                    "output_uri": f"s3://nhms/runs/{run_id}/output/",
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


def _inject_state_open_failures(
    monkeypatch: pytest.MonkeyPatch,
    config: archive.MoverConfig,
    failures: dict[str, int],
) -> None:
    real_open = archive.open_directory_no_follow

    def injected(path: Path, *, containment_root: Path | None = None) -> int:
        try:
            relative = Path(path).relative_to(config.object_store_root).as_posix()
        except ValueError:
            relative = ""
        if relative in failures:
            code = failures[relative]
            raise PermissionError(code, "private-state-token", str(path))
        return real_open(path, containment_root=containment_root)

    monkeypatch.setattr(archive, "open_directory_no_follow", injected)


def _assert_states_access_failure(
    failures: list[archive.DiscoveryFailure], *, count: int
) -> None:
    assert failures == [
        archive.DiscoveryFailure(
            "states",
            "states",
            f"STATES_ACCESS_DENIED count={count} euid={os.geteuid()} egid={os.getegid()}",
        )
    ]


def test_live_shape_provenance_binds_projected_fixture_bytes_and_authoritative_fields() -> None:
    provenance = json.loads(_LIVE_SHAPE_PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["schema_version"] == "1.0"
    assert len(provenance["samples"]) == 6
    assert all(Path(sample["source_path"]).is_absolute() for sample in provenance["samples"])
    for sample in provenance["samples"]:
        fixture = _LIVE_SHAPE.parent / sample["fixture_path"]
        assert fixture.is_file()
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == sample["fixture_sha256"]
        original = sample["original_manifest_sha256"]
        assert original is None or original != sample["fixture_sha256"]
        assert sample["authoritative_fields"]
        for relative, expected_sha in sample.get("fixture_bundle_sha256", {}).items():
            bundled = fixture.parent / relative
            assert hashlib.sha256(bundled.read_bytes()).hexdigest() == expected_sha

    ifs_run = next(
        sample
        for sample in provenance["samples"]
        if sample["lane"] == "runs"
        and sample["authoritative_fields"]["source_id"] == "IFS"
    )
    assert ifs_run["authoritative_fields"]["end_time"] == "2026-06-06T06:00:00Z"


def test_states_permission_runbook_requires_enforce_write_access_and_future_inheritance() -> None:
    runbook = (
        _ROOT / "docs/runbooks/tier-node27-timeseries-storage.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())
    assert "read/write/search (`rwx`)" in normalized
    assert "`rx` is only sufficient for discovery/dry-run" in normalized
    assert "POSIX default ACLs cannot express different named-user entries" in normalized
    assert "directory `test -x`/`test -w`, and file `test -r`" in normalized
    assert "Every product selected from `forcing`, `runs`, or `states`" in normalized
    assert "one randomized hidden probe per unique opened source parent" in normalized
    assert "SOURCE_RETIREMENT_PREFLIGHT_FAILED" in normalized
    assert ".selected[].source_path | @base64" in runbook
    assert "find \"$source\" -xdev -type d" in runbook
    assert "find \"$source\" -xdev -type f" in runbook
    assert "Do not manually delete prior" in normalized
    assert "foreign/ambiguous guards" in normalized


def test_live_shape_complete_forcing_bundles_execute_real_domain_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _live_shape_config(tmp_path)
    calls: list[str] = []
    real_validate = archive._validate_forcing_domain_bundle

    def observed_validate(forcing, *, source_relative, **kwargs):
        calls.append(source_relative)
        return real_validate(
            forcing,
            source_relative=source_relative,
            **kwargs,
        )

    monkeypatch.setattr(archive, "_validate_forcing_domain_bundle", observed_validate)
    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )

    assert failures == []
    assert {candidate.identity.lane for candidate in candidates} == {
        "forcing",
        "runs",
        "states",
    }
    assert calls == [
        "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud",
        "forcing/ifs/2026070500/basins_qhh_vbasins/basins_qhh_shud",
    ]


@pytest.mark.parametrize(
    "mutation",
    ["payload-checksum", "lineage-checksum", "domain-identity"],
)
def test_live_shape_forcing_bundle_checksum_and_provenance_drift_fail_validation(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = _live_shape_config(tmp_path)
    leaf = (
        config.object_store_root
        / "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud"
    )
    if mutation == "payload-checksum":
        (leaf / "payloads/station_inventory.json").write_bytes(b"[]\n")
    elif mutation == "lineage-checksum":
        path = leaf / "forcing_version_record.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["lineage_json"]["forcing_package_manifest_checksum"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path = leaf / "forcing_domain_package.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["model_id"] = "basins_qhh_shud"
        path.write_text(json.dumps(value), encoding="utf-8")

    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )

    assert len(candidates) == 5
    assert len(failures) == 1
    assert failures[0].locator == (
        "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud"
    )


def test_states_root_eacces_is_one_lane_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enforce=False)
    _state(config, provider=True)
    real_exists = archive._entry_exists

    def denied_root(path: Path, root: Path) -> bool:
        if path == config.object_store_root / "states":
            raise PermissionError(errno.EACCES, "private-root-token", str(path))
        return real_exists(path, root)

    monkeypatch.setattr(archive, "_entry_exists", denied_root)
    locators, failures = archive.discover_candidate_locators(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )

    assert locators == []
    _assert_states_access_failure(failures, count=1)
    assert "private-root-token" not in failures[0].reason


@pytest.mark.parametrize(
    ("provider", "denied"),
    [
        (True, "states/IFS"),
        (False, "states/model-c"),
    ],
    ids=["provider-root", "legacy-model-root"],
)
def test_states_provider_and_legacy_root_eacces_are_aggregated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: bool,
    denied: str,
) -> None:
    config = _config(tmp_path, enforce=False)
    _state(config, provider=provider)
    _inject_state_open_failures(monkeypatch, config, {denied: errno.EACCES})

    locators, failures = archive.discover_candidate_locators(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )

    assert locators == []
    _assert_states_access_failure(failures, count=1)


@pytest.mark.parametrize("denied_count", [1, 2], ids=["one-leaf", "multiple-leaves"])
def test_discover_candidates_compatibility_helper_aggregates_full_validation_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied_count: int,
) -> None:
    config = _live_shape_config(tmp_path)
    real_validate = archive._validate_candidate_locator
    denied = {
        "states/IFS/basins_qhh_shud/2026050100",
        "states/gfs/basins_heihe_shud/2026050100",
    }
    if denied_count == 1:
        denied = {"states/IFS/basins_qhh_shud/2026050100"}

    def injected(locator, runtime_config, *, mount_id_provider):
        if locator.source_relative in denied:
            raise PermissionError(
                errno.EACCES,
                "private-full-validation-token",
                str(locator.source_path),
            )
        return real_validate(
            locator,
            runtime_config,
            mount_id_provider=mount_id_provider,
        )

    monkeypatch.setattr(archive, "_validate_candidate_locator", injected)
    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )

    assert len(candidates) == 6 - denied_count
    _assert_states_access_failure(failures, count=denied_count)
    assert "private-full-validation-token" not in failures[0].reason


def test_live_shape_fixture_accepts_canonical_gfs_ifs_qhh_heihe_and_reproduces_historical_prefix(
    tmp_path: Path,
) -> None:
    config = _live_shape_config(tmp_path)
    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )
    assert failures == []
    assert len(candidates) == 6
    assert {
        (
            candidate.identity.lane,
            candidate.identity.source,
            candidate.identity.basin_version_id,
            candidate.identity.model_id,
            candidate.identity.run_id,
        )
        for candidate in candidates
    } == {
        ("forcing", "gfs", "basins_heihe_vbasins", "basins_heihe_shud", None),
        ("forcing", "IFS", "basins_qhh_vbasins", "basins_qhh_shud", None),
        ("runs", "gfs", None, None, "fcst_gfs_2026053106_basins_heihe_shud"),
        ("runs", "IFS", None, None, "fcst_ifs_2026053106_basins_qhh_shud"),
        ("states", "gfs", None, "basins_heihe_shud", None),
        ("states", "IFS", None, "basins_qhh_shud", None),
    }
    assert {candidate.source_relative for candidate in candidates} == {
        "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud",
        "forcing/ifs/2026070500/basins_qhh_vbasins/basins_qhh_shud",
        "runs/fcst_gfs_2026053106_basins_heihe_shud",
        "runs/fcst_ifs_2026053106_basins_qhh_shud",
        "states/gfs/basins_heihe_shud/2026050100",
        "states/IFS/basins_qhh_shud/2026050100",
    }
    assert {
        candidate.producer["subject_id"]
        for candidate in candidates
        if candidate.identity.lane == "forcing" and candidate.producer is not None
    } == {
        "forc_gfs_2026061600_basins_heihe_shud",
        "forc_ifs_2026070500_basins_qhh_shud",
    }
    assert {
        (candidate.identity.run_id, candidate.producer["basin_version_id"], candidate.producer["model_id"])
        for candidate in candidates
        if candidate.identity.lane == "runs" and candidate.producer is not None
    } == {
        (
            "fcst_gfs_2026053106_basins_heihe_shud",
            "basins_heihe_vbasins",
            "basins_heihe_shud",
        ),
        (
            "fcst_ifs_2026053106_basins_qhh_shud",
            "basins_qhh_vbasins",
            "basins_qhh_shud",
        ),
    }

    historical = archive.MoverConfig(
        **{**config.__dict__, "object_store_prefix": "s3://nhms-object-store"}
    )
    historical_candidates, historical_failures = archive.discover_candidates(
        historical,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )
    assert {candidate.identity.lane for candidate in historical_candidates} == {"states"}
    assert [failure.reason for failure in historical_failures].count(
        "forcing manifest file URI escapes its exact package leaf"
    ) == 2
    assert [failure.reason for failure in historical_failures].count(
        "run manifest identity/outputs do not bind run directory"
    ) == 2
    assert {failure.locator for failure in historical_failures} == {
        "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud",
        "forcing/ifs/2026070500/basins_qhh_vbasins/basins_qhh_shud",
        "runs/fcst_gfs_2026053106_basins_heihe_shud",
        "runs/fcst_ifs_2026053106_basins_qhh_shud",
    }


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("forcing-cross-leaf", "forcing manifest file URI escapes its exact package leaf"),
        ("run-drift", "run manifest identity/outputs do not bind run directory"),
        ("run-double-trailing-slash", "multiple trailing slashes"),
    ],
)
def test_live_shape_fixture_retains_cross_leaf_and_uri_safety_boundaries(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    config = _live_shape_config(tmp_path)
    if case == "forcing-cross-leaf":
        path = (
            config.object_store_root
            / "forcing/gfs/2026061600/basins_heihe_vbasins/basins_heihe_shud/forcing_package.json"
        )
        manifest = json.loads(path.read_text())
        manifest["files"][0]["uri"] = (
            "s3://nhms/forcing/gfs/2026061600/basins_qhh_vbasins/"
            "basins_heihe_shud/forcing.tsd.forc"
        )
    else:
        path = (
            config.object_store_root
            / "runs/fcst_ifs_2026053106_basins_qhh_shud/input/manifest.json"
        )
        manifest = json.loads(path.read_text())
        manifest["outputs"]["output_uri"] = (
            "s3://nhms/runs/other/output"
            if case == "run-drift"
            else "s3://nhms/runs/fcst_ifs_2026053106_basins_qhh_shud/output//"
        )
    path.write_text(json.dumps(manifest), encoding="utf-8")

    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )
    assert len(candidates) == 5
    assert len(failures) == 1
    assert reason in failures[0].reason


def test_live_shape_run_output_without_real_trailing_slash_remains_accepted(
    tmp_path: Path,
) -> None:
    config = _live_shape_config(tmp_path)
    path = (
        config.object_store_root
        / "runs/fcst_ifs_2026053106_basins_qhh_shud/input/manifest.json"
    )
    manifest = json.loads(path.read_text())
    manifest["outputs"]["output_uri"] = manifest["outputs"]["output_uri"].removesuffix("/")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )
    assert len(candidates) == 6
    assert failures == []


@pytest.mark.parametrize(
    "denied",
    [
        {"states/gfs/basins_heihe_shud": errno.EACCES},
        {
            "states/gfs/basins_heihe_shud": errno.EACCES,
            "states/IFS/basins_qhh_shud": errno.EACCES,
        },
    ],
    ids=["one-eacces", "multiple-eacces"],
)
def test_state_eacces_is_aggregated_once_before_any_enforce_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied: dict[str, int],
) -> None:
    config = _live_shape_config(tmp_path, enforce=True)
    before = _tree_snapshot(config.object_store_root)
    _inject_state_open_failures(monkeypatch, config, denied)

    receipt, code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )

    assert code == 1
    assert receipt["outcome"] == "failed"
    assert receipt["selected"] == []
    assert receipt["candidates"] == receipt["deferred"]
    assert receipt["terminals"] == []
    assert receipt["events"] == []
    assert receipt["validation_attempts"] == 0
    assert receipt["bytes"] == {"source": 0, "archived": 0}
    assert receipt["discovery_failures"] == [
        {
            "lane_hint": "states",
            "locator": "states",
            "reason": (
                f"STATES_ACCESS_DENIED count={len(denied)} "
                f"euid={os.geteuid()} egid={os.getegid()}"
            ),
        }
    ]
    assert "private-state-token" not in json.dumps(receipt)
    assert str(config.object_store_root) not in json.dumps(receipt)
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []
    archive.validate_receipt_semantics(receipt)


def test_state_eacces_aggregate_does_not_swallow_non_eacces_locator_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _live_shape_config(tmp_path, enforce=True)
    _inject_state_open_failures(
        monkeypatch,
        config,
        {
            "states/gfs/basins_heihe_shud": errno.EIO,
            "states/IFS/basins_qhh_shud": errno.EACCES,
        },
    )

    receipt, code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )

    assert code == 1
    assert [(item["lane_hint"], item["locator"]) for item in receipt["discovery_failures"]] == [
        ("states", "states"),
        ("states", "states/gfs/basins_heihe_shud"),
    ]
    assert receipt["discovery_failures"][0]["reason"].startswith(
        "STATES_ACCESS_DENIED count=1"
    )
    assert "private-state-token" in receipt["discovery_failures"][1]["reason"]
    assert receipt["selected"] == []
    assert list(config.archive_root.iterdir()) == []


def test_state_eacces_during_bounded_leaf_validation_freezes_all_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _live_shape_config(tmp_path, enforce=True)
    before = _tree_snapshot(config.object_store_root)
    real_validate = archive._validate_candidate_locator

    def injected(locator, runtime_config, *, mount_id_provider):
        if locator.identity.lane == "states":
            raise PermissionError(
                errno.EACCES,
                "private-validation-token",
                str(locator.source_path),
            )
        return real_validate(
            locator,
            runtime_config,
            mount_id_provider=mount_id_provider,
        )

    monkeypatch.setattr(archive, "_validate_candidate_locator", injected)
    receipt, code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )

    assert code == 1
    assert receipt["validation_attempts"] == 6
    assert receipt["selected"] == []
    assert receipt["candidates"] == receipt["deferred"]
    assert receipt["terminals"] == []
    assert receipt["events"] == []
    assert receipt["discovery_failures"] == [
        {
            "lane_hint": "states",
            "locator": "states",
            "reason": (
                f"STATES_ACCESS_DENIED count=2 euid={os.geteuid()} egid={os.getegid()}"
            ),
        }
    ]
    assert "private-validation-token" not in json.dumps(receipt)
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []


def test_mixed_full_validation_failures_exclude_failed_locator_before_later_state_eacces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _live_shape_config(tmp_path, enforce=True)
    before = _tree_snapshot(config.object_store_root)
    locators, discovery_failures = archive.discover_candidate_locators(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
    )
    state_locators = [locator for locator in locators if locator.identity.lane == "states"]
    assert discovery_failures == []
    assert len(state_locators) == 2
    first_failed = state_locators[0].source_relative
    later_denied = state_locators[1].source_relative
    real_validate = archive._validate_candidate_locator

    def injected(locator, runtime_config, *, mount_id_provider):
        if locator.source_relative == first_failed:
            raise OSError(errno.EIO, "private-io-token", str(locator.source_path))
        if locator.source_relative == later_denied:
            raise PermissionError(
                errno.EACCES,
                "private-access-token",
                str(locator.source_path),
            )
        return real_validate(
            locator,
            runtime_config,
            mount_id_provider=mount_id_provider,
        )

    monkeypatch.setattr(archive, "_validate_candidate_locator", injected)
    receipt, code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )

    assert code == 1
    assert receipt["outcome"] == "failed"
    assert receipt["validation_attempts"] == 6
    assert receipt["selected"] == []
    assert receipt["candidates"] == receipt["deferred"]
    assert first_failed not in {item["source_path"] for item in receipt["candidates"]}
    assert later_denied in {item["source_path"] for item in receipt["deferred"]}
    assert receipt["terminals"] == []
    assert receipt["events"] == []
    assert receipt["bytes"] == {"source": 0, "archived": 0}
    assert [(item["lane_hint"], item["locator"]) for item in receipt["discovery_failures"]] == [
        ("states", "states"),
        ("states", first_failed),
    ]
    assert receipt["discovery_failures"][0]["reason"].startswith(
        "STATES_ACCESS_DENIED count=1"
    )
    assert "private-access-token" not in json.dumps(receipt)
    assert "private-io-token" in receipt["discovery_failures"][1]["reason"]
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []
    archive.validate_receipt_semantics(receipt)


def test_receipt_semantics_rejects_failed_locator_reintroduced_as_candidate(
    tmp_path: Path,
) -> None:
    config = _live_shape_config(tmp_path)
    receipt, code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    failed_source = receipt["candidates"][0]["source_path"]
    receipt["discovery_failures"] = [
        {
            "lane_hint": receipt["candidates"][0]["identity"]["lane"],
            "locator": failed_source,
            "reason": "injected validation failure",
        }
    ]
    receipt["outcome"] = "failed"

    with pytest.raises(
        archive.ArchiveMoverError,
        match="failed locator must not also appear",
    ):
        archive.validate_receipt_semantics(receipt)


def test_main_publishes_exact_state_access_receipt_then_emits_one_compact_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _live_shape_config(tmp_path, enforce=True)
    before = _tree_snapshot(config.object_store_root)
    _inject_state_open_failures(
        monkeypatch,
        config,
        {
            "states/gfs/basins_heihe_shud": errno.EACCES,
            "states/IFS/basins_qhh_shud": errno.EACCES,
        },
    )
    real_run = archive.run

    def local_run(runtime_config: archive.MoverConfig):
        return real_run(
            runtime_config,
            now=_LIVE_SHAPE_NOW,
            mount_id_provider=_mount_id,
            rename_impl=_rename_noreplace,
        )

    monkeypatch.setattr(archive, "run", local_run)
    code = archive.main(
        [
            "--object-store-root",
            str(config.object_store_root),
            "--object-store-prefix",
            config.object_store_prefix,
            "--archive-root",
            str(config.archive_root),
            "--receipt",
            str(config.receipt_path),
            "--lock-file",
            str(config.lock_path),
            "--zstd",
            str(config.zstd_path),
            "--enforce",
        ]
    )

    assert code == 2
    expected = {
        "count": 2,
        "egid": os.getegid(),
        "euid": os.geteuid(),
        "exit_reason": "STATES_ACCESS_DENIED",
        "status": "failed",
    }
    assert capsys.readouterr().err == json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ) + "\n"
    receipt = json.loads(config.receipt_path.read_text())
    assert receipt["discovery_failures"] == [
        {
            "lane_hint": "states",
            "locator": "states",
            "reason": (
                f"STATES_ACCESS_DENIED count=2 euid={os.geteuid()} egid={os.getegid()}"
            ),
        }
    ]
    assert stat.S_IMODE(config.receipt_path.stat().st_mode) == 0o600
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []


def test_main_keeps_exit_one_for_other_receipt_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, enforce=False)
    monkeypatch.setattr(
        archive,
        "run",
        lambda _config: (
            {
                "discovery_failures": [
                    {
                        "lane_hint": "runs",
                        "locator": "runs/bad",
                        "reason": "run manifest identity/outputs do not bind run directory",
                    }
                ]
            },
            1,
        ),
    )
    code = archive.main(
        [
            "--object-store-root",
            str(config.object_store_root),
            "--object-store-prefix",
            config.object_store_prefix,
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
    assert code == 1
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("zero-count", "diagnostic shape"),
        ("wrong-locator", "diagnostic shape"),
    ],
)
def test_receipt_semantics_rejects_malformed_state_access_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    config = _live_shape_config(tmp_path)
    _inject_state_open_failures(
        monkeypatch,
        config,
        {"states/gfs/basins_heihe_shud": errno.EACCES},
    )
    receipt, _code = archive.run(
        config,
        now=_LIVE_SHAPE_NOW,
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    if mutation == "zero-count":
        receipt["discovery_failures"][0]["reason"] = (
            f"STATES_ACCESS_DENIED count=0 euid={os.geteuid()} egid={os.getegid()}"
        )
    elif mutation == "wrong-locator":
        receipt["discovery_failures"][0]["locator"] = "states/gfs/basins_heihe_shud"
    with pytest.raises(archive.ArchiveMoverError, match=reason):
        archive.validate_receipt_semantics(receipt)


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
            object_store_prefix=config.object_store_prefix,
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


def test_dry_run_unwritable_source_parent_fails_without_publication(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    source = _run(config)
    denied_inode = source.parent.stat().st_ino

    def access_check(fd: int, mode: int) -> bool:
        return not (os.fstat(fd).st_ino == denied_inode and mode == os.W_OK | os.X_OK)

    before = _tree_snapshot(config.object_store_root)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=access_check,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "failed"
    assert receipt["terminals"][0]["reason"].endswith("check=source-parent-wx")
    assert receipt["bytes"]["archived"] == 0
    assert receipt["events"] == []
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []


def test_enforce_parent_probe_failure_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    before = _tree_snapshot(config.object_store_root)

    def denied(candidate, _config, _provider, _parent_fd):
        raise archive.RetirementPreflightError(candidate.source_relative, "source-parent-probe")

    monkeypatch.setattr(archive, "_probe_source_parent_capability", denied)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["terminals"][0]["reason"].endswith("check=source-parent-probe")
    assert receipt["bytes"]["archived"] == 0
    assert receipt["events"] == []
    assert _tree_snapshot(config.object_store_root) == before
    assert source.exists()
    assert list(config.archive_root.iterdir()) == []


@pytest.mark.parametrize("enforce", [False, True], ids=["dry-run", "enforce"])
def test_internal_directory_without_write_access_fails_batch_preflight(
    tmp_path: Path, enforce: bool
) -> None:
    config = _config(tmp_path, enforce=enforce)
    source = _run(config)
    denied_inode = (source / "output").stat().st_ino

    def access_check(fd: int, mode: int) -> bool:
        return not (
            os.fstat(fd).st_ino == denied_inode
            and mode == os.R_OK | os.W_OK | os.X_OK
        )

    before = _tree_snapshot(config.object_store_root)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=access_check,
    )
    assert code == 1
    assert receipt["terminals"][0]["reason"].endswith("check=tree-directory-rwx")
    assert receipt["bytes"]["archived"] == 0
    assert receipt["events"] == []
    assert _tree_snapshot(config.object_store_root) == before
    assert list(config.archive_root.iterdir()) == []


def test_mixed_batch_one_retirement_blocker_prevents_every_publication(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    first = _forcing(config, "2026010100")
    second = _forcing(config, "2026010200")
    denied_inode = second.parent.stat().st_ino

    def access_check(fd: int, mode: int) -> bool:
        return not (os.fstat(fd).st_ino == denied_inode and mode == os.W_OK | os.X_OK)

    before = _tree_snapshot(config.object_store_root)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=access_check,
    )
    assert code == 1
    assert len(receipt["selected"]) == 2
    assert [item["status"] for item in receipt["terminals"]] == ["failed", "failed"]
    assert receipt["terminals"][0]["reason"] == (
        "SOURCE_RETIREMENT_PREFLIGHT_BATCH_ABORTED"
    )
    assert receipt["terminals"][1]["reason"].endswith("check=source-parent-wx")
    assert receipt["bytes"]["archived"] == 0
    assert receipt["events"] == []
    assert _tree_snapshot(config.object_store_root) == before
    assert first.exists() and second.exists()
    assert list(config.archive_root.iterdir()) == []


def test_parent_probe_failure_cleans_probe_and_reports_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert failures == [] and len(candidates) == 1
    real_fsync = os.fsync
    failed = False

    def fail_first_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and os.fstat(fd).st_ino == source.parent.stat().st_ino:
            failed = True
            raise OSError(errno.EIO, "private-probe-token", str(source.parent))
        real_fsync(fd)

    monkeypatch.setattr(archive.os, "fsync", fail_first_fsync)
    parent_fd = archive.open_directory_no_follow(
        source.parent, containment_root=config.object_store_root
    )
    try:
        with pytest.raises(archive.RetirementPreflightError) as caught:
            archive._probe_source_parent_capability(
                candidates[0], config, _mount_id, parent_fd
            )
    finally:
        os.close(parent_fd)
    assert caught.value.indeterminate is False
    assert caught.value.residue == ()
    assert not any(path.name.startswith(".archive-preflight-") for path in source.parent.iterdir())
    assert "private-probe-token" not in str(caught.value)
    assert str(source.parent) not in str(caught.value)


def test_parent_probe_cleanup_uncertainty_is_indeterminate_with_safe_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert failures == [] and len(candidates) == 1
    real_rmdir = os.rmdir

    def deny_probe_cleanup(path, *args, **kwargs):
        if str(path).startswith(".archive-preflight-"):
            raise OSError(errno.EIO, "private-cleanup-token", str(source.parent))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(archive.os, "rmdir", deny_probe_cleanup)
    parent_fd = archive.open_directory_no_follow(
        source.parent, containment_root=config.object_store_root
    )
    try:
        with pytest.raises(archive.RetirementPreflightError) as caught:
            archive._probe_source_parent_capability(
                candidates[0], config, _mount_id, parent_fd
            )
    finally:
        os.close(parent_fd)
    assert caught.value.indeterminate is True
    assert len(caught.value.residue) == 1
    residue = caught.value.residue[0]
    assert residue.startswith("runs/.archive-preflight-")
    assert "private-cleanup-token" not in str(caught.value)
    assert str(source.parent) not in str(caught.value)
    real_rmdir(config.object_store_root / residue)


def test_enforce_probe_cleanup_uncertainty_is_a_safe_indeterminate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    before = _tree_snapshot(config.object_store_root)

    def indeterminate(candidate, _config, _provider, _parent_fd):
        raise archive.RetirementPreflightError(
            candidate.source_relative,
            "source-parent-probe-cleanup",
            indeterminate=True,
            residue=("runs/.archive-preflight-safe-token",),
        )

    monkeypatch.setattr(archive, "_probe_source_parent_capability", indeterminate)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["outcome"] == "indeterminate"
    assert receipt["terminals"] == [
        {
            "identity": receipt["selected"][0]["identity"],
            "status": "indeterminate",
            "reason": "SOURCE_RETIREMENT_PREFLIGHT_FAILED check=source-parent-probe-cleanup",
            "source_bytes": receipt["selected"][0]["source_bytes"],
            "archive_bytes": 0,
            "residue": ["runs/.archive-preflight-safe-token"],
        }
    ]
    assert receipt["events"] == []
    assert _tree_snapshot(config.object_store_root) == before
    assert source.exists()
    assert list(config.archive_root.iterdir()) == []


def test_enforce_probes_each_unique_source_parent_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _run(config, "opaque-run-a")
    _run(config, "opaque-run-b")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert failures == [] and len(candidates) == 2
    original = archive._probe_source_parent_capability
    probed: list[Path] = []

    def counted(candidate, mover_config, provider, parent_fd):
        probed.append(candidate.source_path.parent)
        original(candidate, mover_config, provider, parent_fd)

    monkeypatch.setattr(archive, "_probe_source_parent_capability", counted)
    archive._preflight_selected_retirement(
        candidates,
        config,
        _mount_id,
        archive._directory_effective_access,
    )
    assert probed == [config.object_store_root / "runs"]
    assert not any(
        path.name.startswith(".archive-preflight-")
        for path in (config.object_store_root / "runs").iterdir()
    )


def test_retirement_preflight_receipt_never_exposes_raw_path_or_exception(
    tmp_path: Path
) -> None:
    config = _config(tmp_path, enforce=False)
    source = _run(config)

    def unsafe_access(_fd: int, _mode: int) -> bool:
        raise PermissionError(errno.EACCES, "private-preflight-token", str(source))

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=unsafe_access,
    )
    rendered = json.dumps(receipt, sort_keys=True)
    assert code == 1
    assert "private-preflight-token" not in rendered
    assert str(tmp_path) not in rendered
    assert receipt["terminals"][0]["reason"].endswith("check=verification")


def test_enforce_parent_probe_stays_bound_to_held_fd_across_namespace_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert failures == [] and len(candidates) == 1
    parent = source.parent
    displaced = parent.with_name("runs-displaced")
    original = archive._probe_source_parent_capability
    bound_inode = parent.stat().st_ino

    def swap_then_probe(candidate, mover_config, provider, parent_fd):
        assert os.fstat(parent_fd).st_ino == bound_inode
        parent.rename(displaced)
        parent.mkdir()
        assert parent.stat().st_ino != bound_inode
        original(candidate, mover_config, provider, parent_fd)

    monkeypatch.setattr(archive, "_probe_source_parent_capability", swap_then_probe)
    with pytest.raises(archive.RetirementPreflightError) as caught:
        archive._preflight_selected_retirement(
            candidates,
            config,
            _mount_id,
            archive._directory_effective_access,
        )
    assert caught.value.check == "source-parent-identity"
    assert not any(path.name.startswith(".archive-preflight-") for path in parent.iterdir())
    assert not any(path.name.startswith(".archive-preflight-") for path in displaced.iterdir())


@pytest.mark.parametrize(
    ("sticky_target", "expected_check"),
    [("parent", "source-parent-sticky"), ("nested", "tree-directory-sticky")],
)
def test_sticky_directory_without_proven_ownership_blocks_even_when_access_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sticky_target: str,
    expected_check: str,
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _run(config)
    target = source.parent if sticky_target == "parent" else source / "output"
    target.chmod(stat.S_IMODE(target.stat().st_mode) | stat.S_ISVTX)
    monkeypatch.setattr(archive.os, "geteuid", lambda: target.stat().st_uid + 100_000)
    probes = 0

    def probe(*_args, **_kwargs):
        nonlocal probes
        probes += 1

    monkeypatch.setattr(archive, "_probe_source_parent_capability", probe)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=lambda _fd, _mode: True,
    )
    assert code == 1
    assert receipt["terminals"][0]["reason"] == (
        f"SOURCE_RETIREMENT_PREFLIGHT_FAILED check={expected_check}"
    )
    assert receipt["bytes"]["archived"] == 0
    assert receipt["events"] == []
    assert probes == 0
    assert source.exists()
    assert list(config.archive_root.iterdir()) == []


@pytest.mark.parametrize("lane", ["forcing", "runs", "states"])
def test_preflight_reason_supports_legal_space_bearing_source_locator(
    tmp_path: Path, lane: str
) -> None:
    config = _config(tmp_path, enforce=False)
    if lane == "forcing":
        original = _forcing(config)
        leaf = original.parent.parent / "basin a" / "model a"
        leaf.parent.mkdir()
        original.rename(leaf)
        original.parent.rmdir()
        manifest_path = leaf / "forcing_package.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["basin_version_id"] = "basin a"
        manifest["model_id"] = "model a"
        manifest["files"][0]["uri"] = (
            "s3://nhms/forcing/gfs/2026010100/basin a/model a/payload.csv"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif lane == "runs":
        leaf = _run(config, "opaque run")
    else:
        leaf = config.object_store_root / "states/IFS/model c/2026010300"
        leaf.mkdir(parents=True)
        (leaf / "state.cfg.ic").write_bytes(b"state")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=lambda _fd, _mode: False,
    )
    assert code == 1, receipt
    assert " " in receipt["selected"][0]["source_path"]
    assert receipt["terminals"][0]["reason"] == (
        "SOURCE_RETIREMENT_PREFLIGHT_FAILED check=source-parent-wx"
    )
    archive.validate_receipt_semantics(receipt)
    assert leaf.exists()


@pytest.mark.parametrize(
    ("mode", "reason", "expected"),
    [
        ("dry-run", "SOURCE_RETIREMENT_PREFLIGHT_FAILED check=unknown-token", "check token"),
        (
            "dry-run",
            "SOURCE_RETIREMENT_PREFLIGHT_FAILED check=source-parent-probe",
            "enforce-only",
        ),
        (
            "enforce",
            "SOURCE_RETIREMENT_PREFLIGHT_FAILED check=source-parent-wx-extra",
            "check token",
        ),
    ],
)
def test_receipt_semantics_rejects_unknown_or_mode_invalid_preflight_check(
    tmp_path: Path, mode: str, reason: str, expected: str
) -> None:
    config = _config(tmp_path, enforce=False)
    _run(config)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=lambda _fd, _mode: False,
    )
    assert code == 1
    receipt["mode"] = mode
    receipt["terminals"][0]["reason"] = reason
    with pytest.raises(archive.ArchiveMoverError, match=expected):
        archive.validate_receipt_semantics(receipt)


def test_receipt_semantics_rejects_ambiguous_batch_abort_reason(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config, "2026010100")
    second = _forcing(config, "2026010200")
    denied_inode = second.parent.stat().st_ino
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
        access_check=lambda fd, mode: not (
            os.fstat(fd).st_ino == denied_inode and mode == os.W_OK | os.X_OK
        ),
    )
    assert code == 1
    receipt["terminals"][0]["reason"] += " locator=forcing/with space"
    with pytest.raises(archive.ArchiveMoverError, match="reason is unsafe"):
        archive.validate_receipt_semantics(receipt)


def test_validation_bound_limits_full_tree_scans_and_defers_without_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=False, bound=1)
    _forcing(config, "2026010100")
    _forcing(config, "2026010200")
    original = archive._validate_candidate_locator
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(archive, "_validate_candidate_locator", counted)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["validation_attempts"] == 1
    assert calls == 1
    assert receipt["selected"][0]["validation_state"] == "validated"
    assert receipt["deferred"][0]["validation_state"] == "pending-validation"
    assert "source_bytes" not in receipt["deferred"][0]


def test_failed_earliest_validation_consumes_bound_and_leaves_next_pending(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False, bound=1)
    earliest = _forcing(config, "2026010100")
    _forcing(config, "2026010200")
    (earliest / "payload.csv").unlink()
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["validation_attempts"] == 1
    assert receipt["selected"] == []
    assert len(receipt["deferred"]) == 1
    assert receipt["deferred"][0]["identity"]["cycle_identity"] == "2026010200"
    assert receipt["discovery_failures"][0]["locator"].endswith("2026010100/basin-a/model-a")


def test_bounded_validation_receipt_order_is_repeatable(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False, bound=1)
    _forcing(config, "2026010200")
    _forcing(config, "2026010100")
    kwargs = {
        "now": datetime(2026, 7, 11, tzinfo=UTC),
        "mount_id_provider": _mount_id,
        "rename_impl": _rename_noreplace,
    }
    first, first_code = archive.run(config, **kwargs)
    second, second_code = archive.run(config, **kwargs)
    assert first_code == second_code == 0
    assert first["candidates"] == second["candidates"]
    assert first["selected"] == second["selected"]
    assert first["deferred"] == second["deferred"]


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


def test_forcing_and_run_eligibility_uses_authoritative_end_time(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    forcing = _forcing(config)
    forcing_manifest = forcing / "forcing_package.json"
    forcing_value = json.loads(forcing_manifest.read_text())
    forcing_value["end_time"] = "2026-07-10T00:00:00Z"
    forcing_manifest.write_text(json.dumps(forcing_value), encoding="utf-8")
    run = _run(config)
    run_manifest = run / "input/manifest.json"
    run_value = json.loads(run_manifest.read_text())
    run_value["end_time"] = "2026-07-10T00:00:00Z"
    run_manifest.write_text(json.dumps(run_value), encoding="utf-8")

    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert failures == []


@pytest.mark.parametrize("lane", ["forcing", "runs"])
def test_hot_product_gate_skips_payload_scan_and_cold_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config) if lane == "forcing" else _run(config)
    if lane == "runs":
        (leaf / "output/result.nc").unlink()

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("hot product must not scan/hash its payload tree")

    monkeypatch.setattr(archive, "scan_tree_snapshot", forbidden_scan)
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 1, 10, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert failures == []


@pytest.mark.parametrize("mutation", ["identity", "window", "prefix"])
def test_hot_forcing_still_rejects_lightweight_contract_drift(tmp_path: Path, mutation: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config)
    path = leaf / "forcing_package.json"
    manifest = json.loads(path.read_text())
    if mutation == "identity":
        manifest["model_id"] = "other"
    elif mutation == "window":
        manifest["start_time"], manifest["end_time"] = manifest["end_time"], "2025-12-31T00:00:00Z"
    else:
        manifest["files"][0]["uri"] = "s3://other/forcing/gfs/2026010100/basin-a/model-a/payload.csv"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 1, 10, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1


def test_cold_run_still_requires_complete_output(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    (leaf / "output/result.nc").unlink()
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1
    assert "no regular product" in failures[0].reason


def test_inverted_forcing_window_is_discovery_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    forcing = _forcing(config)
    manifest_path = forcing / "forcing_package.json"
    value = json.loads(manifest_path.read_text())
    value["start_time"] = "2026-01-02T00:00:00Z"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert "window is inverted" in failures[0].reason


@pytest.mark.parametrize(
    "mutation",
    ["unsafe-version", "empty-files", "duplicate", "escape-uri", "bad-checksum", "missing-product", "extra-product"],
)
def test_forcing_manifest_must_completely_bind_pinned_package(tmp_path: Path, mutation: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _forcing(config)
    path = leaf / "forcing_package.json"
    manifest = json.loads(path.read_text())
    if mutation == "unsafe-version":
        manifest["forcing_version_id"] = "../unsafe"
    elif mutation == "empty-files":
        manifest["files"] = []
    elif mutation == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    elif mutation == "escape-uri":
        manifest["files"][0]["uri"] = "s3://nhms/forcing/gfs/2026010100/other/model-a/payload.csv"
    elif mutation == "bad-checksum":
        manifest["files"][0]["checksum"] = "0" * 64
    elif mutation == "missing-product":
        manifest["files"][0]["uri"] = "s3://nhms/forcing/gfs/2026010100/basin-a/model-a/missing.csv"
    else:
        (leaf / "undeclared.bin").write_bytes(b"extra")
    path.write_text(json.dumps(manifest), encoding="utf-8")

    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1


def test_run_without_regular_output_product_is_discovery_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    (leaf / "output/result.nc").unlink()
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert len(failures) == 1
    assert "no regular product" in failures[0].reason


@pytest.mark.parametrize(
    "uri",
    [
        "s3://other/runs/opaque-run/output",
        "s3://nhms/wrong/runs/opaque-run/output",
        "s3://nhms/runs/opaque-run/output?x=1",
        "s3://nhms/runs/opaque-run/output#fragment",
        "s3://nhms/runs/%2e%2e/output",
        "s3://nhms/runs\\opaque-run/output",
        "https://nhms/runs/opaque-run/output",
    ],
)
def test_run_output_uri_must_bind_configured_s3_prefix(tmp_path: Path, uri: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    manifest_path = leaf / "input/manifest.json"
    value = json.loads(manifest_path.read_text())
    value["outputs"]["output_uri"] = uri
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert failures


def test_run_output_uri_without_trailing_slash_remains_compatible(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    manifest_path = leaf / "input/manifest.json"
    value = json.loads(manifest_path.read_text())
    value["outputs"]["output_uri"] = "s3://nhms/runs/opaque-run/output"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert len(candidates) == 1
    assert failures == []


@pytest.mark.parametrize(
    ("field", "uri"),
    [
        ("output_uri", "s3://nhms/runs/opaque-run/output//"),
        ("run_manifest_uri", "s3://nhms/runs/opaque-run/input/manifest.json/"),
    ],
)
def test_run_output_uris_reject_noncanonical_trailing_slashes(tmp_path: Path, field: str, uri: str) -> None:
    config = _config(tmp_path, enforce=False)
    leaf = _run(config)
    manifest_path = leaf / "input/manifest.json"
    value = json.loads(manifest_path.read_text())
    value["outputs"][field] = uri
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert candidates == []
    assert failures


def test_bad_shallow_forcing_and_state_siblings_do_not_hide_valid_leaves(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    _forcing(config)
    _state(config, provider=True)
    (config.object_store_root / "forcing/bad-source").write_text("bad", encoding="utf-8")
    (config.object_store_root / "states/bad-state").write_text("bad", encoding="utf-8")
    candidates, failures = archive.discover_candidates(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert {candidate.identity.lane for candidate in candidates} == {"forcing", "states"}
    assert {failure.locator for failure in failures} == {"forcing/bad-source", "states/bad-state"}


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
    _forcing(enforce)
    manifest_member = next(item for item in receipt["candidates"][0:1])
    del manifest_member  # receipt identity is not used to reconstruct content
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
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    _forcing(config)

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
    assert second["terminals"][0]["status"] == "indeterminate"
    assert all(event["kind"] != "quarantined" for event in second["events"])
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    archive.verify_archive_pair(
        final,
        config.archive_root,
        zstd_path=config.zstd_path,
        object_store_prefix=config.object_store_prefix,
        mount_id_provider=_mount_id,
    )


def test_existing_manifest_file_order_is_irrelevant(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    manifest_path = final / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].reverse()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = _forcing(config)
    second, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert second["terminals"][0]["status"] == "retired-from-existing"
    assert not source.exists()


def test_operational_existing_verify_failure_never_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    source = _forcing(config)

    def timeout(*args, **kwargs):
        raise archive.ArchiveOperationalError("decompressor timed out")

    monkeypatch.setattr(archive, "verify_archive_pair", timeout)
    second, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert second["terminals"][0]["status"] == "indeterminate"
    assert second["events"] == []
    assert final.exists() and source.exists()


def test_existing_final_mount_evidence_failure_keeps_archive_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    source = _forcing(config)
    original_verify = archive.verify_archive_pair

    def verify_with_missing_mount(*args, **kwargs):
        def unavailable(fd: int) -> int:
            raise archive.ArchiveOperationalError("mount evidence unavailable")

        kwargs["mount_id_provider"] = unavailable
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(archive, "verify_archive_pair", verify_with_missing_mount)
    second, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert second["terminals"][0]["status"] == "indeterminate"
    assert second["events"] == []
    assert final.exists() and source.exists()


@pytest.mark.parametrize("body", ["exit 7\n", "cat\nexit 7\n"])
def test_real_decompressor_nonzero_is_operational_and_preserves_evidence(tmp_path: Path, body: str) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    source = _forcing(config)
    failing = tmp_path / "failing-zstd"
    failing.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    failing.chmod(0o700)
    config = archive.MoverConfig(**{**config.__dict__, "zstd_path": failing})

    second, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert second["terminals"][0]["status"] == "indeterminate"
    assert second["events"] == []
    assert final.exists() and source.exists()


def test_decompressor_spawn_race_is_operational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("executable disappeared")

    monkeypatch.setattr(archive.subprocess, "Popen", missing)
    with pytest.raises(archive.ArchiveOperationalError, match="operation failed"):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            object_store_prefix=config.object_store_prefix,
            mount_id_provider=_mount_id,
        )


def test_existing_final_internal_swap_before_retirement_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    first, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
    source = _forcing(config)
    original_same = archive._same_snapshot
    calls = 0

    def swap_after_source_recheck(candidate, source_fd, provider):
        nonlocal calls
        result = original_same(candidate, source_fd, provider)
        calls += 1
        if calls == 2:
            (final / "manifest.json").write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(archive, "_same_snapshot", swap_after_source_recheck)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "failed"
    assert source.exists()
    assert not list(source.parent.glob(".archive-delete-*"))


def test_fresh_final_internal_swap_before_retirement_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    original_retire = archive._retire_source

    def swap_then_retire(candidate, mover_config, source_fd, events, identity, provider, rename_impl, archive_guard):
        _guard_fd, final_leaf, _archive_root = archive_guard
        (final_leaf / "manifest.json").write_text("{}", encoding="utf-8")
        return original_retire(
            candidate,
            mover_config,
            source_fd,
            events,
            identity,
            provider,
            rename_impl,
            archive_guard,
        )

    monkeypatch.setattr(archive, "_retire_source", swap_then_retire)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "failed"
    assert source.exists()
    assert not list(source.parent.glob(".archive-delete-*"))


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
        object_store_prefix=config.object_store_prefix,
        mount_id_provider=_mount_id,
    )


def test_fresh_staging_verification_failure_never_publishes_or_retires_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    original_verify = archive.verify_archive_pair

    def fail_staging(*args, **kwargs):
        if kwargs.get("require_canonical_location") is False:
            raise archive.ArchiveCorruptError("injected fresh staging verification failure")
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(archive, "verify_archive_pair", fail_staging)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert source.exists()
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    assert not final.exists()
    assert receipt["terminals"][0]["status"] == "failed"
    assert all(event["kind"] != "published" for event in receipt["events"])
    assert config.receipt_path.exists()
    assert config.receipt_path.stat().st_mode & 0o777 == 0o600


def test_fresh_staging_tarball_digest_mismatch_never_publishes_or_retires_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    original_manifest = archive._manifest

    def manifest_with_wrong_archive_digest(*args, **kwargs):
        manifest = original_manifest(*args, **kwargs)
        actual_digest = manifest["archive"]["sha256"]
        manifest["archive"]["sha256"] = "0" * 64 if actual_digest != "0" * 64 else "1" * 64
        return manifest

    monkeypatch.setattr(archive, "_manifest", manifest_with_wrong_archive_digest)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )

    assert code == 1
    assert source.exists()
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    assert not final.exists()
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "failed"
    assert "archive tarball size/sha256 mismatch" in terminal["reason"]
    assert all(event["kind"] != "published" for event in receipt["events"])


def test_corrupt_final_namespace_swap_is_restored_without_quarantine_event(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
    final.mkdir(parents=True)
    (final / "unexpected").write_bytes(b"original-corrupt")
    swapped = False

    def swap_before_rename(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal swapped
        if not swapped and src == "model-a" and dst != "model-a":
            swapped = True
            os.rename(src, ".raced-original", src_dir_fd=src_fd, dst_dir_fd=src_fd)
            os.mkdir(src, dir_fd=src_fd)
            replacement_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
            try:
                marker_fd = os.open("replacement", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=replacement_fd)
                os.close(marker_fd)
            finally:
                os.close(replacement_fd)
        _rename_noreplace(src_fd, src, dst_fd, dst)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=swap_before_rename,
    )
    assert code == 1
    assert receipt["events"] == []
    assert receipt["terminals"][0]["status"] == "failed"
    assert source.exists()
    assert (final / "replacement").exists()
    assert not list((config.archive_root / ".quarantine").iterdir())


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
            object_store_prefix=config.object_store_prefix,
            mount_id_provider=_mount_id,
        )


@pytest.mark.parametrize("lane", ["forcing", "runs"])
def test_embedded_producer_rejects_self_consistent_sidecar_rewrite(
    tmp_path: Path, lane: str
) -> None:
    config = _config(tmp_path, enforce=True)
    (_forcing(config) if lane == "forcing" else _run(config))
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    final = config.archive_root / Path(receipt["candidates"][0]["archive_path"]).parent
    manifest_path = final / "manifest.json"
    outer = json.loads(manifest_path.read_text())
    payloads: dict[str, bytes] = {}
    with tarfile.open(final / "archive.tar.zst", mode="r:") as source:
        for member in source:
            extracted = source.extractfile(member)
            assert extracted is not None
            payloads[member.name] = extracted.read()
    embedded_path = "forcing_package.json" if lane == "forcing" else "input/manifest.json"
    embedded = json.loads(payloads[embedded_path])
    if lane == "forcing":
        embedded["files"][0]["uri"] = "s3://other/forcing/gfs/2026010100/basin-a/model-a/payload.csv"
    else:
        embedded["outputs"]["output_uri"] = "s3://other/runs/opaque-run/output/"
    payloads[embedded_path] = json.dumps(embedded, sort_keys=True).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as target:
        for name, raw in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            target.addfile(info, io.BytesIO(raw))
    archive_raw = buffer.getvalue()
    (final / "archive.tar.zst").write_bytes(archive_raw)
    digest = hashlib.sha256(payloads[embedded_path]).hexdigest()
    for entry in outer["files"]:
        if entry["path"] == embedded_path:
            entry["size_bytes"] = len(payloads[embedded_path])
            entry["sha256"] = digest
    outer["producer"]["manifest_sha256"] = digest
    outer["archive"]["size_bytes"] = len(archive_raw)
    outer["archive"]["sha256"] = hashlib.sha256(archive_raw).hexdigest()
    manifest_path.write_text(json.dumps(outer), encoding="utf-8")
    with pytest.raises(archive.ArchiveCorruptError, match="URI escapes|identity/outputs"):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            object_store_prefix=config.object_store_prefix,
            mount_id_provider=_mount_id,
        )


def test_oversized_local_pax_extension_is_rejected_before_body_read() -> None:
    size = archive.MAX_PAX_EXTENSION_BYTES + 1
    info = tarfile.TarInfo("pax")
    info.type = tarfile.XHDTYPE
    info.size = size
    header = info.tobuf(format=tarfile.USTAR_FORMAT)

    class CountingStream(io.BytesIO):
        bytes_read = 0

        def read(self, count: int = -1) -> bytes:
            value = super().read(count)
            self.bytes_read += len(value)
            return value

    class Process:
        def kill(self) -> None:
            pass

    stream = CountingStream(header + b"x" * size)
    guarded = archive._TarHeaderGuardReader(archive._LimitedReader(stream, len(header) + size, Process()))
    with pytest.raises(archive.ArchiveMoverError, match="before body consumption"):
        guarded.read(10_240)
    assert stream.bytes_read == 512


@pytest.mark.parametrize(
    "extension",
    [tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.GNUTYPE_SPARSE],
)
def test_unsupported_tar_extensions_are_rejected_before_body_read(extension: bytes) -> None:
    info = tarfile.TarInfo("extension")
    info.type = extension
    info.size = 4
    header = info.tobuf(format=tarfile.USTAR_FORMAT)

    class Process:
        def kill(self) -> None:
            pass

    stream = io.BytesIO(header + b"test")
    guarded = archive._TarHeaderGuardReader(
        archive._LimitedReader(stream, len(header) + 4, Process())
    )
    with pytest.raises(archive.ArchiveMoverError, match="unsupported extension"):
        guarded.read(10_240)
    assert stream.tell() == 512


def test_many_small_local_pax_headers_fail_typed_without_recursion() -> None:
    info = tarfile.TarInfo("pax")
    info.type = tarfile.XHDTYPE
    body = b"6 x=y\n"
    info.size = len(body)
    header = info.tobuf(format=tarfile.USTAR_FORMAT)
    record = header + body + b"\0" * (512 - len(body))

    class Process:
        def kill(self) -> None:
            pass

    stream = io.BytesIO(record * 330)
    guarded = archive._TarHeaderGuardReader(
        archive._LimitedReader(stream, len(record) * 330, Process()),
        expected_member_count=330,
    )
    with pytest.raises(archive.ArchiveMoverError, match="consecutive local PAX"):
        while guarded.read(512):
            pass


def test_local_pax_count_is_bounded_by_expected_members() -> None:
    info = tarfile.TarInfo("pax")
    info.type = tarfile.XHDTYPE
    body = b"6 x=y\n"
    info.size = len(body)
    pax = info.tobuf(format=tarfile.USTAR_FORMAT) + body + b"\0" * (512 - len(body))
    regular = tarfile.TarInfo("member")
    regular.size = 0
    raw = pax + regular.tobuf(format=tarfile.USTAR_FORMAT) + pax

    class Process:
        def kill(self) -> None:
            pass

    guarded = archive._TarHeaderGuardReader(
        archive._LimitedReader(io.BytesIO(raw), len(raw), Process()),
        expected_member_count=1,
    )
    with pytest.raises(archive.ArchiveMoverError, match="raw header count|local PAX count"):
        while guarded.read(512):
            pass


def test_deterministic_writer_local_pax_path_round_trips(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    leaf = _forcing(config)
    long_name = "product-" + "x" * 110 + ".csv"
    content = b"long-path-product"
    (leaf / long_name).write_bytes(content)
    manifest_path = leaf / "forcing_package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append(
        {
            "uri": f"s3://nhms/forcing/gfs/2026010100/basin-a/model-a/{long_name}",
            "checksum": hashlib.sha256(content).hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0, receipt


@pytest.mark.parametrize("fault", ["unexpected", "declared-size"])
def test_tar_header_limits_fail_before_member_body(tmp_path: Path, fault: str) -> None:
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
    info = tarfile.TarInfo("unexpected.bin" if fault == "unexpected" else manifest["files"][0]["path"])
    info.size = archive.MAX_FILE_BYTES + 1
    raw = info.tobuf(format=tarfile.PAX_FORMAT)
    (final / "archive.tar.zst").write_bytes(raw)
    manifest["archive"]["size_bytes"] = len(raw)
    manifest["archive"]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    expected = "unexpected tar member" if fault == "unexpected" else "declared size differs"
    with pytest.raises(archive.ArchiveCorruptError, match=expected):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            object_store_prefix=config.object_store_prefix,
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
            object_store_prefix=config.object_store_prefix,
            mount_id_provider=mismatch,
        )


@pytest.mark.parametrize("boundary", ["after-hash", "after-tar", "manifest"])
def test_archive_pair_namespace_rebinding_rejects_same_bytes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
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

    def replace_same_bytes(path: Path) -> None:
        raw = path.read_bytes()
        displaced = path.with_name(path.name + ".displaced")
        path.rename(displaced)
        path.write_bytes(raw)

    if boundary == "after-hash":
        original = archive._size_digest_fd

        def swap_after_hash(fd: int, *, max_bytes: int):
            result = original(fd, max_bytes=max_bytes)
            replace_same_bytes(final / "archive.tar.zst")
            return result

        monkeypatch.setattr(archive, "_size_digest_fd", swap_after_hash)
    elif boundary == "manifest":
        original = archive._read_json_relative_fd
        count = 0

        def swap_after_manifest(*args, **kwargs):
            nonlocal count
            result = original(*args, **kwargs)
            if args[1] == "manifest.json":
                count += 1
                if count == 2:
                    replace_same_bytes(final / "manifest.json")
            return result

        monkeypatch.setattr(archive, "_read_json_relative_fd", swap_after_manifest)
    else:
        original = archive._decompressed_tar_stream

        class SwapAfterTar:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                result = self.inner.__exit__(exc_type, exc, traceback)
                replace_same_bytes(final / "archive.tar.zst")
                return result

        monkeypatch.setattr(
            archive,
            "_decompressed_tar_stream",
            lambda fd, zstd, **kwargs: SwapAfterTar(original(fd, zstd, **kwargs)),
        )
    with pytest.raises(archive.ArchiveCorruptError, match="namespace entry changed"):
        archive.verify_archive_pair(
            final,
            config.archive_root,
            zstd_path=config.zstd_path,
            object_store_prefix=config.object_store_prefix,
            mount_id_provider=_mount_id,
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
            object_store_prefix=config.object_store_prefix,
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
    assert terminal["status"] == "indeterminate"
    assert any(".archive-delete-" in item for item in terminal["residue"])
    assert not source.exists()
    tombstone_residue = next(item for item in terminal["residue"] if ".archive-delete-" in item)
    assert (config.object_store_root / tombstone_residue).exists()


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_atomic_tombstone_claim_preserves_preclaim_replacement(
    tmp_path: Path, entry_kind: str
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config) if entry_kind == "file" else _run(config)
    target = "payload.csv" if entry_kind == "file" else "output"
    replaced = False

    def replace_before_claim(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal replaced
        if not replaced and src == target and dst.startswith("entry-"):
            replaced = True
            os.rename(src, f"{src}.original", src_dir_fd=src_fd, dst_dir_fd=src_fd)
            if entry_kind == "file":
                replacement_fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_fd)
                os.write(replacement_fd, b"replacement")
                os.close(replacement_fd)
            else:
                os.mkdir(src, dir_fd=src_fd)
                replacement_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY, dir_fd=src_fd)
                marker_fd = os.open("replacement", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=replacement_fd)
                os.close(marker_fd)
                os.close(replacement_fd)
        _rename_noreplace(src_fd, src, dst_fd, dst)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=replace_before_claim,
    )
    assert code == 1
    assert replaced
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert any(".archive-claims-" in path for path in terminal["residue"])
    claims = list(source.parent.glob(".archive-claims-*"))
    assert claims
    claimed = next(path for path in claims[0].iterdir() if path.name.startswith("entry-"))
    if entry_kind == "file":
        assert claimed.read_bytes() == b"replacement"
    else:
        assert (claimed / "replacement").exists()


def test_late_extra_tombstone_entry_blocks_every_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    original_remove = archive._remove_tree_contents_fd

    def add_extra(directory_fd: int, label: str, **kwargs) -> None:
        extra_fd = os.open("late-extra", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        os.close(extra_fd)
        original_remove(directory_fd, label, **kwargs)

    monkeypatch.setattr(archive, "_remove_tree_contents_fd", add_extra)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    tombstone = next(config.object_store_root.glob("forcing/gfs/2026010100/basin-a/.archive-delete-*"))
    assert (tombstone / "late-extra").exists()
    assert (tombstone / "payload.csv").exists()
    assert (tombstone / "forcing_package.json").exists()


@pytest.mark.parametrize("member", ["archive.tar.zst", "manifest.json"])
def test_durable_guard_preserves_exact_pair_when_canonical_swaps_before_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    original = archive._install_durable_archive_guard
    captured: list[Path] = []

    def install_then_swap(*args, **kwargs):
        guard = original(*args, **kwargs)
        captured.append(guard.path)
        target = guard.canonical_leaf / member
        raw = target.read_bytes()
        target.rename(target.with_name(member + ".displaced"))
        target.write_bytes(raw)
        return guard

    monkeypatch.setattr(archive, "_install_durable_archive_guard", install_then_swap)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert source.exists()
    assert captured and captured[0].is_dir()
    assert captured[0].relative_to(config.archive_root).as_posix() in terminal["residue"]
    archive.verify_archive_pair(
        captured[0],
        config.archive_root,
        zstd_path=config.zstd_path,
        object_store_prefix=config.object_store_prefix,
        require_canonical_location=False,
        mount_id_provider=_mount_id,
    )


@pytest.mark.parametrize("member", ["archive.tar.zst", "manifest.json"])
def test_durable_guard_survives_swap_at_first_destructive_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    original = archive._validate_tombstone_allowlist
    calls = 0

    def validate_then_swap(candidate, tomb_fd, provider):
        nonlocal calls
        original(candidate, tomb_fd, provider)
        calls += 1
        if calls == 2:
            final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
            target = final / member
            raw = target.read_bytes()
            target.rename(target.with_name(member + ".displaced"))
            target.write_bytes(raw)

    monkeypatch.setattr(archive, "_validate_tombstone_allowlist", validate_then_swap)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert not source.exists()
    guard_relative = next(item for item in terminal["residue"] if item.startswith(".archive-guards/"))
    guard = config.archive_root / guard_relative
    archive.verify_archive_pair(
        guard,
        config.archive_root,
        zstd_path=config.zstd_path,
        object_store_prefix=config.object_store_prefix,
        require_canonical_location=False,
        mount_id_provider=_mount_id,
    )


def test_successful_retirement_leaves_no_durable_guard_leaf(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0, receipt
    guard_parent = config.archive_root / ".archive-guards"
    assert guard_parent.is_dir()
    assert list(guard_parent.iterdir()) == []


def _leave_matching_prior_guard(
    config: archive.MoverConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    source = _forcing(config)
    original = archive._retire_source_destructive

    def fail_before_source_mutation(*_args, **_kwargs):
        raise archive.ArchiveMoverError("injected pre-retirement stop")

    monkeypatch.setattr(archive, "_retire_source_destructive", fail_before_source_mutation)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    monkeypatch.setattr(archive, "_retire_source_destructive", original)
    assert code == 1
    assert source.exists()
    guard_relative = next(
        item for item in receipt["terminals"][0]["residue"]
        if item.startswith(".archive-guards/")
    )
    guard = config.archive_root / guard_relative
    assert guard.is_dir()
    return source, guard


def test_existing_archive_reconciles_matching_prior_guard_before_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source, old_guard = _leave_matching_prior_guard(config, monkeypatch)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0, receipt
    assert receipt["terminals"][0]["status"] == "retired-from-existing"
    assert receipt["terminals"][0]["residue"] == []
    assert not source.exists()
    assert not old_guard.exists()
    assert list((config.archive_root / ".archive-guards").iterdir()) == []


def test_prior_guard_reconcile_preserves_foreign_and_ambiguous_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source, ambiguous = _leave_matching_prior_guard(config, monkeypatch)
    (ambiguous / "operator-note").write_text("preserve", encoding="utf-8")
    final = config.archive_root / "forcing/gfs/2026010100/basin-a/model-a"
    foreign = config.archive_root / ".archive-guards/foreign"
    foreign.mkdir()
    shutil.copy2(final / "archive.tar.zst", foreign / "archive.tar.zst")
    shutil.copy2(final / "manifest.json", foreign / "manifest.json")

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0, receipt
    assert not source.exists()
    assert (ambiguous / "operator-note").read_text() == "preserve"
    assert foreign.is_dir()
    assert (foreign / "archive.tar.zst").stat().st_ino != (
        final / "archive.tar.zst"
    ).stat().st_ino


def test_prior_matching_guard_cleanup_failure_preserves_source_and_reports_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source, old_guard = _leave_matching_prior_guard(config, monkeypatch)
    old_guard_inode = old_guard.stat().st_ino
    real_unlink = os.unlink

    def fail_old_guard_unlink(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        if dir_fd is not None and os.fstat(dir_fd).st_ino == old_guard_inode:
            raise OSError(errno.EIO, "private-old-guard-token", str(old_guard))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(archive.os, "unlink", fail_old_guard_unlink)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert old_guard.relative_to(config.archive_root).as_posix() in terminal["residue"]
    assert "private-old-guard-token" not in terminal["reason"]
    assert str(tmp_path) not in terminal["reason"]
    assert source.exists()
    assert old_guard.exists()


def test_durable_guard_mount_evidence_change_blocks_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    changed = False
    original = archive._install_durable_archive_guard

    def provider(fd: int) -> int:
        return os.fstat(fd).st_dev + int(changed)

    def install_then_change(*args, **kwargs):
        nonlocal changed
        guard = original(*args, **kwargs)
        changed = True
        return guard

    monkeypatch.setattr(archive, "_install_durable_archive_guard", install_then_change)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=provider,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["terminals"][0]["status"] == "indeterminate"
    assert source.exists()
    assert any(path.startswith(".archive-guards/") for path in receipt["terminals"][0]["residue"])


def test_durable_guard_child_mount_evidence_change_blocks_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    changed = False
    original = archive._install_durable_archive_guard

    def provider(fd: int) -> int:
        info = os.fstat(fd)
        child_drift = changed and stat.S_ISREG(info.st_mode)
        return info.st_dev + int(child_drift)

    def install_then_change(*args, **kwargs):
        nonlocal changed
        guard = original(*args, **kwargs)
        changed = True
        return guard

    monkeypatch.setattr(archive, "_install_durable_archive_guard", install_then_change)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=provider,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert source.exists()
    assert any(path.startswith(".archive-guards/") for path in terminal["residue"])


@pytest.mark.parametrize("boundary", ["canonical-pair", "second-allowlist"])
def test_early_post_tombstone_failures_report_actual_tombstone_and_guard_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    if boundary == "canonical-pair":
        original_match = archive._canonical_pair_matches
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            return False if calls == 2 else original_match(*args, **kwargs)

        monkeypatch.setattr(archive, "_canonical_pair_matches", fail_second)
    else:
        original_allowlist = archive._validate_tombstone_allowlist
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise archive.ArchiveMoverError("second allowlist injected failure", kind="conflict")
            return original_allowlist(*args, **kwargs)

        monkeypatch.setattr(archive, "_validate_tombstone_allowlist", fail_second)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert not source.exists()
    assert any(".archive-delete-" in path for path in terminal["residue"])
    assert any(path.startswith(".archive-guards/") for path in terminal["residue"])


@pytest.mark.parametrize(
    "fault", ["root-rename", "parent-fsync", "claim-fsync", "root-rmdir", "claim-cleanup"]
)
def test_root_claim_failure_receipt_tracks_only_live_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    real_rmdir = os.rmdir
    real_fsync = os.fsync
    root_claimed = False
    post_claim_fsync = 0

    def rename_with_fault(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal root_claimed
        _rename_noreplace(src_fd, src, dst_fd, dst)
        if dst.startswith("root-"):
            root_claimed = True
            if fault == "root-rename":
                raise OSError("root rename durability fault")

    if fault in {"parent-fsync", "claim-fsync"}:
        def fsync_with_fault(fd: int) -> None:
            nonlocal post_claim_fsync
            if root_claimed:
                post_claim_fsync += 1
                if (fault == "parent-fsync" and post_claim_fsync == 1) or (
                    fault == "claim-fsync" and post_claim_fsync == 2
                ):
                    raise OSError(f"{fault} fault")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync_with_fault)

    if fault in {"root-rmdir", "claim-cleanup"}:
        def rmdir_with_fault(path, *args, **kwargs):
            name = os.fspath(path)
            if fault == "root-rmdir" and name.startswith("root-"):
                raise OSError("root rmdir fault")
            if fault == "claim-cleanup" and name.startswith(".archive-claims-"):
                raise OSError("claim cleanup fault")
            return real_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "rmdir", rmdir_with_fault)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=rename_with_fault,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    source_residue = [item for item in terminal["residue"] if ".archive-claims-" in item]
    assert source_residue, terminal
    assert all((config.object_store_root / item).exists() for item in source_residue)
    assert not any(".archive-delete-" in item for item in terminal["residue"])


@pytest.mark.parametrize("fault", ["claim-parent-open", "claim-open"])
def test_post_tombstone_claim_setup_fault_is_indeterminate_without_fd_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    config = _config(tmp_path, enforce=True)
    source = _forcing(config)
    source_parent = source.parent
    original_open = archive.open_directory_no_follow

    def fail_claim_setup(path, *args, **kwargs):
        candidate = Path(path)
        tombstoned = not source.exists() and any(
            item.name.startswith(f".archive-delete-{source.name}-")
            for item in source_parent.iterdir()
        )
        if fault == "claim-parent-open" and tombstoned and candidate == source_parent:
            raise OSError("injected claim parent open failure")
        if fault == "claim-open" and candidate.name.startswith(f".archive-claims-{source.name}-"):
            raise OSError("injected claim directory open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(archive, "open_directory_no_follow", fail_claim_setup)
    before_fds = len(os.listdir("/dev/fd"))
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    after_fds = len(os.listdir("/dev/fd"))
    assert code == 1
    assert before_fds == after_fds
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert not source.exists()
    source_residue = [
        item
        for item in terminal["residue"]
        if ".archive-delete-" in item or ".archive-claims-" in item
    ]
    assert any(".archive-delete-" in item for item in source_residue)
    if fault == "claim-open":
        assert any(".archive-claims-" in item for item in source_residue)
    else:
        assert not any(".archive-claims-" in item for item in source_residue)
    assert all((config.object_store_root / item).exists() for item in source_residue)


def test_tombstone_removal_refuses_cross_mount_descendant(tmp_path: Path) -> None:
    root = tmp_path / "tombstone"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "payload").write_bytes(b"keep")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    claim = tmp_path / "claims"
    claim.mkdir()
    claim_fd = os.open(claim, os.O_RDONLY | os.O_DIRECTORY)
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
                claim_fd=claim_fd,
                claim_label="claims",
                rename_impl=lambda src_fd, src, dst_fd, dst: os.rename(
                    src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd
                ),
            )
    finally:
        os.close(claim_fd)
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
    locators, failures = archive.discover_candidate_locators(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
    )
    assert len(locators) == 1 and failures == []
    monkeypatch.setattr(
        archive,
        "discover_candidate_locators",
        lambda *_args, **_kwargs: (
            locators,
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


@pytest.mark.parametrize("age", [0, 13])
def test_invalid_minimum_age_never_falls_back(tmp_path: Path, age: int) -> None:
    config = _config(tmp_path, enforce=False)
    config = archive.MoverConfig(**{**config.__dict__, "minimum_age_days": age})
    with pytest.raises(archive.ArchiveMoverError, match="at least 14"):
        archive.run(config, mount_id_provider=_mount_id, rename_impl=_rename_noreplace)


def test_minimum_age_equal_to_fourteen_day_policy_is_accepted(tmp_path: Path) -> None:
    config = _config(tmp_path, enforce=False)
    config = archive.MoverConfig(**{**config.__dict__, "minimum_age_days": 14})

    receipt, exit_code = archive.run(
        config, mount_id_provider=_mount_id, rename_impl=_rename_noreplace
    )

    assert exit_code == 0
    assert receipt["minimum_age_days"] == 14
    assert receipt["outcome"] == "success"


def test_receipt_schema_positive_and_negative() -> None:
    schema = json.loads((_ROOT / "schemas/product_archive_receipt.schema.json").read_text())
    example = json.loads((_ROOT / "schemas/examples/product_archive_receipt.example.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    broken = dict(example)
    broken.pop("terminals")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(schema).validate(broken)
    below_policy = dict(example)
    below_policy["minimum_age_days"] = 13
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(schema).validate(below_policy)


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
        lambda value: value["deferred"][0].update({"source_bytes": 99}),
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
        "eligibility_end": "2026-01-01T00:00:00Z",
        "validation_state": "validated",
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


def test_leaf_rename_parent_fsync_failure_reports_real_destination_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    source = root / "source"
    destination = root / "destination"
    source.mkdir(parents=True)
    real_fsync = os.fsync
    failed = False

    def fail_first_parent_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("durability unknown")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_parent_fsync)
    with pytest.raises(archive.ArchiveMoverError, match="parent fsync failed") as caught:
        archive._rename_leaf(source, destination, root, _rename_noreplace, _mount_id)
    assert caught.value.indeterminate
    assert caught.value.residue == ("destination",)
    assert destination.is_dir()
    assert not source.exists()


@pytest.mark.parametrize("path_kind", ["publish", "quarantine"])
def test_indeterminate_leaf_rename_receipt_uses_destination_without_false_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_kind: str
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)
    if path_kind == "quarantine":
        first, code = archive.run(
            config,
            now=datetime(2026, 7, 11, tzinfo=UTC),
            mount_id_provider=_mount_id,
            rename_impl=_rename_noreplace,
        )
        assert code == 0
        _forcing(config)
        final = config.archive_root / Path(first["candidates"][0]["archive_path"]).parent
        (final / "archive.tar.zst").write_bytes(b"corrupt")
    original = archive._rename_leaf

    def rename_then_indeterminate(source, destination, containment_root, rename_impl, provider, **kwargs):
        original(source, destination, containment_root, rename_impl, provider, **kwargs)
        if (path_kind == "publish" and ".staging" in source.parts) or (
            path_kind == "quarantine" and ".quarantine" in destination.parts
        ):
            raise archive.ArchiveMoverError(
                "rename completed but parent fsync failed",
                indeterminate=True,
                residue=(destination.relative_to(containment_root).as_posix(),),
            )

    monkeypatch.setattr(archive, "_rename_leaf", rename_then_indeterminate)
    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    terminal = receipt["terminals"][0]
    assert terminal["status"] == "indeterminate"
    assert all(not path.startswith(".staging/") for path in terminal["residue"])
    if path_kind == "quarantine":
        assert any(path.startswith(".quarantine/") for path in terminal["residue"])
        assert all(event["kind"] != "quarantined" for event in receipt["events"])


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
        "eligibility_end": "2026-01-01T00:00:00Z",
        "validation_state": "validated",
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


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("relative-wrapper-path", "wrapper paths must be absolute"),
        ("env-mode", "env file must have mode 0600"),
        ("env-symlink", "env file must be a regular non-symlink file"),
        ("missing-object-prefix", "canonical object-store prefix must be configured"),
        ("relative-zstd", "zstd path must be absolute"),
        ("missing-zstd", "zstd executable is unavailable or unsafe"),
        ("symlink-zstd", "zstd executable is unavailable or unsafe"),
    ],
)
def test_product_archive_wrapper_rejects_unsafe_runtime_contract(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    wrapper = _ROOT / "scripts/node27_product_archive_once.sh"
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
    entrypoint = tmp_path / "archive.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    zstd = tmp_path / "zstd"
    zstd.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    zstd.chmod(0o700)

    env_file = tmp_path / ("bad-mode.env" if case == "env-mode" else "archive.env")
    configured_zstd = str(zstd)
    if case == "relative-zstd":
        configured_zstd = "zstd"
    elif case == "missing-zstd":
        configured_zstd = str(tmp_path / "missing-zstd")
    elif case == "symlink-zstd":
        zstd_link = tmp_path / "zstd-link"
        zstd_link.symlink_to(zstd)
        configured_zstd = str(zstd_link)
    env_lines = [
        f"NODE27_PRODUCT_ARCHIVE_OBJECT_STORE_ROOT={tmp_path / 'object-store'}",
        f"NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT={tmp_path / 'archive'}",
        f"NODE27_PRODUCT_ARCHIVE_RECEIPT={tmp_path / 'receipt.json'}",
        f"NODE27_PRODUCT_ARCHIVE_LOCK_FILE={tmp_path / 'archive.lock'}",
        f"NODE27_PRODUCT_ARCHIVE_ZSTD={configured_zstd}",
    ]
    if case != "missing-object-prefix":
        env_lines.append("OBJECT_STORE_PREFIX=s3://nhms")
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    if case == "env-symlink":
        target = tmp_path / "real.env"
        env_file.rename(target)
        env_file.symlink_to(target)

    process_env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NODE27_PRODUCT_ARCHIVE_ENV_FILE": (
            "relative.env" if case == "relative-wrapper-path" else str(env_file)
        ),
        "NODE27_PRODUCT_ARCHIVE_PYTHON": str(python_bin),
        "NODE27_PRODUCT_ARCHIVE_SCRIPT": str(entrypoint),
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


# ---------------------------------------------------------------------------
# #849: archive-root free-space refusal gate.
# The refusal must fire BEFORE candidate discovery/mutation. See the
# governing invariant + Invariant Matrix in
# openspec/changes/tier-node27-timeseries-storage/design.md
# under the "Workflow Fixture: Issue #849" section.


def _config_with_watermarks(
    tmp_path: Path,
    *,
    enforce: bool,
    warn_bytes: int | None,
    refuse_bytes: int | None,
) -> archive.MoverConfig:
    base = _config(tmp_path, enforce=enforce)
    return archive.MoverConfig(
        **{
            **base.__dict__,
            "free_space_warn_bytes": warn_bytes,
            "free_space_refuse_bytes": refuse_bytes,
        }
    )


def _fake_disk_usage(free: int) -> object:
    return type("U", (), {"total": free * 4, "used": free * 3, "free": free})()


def test_enforce_below_refuse_watermark_refuses_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_watermarks(
        tmp_path, enforce=True, warn_bytes=2_000, refuse_bytes=1_000
    )
    source_leaf = _forcing(config)
    # Free space (500) is below refuse (1000).
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: _fake_disk_usage(500))

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not run after free-space refusal")

    monkeypatch.setattr(archive, "discover_candidate_locators", forbidden)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["outcome"] == "refused_free_space"
    assert receipt["candidates"] == []
    assert receipt["selected"] == []
    assert receipt["deferred"] == []
    assert receipt["terminals"] == []
    assert receipt["events"] == []
    assert receipt["discovery_failures"] == []
    assert receipt["free_space"]["band"] == "refuse"
    assert receipt["free_space"]["free_bytes"] == 500
    assert receipt["free_space"]["warn_bytes"] == 2_000
    assert receipt["free_space"]["refuse_bytes"] == 1_000
    assert receipt["free_space"]["archive_root"] == str(config.archive_root)
    # Source untouched.
    assert source_leaf.exists()
    # No archive tree published.
    assert list(config.archive_root.iterdir()) == []
    # Refusal is receipt-schema-valid.
    jsonschema.Draft7Validator(
        json.loads((_ROOT / "schemas/product_archive_receipt.schema.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    ).validate(receipt)


def test_dry_run_below_refuse_watermark_also_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_watermarks(
        tmp_path, enforce=False, warn_bytes=2_000, refuse_bytes=1_000
    )
    _forcing(config)
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: _fake_disk_usage(500))

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not run after free-space refusal")

    monkeypatch.setattr(archive, "discover_candidate_locators", forbidden)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 1
    assert receipt["outcome"] == "refused_free_space"
    assert receipt["mode"] == "dry-run"


def test_enforce_between_refuse_and_warn_proceeds_with_warn_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_watermarks(
        tmp_path, enforce=True, warn_bytes=2_000, refuse_bytes=1_000
    )
    _forcing(config)
    # Free (1500) >= refuse (1000) and < warn (2000).
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: _fake_disk_usage(1_500))

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["outcome"] == "success"
    assert receipt["free_space"]["band"] == "warn"


def test_enforce_at_or_above_warn_watermark_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_watermarks(
        tmp_path, enforce=True, warn_bytes=2_000, refuse_bytes=1_000
    )
    _forcing(config)
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda path: _fake_disk_usage(5_000))

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["outcome"] == "success"
    assert receipt["free_space"]["band"] == "clean"


def test_watermarks_unset_preserves_legacy_run_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, enforce=True)
    _forcing(config)

    # Even a zero-free-space filesystem MUST NOT refuse when watermarks are
    # unset — backwards-compatible with pre-#849 deployments.
    def would_be_refuse(path):
        return _fake_disk_usage(0)

    monkeypatch.setattr(archive.shutil, "disk_usage", would_be_refuse)

    receipt, code = archive.run(
        config,
        now=datetime(2026, 7, 11, tzinfo=UTC),
        mount_id_provider=_mount_id,
        rename_impl=_rename_noreplace,
    )
    assert code == 0
    assert receipt["outcome"] == "success"
    assert "free_space" not in receipt


@pytest.mark.parametrize(
    ("warn", "refuse"),
    [
        (None, "1000"),
        ("2000", None),
    ],
)
def test_partial_watermark_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, warn: str | None, refuse: str | None
) -> None:
    for key in (
        "NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES",
        "NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    if warn is not None:
        monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", warn)
    if refuse is not None:
        monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", refuse)
    with pytest.raises(archive.ArchiveMoverError, match="watermarks"):
        archive._parse_free_space_watermarks(os.environ)


@pytest.mark.parametrize(
    ("warn", "refuse"),
    [
        ("", "1000"),
        ("2000", ""),
        ("not-an-int", "1000"),
        ("2000", "not-an-int"),
        ("0", "0"),
        ("-1", "-2"),
        ("100", "200"),  # refuse >= warn
        ("100", "100"),
    ],
)
def test_invalid_watermark_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch, warn: str, refuse: str
) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", warn)
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", refuse)
    with pytest.raises(archive.ArchiveMoverError):
        archive._parse_free_space_watermarks(os.environ)


def test_valid_watermark_env_parses_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", "2000")
    monkeypatch.setenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", "1000")
    warn, refuse = archive._parse_free_space_watermarks(os.environ)
    assert (warn, refuse) == (2000, 1000)


def test_missing_watermark_env_is_backwards_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES", raising=False)
    monkeypatch.delenv("NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES", raising=False)
    assert archive._parse_free_space_watermarks(os.environ) == (None, None)


def test_refusal_receipt_semantics_reject_extra_terminals() -> None:
    receipt = {
        "schema_version": archive.SCHEMA_VERSION,
        "generated_at": "2026-07-11T00:00:00Z",
        "mode": "enforce",
        "cutoff": "2026-05-27T00:00:00Z",
        "minimum_age_days": 45,
        "per_tick_bound": 8,
        "outcome": "refused_free_space",
        "validation_attempts": 0,
        "candidates": [],
        "selected": [],
        "deferred": [],
        "terminals": [
            {
                "identity": {
                    "lane": "runs",
                    "source": "gfs",
                    "cycle_identity": "2026010100",
                    "cycle_time": "2026-01-01T00:00:00Z",
                    "run_id": "r",
                },
                "status": "planned",
                "reason": "should not be here",
                "source_bytes": 0,
                "archive_bytes": 0,
                "residue": [],
            }
        ],
        "events": [],
        "discovery_failures": [],
        "bytes": {"source": 0, "archived": 0},
        "free_space": {
            "archive_root": "/tmp/archive",
            "free_bytes": 100,
            "warn_bytes": 2_000,
            "refuse_bytes": 1_000,
            "band": "refuse",
        },
    }
    with pytest.raises(archive.ArchiveMoverError, match="must not carry terminals"):
        archive.validate_receipt_semantics(receipt)


def test_refusal_receipt_requires_free_below_refuse() -> None:
    receipt = {
        "schema_version": archive.SCHEMA_VERSION,
        "generated_at": "2026-07-11T00:00:00Z",
        "mode": "enforce",
        "cutoff": "2026-05-27T00:00:00Z",
        "minimum_age_days": 45,
        "per_tick_bound": 8,
        "outcome": "refused_free_space",
        "validation_attempts": 0,
        "candidates": [],
        "selected": [],
        "deferred": [],
        "terminals": [],
        "events": [],
        "discovery_failures": [],
        "bytes": {"source": 0, "archived": 0},
        "free_space": {
            "archive_root": "/tmp/archive",
            "free_bytes": 5_000,  # Above refuse — invalid for refusal receipt.
            "warn_bytes": 2_000,
            "refuse_bytes": 1_000,
            "band": "refuse",
        },
    }
    with pytest.raises(archive.ArchiveMoverError, match="free_bytes must be"):
        archive.validate_receipt_semantics(receipt)


def test_refused_free_space_receipt_schema_positive_example() -> None:
    schema = json.loads((_ROOT / "schemas/product_archive_receipt.schema.json").read_text())
    receipt = {
        "schema_version": archive.SCHEMA_VERSION,
        "generated_at": "2026-07-11T00:00:00Z",
        "mode": "enforce",
        "cutoff": "2026-05-27T00:00:00Z",
        "minimum_age_days": 45,
        "per_tick_bound": 8,
        "outcome": "refused_free_space",
        "validation_attempts": 0,
        "candidates": [],
        "selected": [],
        "deferred": [],
        "terminals": [],
        "events": [],
        "discovery_failures": [],
        "bytes": {"source": 0, "archived": 0},
        "free_space": {
            "archive_root": "/tmp/archive",
            "free_bytes": 500,
            "warn_bytes": 2_000,
            "refuse_bytes": 1_000,
            "band": "refuse",
        },
    }
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)


def test_disk_usage_error_at_free_space_measurement_is_operational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_watermarks(
        tmp_path, enforce=True, warn_bytes=2_000, refuse_bytes=1_000
    )

    def raiser(path):
        raise OSError("disk usage unavailable")

    monkeypatch.setattr(archive.shutil, "disk_usage", raiser)
    with pytest.raises(archive.ArchiveOperationalError):
        archive._measure_archive_free_space(config)
