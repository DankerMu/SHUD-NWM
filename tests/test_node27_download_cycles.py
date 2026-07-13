from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_download_cycles as downloader

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "node27_download_once.sh"
ENV_EXAMPLE = REPO_ROOT / "infra" / "env" / "node27-download.example"
SYSTEMD_DOWNLOAD_SERVICE = REPO_ROOT / "infra" / "systemd" / "nhms-node27-download.service"
SYSTEMD_DOWNLOAD_TIMER = REPO_ROOT / "infra" / "systemd" / "nhms-node27-download.timer"


def _prepare_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    object_store_root = tmp_path / "object-store"
    workspace_root = tmp_path / "workspace"
    log_root = tmp_path / "logs"
    fake_bin = tmp_path / "bin"
    for path in (object_store_root, workspace_root, log_root, fake_bin):
        path.mkdir(parents=True, exist_ok=True)
    cdo = fake_bin / "cdo"
    cdo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cdo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NHMS_NODE27_DOWNLOAD_ROLE", downloader.DOWNLOAD_ROLE)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", downloader.DOWNLOAD_ROLE)
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store_root))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("NODE27_DOWNLOAD_LOG_ROOT", str(log_root))
    monkeypatch.setenv("NODE27_DOWNLOAD_LOCK_PATH", str(tmp_path / "download.lock"))
    monkeypatch.setenv("NHMS_NODE27_DOWNLOAD_ALLOWED_CYCLE_HOURS_UTC", "0,12")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_SOUTH", "8")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_NORTH", "64")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_WEST", "63")
    monkeypatch.setenv("NHMS_DOWNLOAD_BBOX_EAST", "145")
    monkeypatch.setenv("NODE27_DOWNLOAD_ALLOWED_DATABASE_ENDPOINTS", "127.0.0.1:55432,localhost:55432")
    return object_store_root, workspace_root, log_root, fake_bin


def _run_main(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, dict[str, Any], str]:
    rc = downloader.main(args)
    rendered = capsys.readouterr().out
    return rc, json.loads(rendered), rendered


def _blocker_codes(summary: dict[str, Any]) -> set[str]:
    preflight = summary.get("preflight") or {}
    direct = summary.get("blockers") or []
    return {blocker["code"] for blocker in [*preflight.get("blockers", []), *direct]}


def _fail_if_called(*_args: object, **_kwargs: object) -> downloader.SourceDownloadResult:
    raise AssertionError("download command must not run before preflight passes")


def _write_raw_manifest(object_store_root: Path, source: str, compact_cycle: str) -> None:
    manifest = object_store_root / "raw" / source / compact_cycle / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}\n", encoding="utf-8")


def test_preflight_rejects_node22_historical_database_before_download_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26T12:00:00Z",
            "--source",
            "GFS",
            "--database-url",
            "postgresql://node22_writer:writer-secret@10.0.2.100:55433/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == downloader.PREFLIGHT_BLOCKED_RC
    assert summary["status"] == "preflight_blocked"
    assert {
        "DATABASE_URL_NODE22_HISTORICAL_ENDPOINT",
        "DATABASE_URL_ENDPOINT_NOT_NODE27",
    }.issubset(_blocker_codes(summary))
    assert "writer-secret" not in rendered


def test_preflight_rejects_display_readonly_runtime_and_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "display_readonly")
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26T12:00:00Z",
            "--database-url",
            "postgresql://nhms_display_ro:readonly-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == downloader.PREFLIGHT_BLOCKED_RC
    assert {
        "DOWNLOAD_DISPLAY_READONLY_ROLE_FORBIDDEN",
        "DATABASE_URL_READONLY_IDENTITY",
    }.issubset(_blocker_codes(summary))
    assert "readonly-secret" not in rendered


def test_preflight_ready_summary_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, fake_bin = _prepare_env(monkeypatch, tmp_path)

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26 12:00:00+00:00",
            "--source",
            "GFS",
            "--database-url",
            "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
            "--preflight-only",
        ],
    )

    assert rc == 0
    assert summary["schema"] == downloader.DOWNLOAD_SUMMARY_SCHEMA
    assert summary["status"] == "preflight_ready"
    assert summary["cycle_time"] == "2026-06-26T12:00:00Z"
    assert summary["sources"] == ["GFS"]
    assert summary["preflight"]["status"] == "ready"
    assert summary["preflight"]["database"] == {
        "configured": True,
        "database": "nhms",
        "host": "127.0.0.1",
        "password_present": True,
        "port": 55432,
        "scheme": "postgresql",
        "username_class": "writer_candidate",
        "username_present": True,
    }
    assert summary["preflight"]["toolchain"]["cdo"]["path"] == str(fake_bin / "cdo")
    assert "writer-secret" not in rendered


def test_automatic_cycle_selection_without_raw_seed_uses_latest_allowed_cycle_after_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", "8")

    selected = downloader._select_automatic_cycle_time(
        dict(os.environ),
        now=datetime(2026, 6, 30, 0, 40, tzinfo=UTC),
    )

    assert selected == "2026-06-29T12:00:00Z"


def test_automatic_cycle_selection_uses_first_raw_continuity_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_store_root, _workspace_root, _log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", "8")

    _write_raw_manifest(object_store_root, "GFS", "2026062118")
    _write_raw_manifest(object_store_root, "IFS", "2026062118")
    for compact_cycle in ("2026062600", "2026062612", "2026062700", "2026062712"):
        _write_raw_manifest(object_store_root, "GFS", compact_cycle)
        _write_raw_manifest(object_store_root, "IFS", compact_cycle)
    for compact_cycle in ("2026070200", "2026070212"):
        _write_raw_manifest(object_store_root, "GFS", compact_cycle)
        _write_raw_manifest(object_store_root, "IFS", compact_cycle)

    selected = downloader._select_automatic_cycle_time(
        dict(os.environ),
        now=datetime(2026, 7, 3, 0, 40, tzinfo=UTC),
    )

    assert selected == "2026-06-28T00:00:00Z"


def test_automatic_cycle_selection_treats_any_selected_source_gap_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_store_root, _workspace_root, _log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", "0")
    _write_raw_manifest(object_store_root, "GFS", "2026062600")
    _write_raw_manifest(object_store_root, "IFS", "2026062600")
    _write_raw_manifest(object_store_root, "GFS", "2026062612")

    selected = downloader._select_automatic_cycle_time(
        dict(os.environ),
        now=datetime(2026, 6, 26, 12, 40, tzinfo=UTC),
    )

    assert selected == "2026-06-26T12:00:00Z"


def test_automatic_cycle_selection_probes_window_without_raw_directory_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    object_store_root, _workspace_root, _log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", "0")
    _write_raw_manifest(object_store_root, "GFS", "2026062600")
    _write_raw_manifest(object_store_root, "IFS", "2026062600")
    _write_raw_manifest(object_store_root, "GFS", "2026062612")
    raw_source_dirs = {object_store_root / "raw" / "GFS", object_store_root / "raw" / "IFS"}
    real_iterdir = Path.iterdir

    def fail_raw_source_iterdir(path: Path) -> Iterator[Path]:
        if path in raw_source_dirs:
            pytest.fail(f"automatic continuity selection must not scan {path}")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_raw_source_iterdir)

    selected = downloader._select_automatic_cycle_time(
        dict(os.environ),
        now=datetime(2026, 6, 26, 12, 40, tzinfo=UTC),
    )

    assert selected == "2026-06-26T12:00:00Z"


def test_missing_cycle_time_defaults_to_automatic_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setattr(downloader, "_select_automatic_cycle_time", lambda _env, **_kwargs: "2026-06-29T12:00:00Z")

    def fake_download(source: str, cycle_time: str, _env: dict[str, str]) -> downloader.SourceDownloadResult:
        return downloader.SourceDownloadResult(
            source=source,
            cycle_time=cycle_time,
            status="downloaded",
            return_code=0,
            command=[f"nhms-{source.lower()}", "download"],
            result={"status": "raw_complete"},
            stdout_tail='{"status":"raw_complete"}',
            stderr_tail="",
        )

    monkeypatch.setattr(downloader, "run_source_download", fake_download)

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--source",
            "GFS",
            "--database-url",
            "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["cycle_time"] == "2026-06-29T12:00:00Z"
    assert summary["cycle_time_selection"] == "automatic"
    assert summary["downloads"]["processed"] == 1
    assert "writer-secret" not in rendered


def test_one_source_failure_isolated_while_other_source_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)

    def fake_download(source: str, cycle_time: str, _env: dict[str, str]) -> downloader.SourceDownloadResult:
        if source == "GFS":
            return downloader.SourceDownloadResult(
                source=source,
                cycle_time=cycle_time,
                status="failed",
                return_code=1,
                command=["nhms-gfs", "download"],
                result={"error_code": "HTTP_403"},
                stdout_tail="",
                stderr_tail='{"p\\u0061ssword": "download-secret"}',
            )
        return downloader.SourceDownloadResult(
            source=source,
            cycle_time=cycle_time,
            status="downloaded",
            return_code=0,
            command=["nhms-ifs", "download"],
            result={"status": "raw_complete", "files": 2},
            stdout_tail='{"status":"raw_complete"}',
            stderr_tail="",
        )

    monkeypatch.setattr(downloader, "run_source_download", fake_download)

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26T12:00:00Z",
            "--database-url",
            "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == 1
    assert summary["status"] == "completed_with_failures"
    assert summary["downloads"]["processed"] == 2
    assert summary["downloads"]["downloaded"] == 1
    assert summary["downloads"]["failed"] == 1
    assert [detail["source"] for detail in summary["downloads"]["details"]] == ["GFS", "IFS"]
    assert "download-secret" not in rendered
    assert "writer-secret" not in rendered


def test_grib_env_root_bin_is_added_to_download_subprocess_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    grib_bin = tmp_path / "nhms-grib" / "bin"
    grib_bin.mkdir(parents=True)
    cdo = grib_bin / "cdo"
    cdo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cdo.chmod(0o755)
    monkeypatch.setenv("NHMS_GRIB_ENV_ROOT", str(tmp_path / "nhms-grib"))
    monkeypatch.setenv("PATH", "/usr/bin")

    seen_path: list[str] = []

    def fake_download(source: str, cycle_time: str, env: dict[str, str]) -> downloader.SourceDownloadResult:
        seen_path.append(env["PATH"])
        return downloader.SourceDownloadResult(
            source=source,
            cycle_time=cycle_time,
            status="downloaded",
            return_code=0,
            command=["nhms-gfs", "download"],
            result={"status": "raw_complete", "files": 1},
            stdout_tail='{"status":"raw_complete"}',
            stderr_tail="",
        )

    monkeypatch.setattr(downloader, "run_source_download", fake_download)

    rc, summary, _rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26T12:00:00Z",
            "--source",
            "GFS",
            "--database-url",
            "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["preflight"]["toolchain"]["cdo"]["path"] == str(cdo)
    assert seen_path == [f"{grib_bin}{os.pathsep}/usr/bin"]


def test_lock_held_blocks_without_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    @contextmanager
    def fake_lock(_lock_path: str) -> Iterator[bool]:
        yield False

    monkeypatch.setattr(downloader, "download_lock", fake_lock)

    rc, summary, _rendered = _run_main(
        capsys,
        [
            "--cycle-time",
            "2026-06-26T12:00:00Z",
            "--database-url",
            "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
            "--object-store-root",
            str(object_store_root),
            "--workspace-root",
            str(workspace_root),
            "--log-root",
            str(log_root),
            "--lock-path",
            str(tmp_path / "download.lock"),
        ],
    )

    assert rc == downloader.LOCK_BLOCKED_RC
    assert summary["status"] == "lock_blocked"
    assert _blocker_codes(summary) == {"NODE27_DOWNLOAD_LOCK_HELD"}


def test_wrapper_and_env_contract_do_not_source_display_env() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    service = SYSTEMD_DOWNLOAD_SERVICE.read_text(encoding="utf-8")
    timer = SYSTEMD_DOWNLOAD_TIMER.read_text(encoding="utf-8")

    assert "node27-download.env" in wrapper
    assert "DOWNLOAD_ENV_DISPLAY_RUNTIME_FORBIDDEN" in wrapper
    assert "NODE27_DOWNLOAD_CYCLE_TIME_MISSING" not in wrapper
    assert "infra/env/display.env" not in wrapper
    assert "NHMS_NODE27_DOWNLOAD_ROLE=node27_data_plane_download" in env_example
    assert "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS=8" in env_example
    assert "127.0.0.1:55432" in env_example
    assert "55433" in env_example
    assert "scripts/node27_download_once.sh" in service
    assert "OnUnitActiveSec=30min" in timer
