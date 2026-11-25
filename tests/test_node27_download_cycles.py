from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_download_cycles as downloader
from services.orchestrator.source_cycle_raw_manifest import nfs_raw_manifest_readiness

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "node27_download_once.sh"
ENV_EXAMPLE = REPO_ROOT / "infra" / "env" / "node27-download.example"
SYSTEMD_DOWNLOAD_SERVICE = REPO_ROOT / "infra" / "systemd" / "nhms-node27-download@.service"
SYSTEMD_DOWNLOAD_TIMER = REPO_ROOT / "infra" / "systemd" / "nhms-node27-download@.timer"


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


def _clear_selection_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell from steering automatic selection."""
    for name in (
        "NODE27_DOWNLOAD_CONTINUITY_START_TIME",
        "NODE27_DOWNLOAD_CONTINUITY_LOOKBACK_DAYS",
        "NHMS_NODE27_DOWNLOAD_SOURCES",
        "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_GFS",
        "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_IFS",
    ):
        monkeypatch.delenv(name, raising=False)


# Directory spelling the live object store uses; macOS is case-insensitive, so
# writing another spelling would make the readiness rule resolve a different
# manifest key there than on Linux.
LIVE_RAW_SOURCE_DIRS = {"GFS": "gfs", "IFS": "IFS"}


def _write_complete_cycle(
    object_store_root: Path,
    source: str,
    compact_cycle: str,
    *,
    prefix: str = "",
    file_names: tuple[str, ...] = ("f000.grib2", "f003.grib2"),
) -> list[Path]:
    """Write a cycle the raw manifest readiness rule accepts; return its entry files."""
    source_dir = LIVE_RAW_SOURCE_DIRS[source]
    cycle_dir = f"raw/{source_dir}/{compact_cycle}"
    cycle_time = datetime.strptime(compact_cycle, "%Y%m%d%H").replace(tzinfo=UTC)
    entries: list[dict[str, Any]] = []
    files: list[Path] = []
    for forecast_hour, name in enumerate(file_names):
        local_key = f"{cycle_dir}/{name}"
        path = object_store_root / local_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"grib-bytes")
        files.append(path)
        entries.append({"local_key": local_key, "forecast_hour": forecast_hour})
    manifest_key = f"{cycle_dir}/manifest.json"
    # Same rule as the adapters' object store: a bare key when no prefix is set.
    manifest_uri = f"{prefix.rstrip('/')}/{manifest_key}" if prefix else manifest_key
    (object_store_root / manifest_key).write_text(
        json.dumps(
            {
                "source_id": source_dir,
                "cycle_time": cycle_time.isoformat(),
                "manifest_uri": manifest_uri,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return files


def _prepare_selection_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    delay_hours: str = "8",
    object_store_prefix: str | None = None,
) -> Path:
    object_store_root, _workspace_root, _log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", delay_hours)
    _clear_selection_overrides(monkeypatch)
    monkeypatch.delenv("NODE27_DOWNLOAD_CYCLE_TIME", raising=False)
    if object_store_prefix is None:
        monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    else:
        monkeypatch.setenv("OBJECT_STORE_PREFIX", object_store_prefix)
    return object_store_root


# With hours 0,12 and an 8 h delay this clock makes 2026-07-02T12 the latest
# allowed cycle.
SELECTION_NOW = datetime(2026, 7, 3, 0, 40, tzinfo=UTC)
LATEST = "2026070212"
LATEST_MINUS_1 = "2026070200"
LATEST_MINUS_2 = "2026070112"
LATEST_MINUS_3 = "2026070100"


def _iso(compact_cycle: str) -> str:
    return datetime.strptime(compact_cycle, "%Y%m%d%H").strftime("%Y-%m-%dT%H:00:00Z")


def _write_complete_for_both(object_store_root: Path, *compact_cycles: str, prefix: str = "") -> None:
    for compact_cycle in compact_cycles:
        for source in ("GFS", "IFS"):
            _write_complete_cycle(object_store_root, source, compact_cycle, prefix=prefix)


def _select(sources: tuple[str, ...] | None = None) -> str:
    return downloader._select_automatic_cycle_time(dict(os.environ), now=SELECTION_NOW, sources=sources)


def _select_with_detail(sources: tuple[str, ...] | None = None) -> tuple[str, dict[str, Any]]:
    return downloader._select_automatic_cycle(dict(os.environ), now=SELECTION_NOW, sources=sources)


READY = {"status": "ready", "reason": None}


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
    _clear_selection_overrides(monkeypatch)

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
    monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    _clear_selection_overrides(monkeypatch)

    _write_complete_cycle(object_store_root, "GFS", "2026062118")
    _write_complete_cycle(object_store_root, "IFS", "2026062118")
    for compact_cycle in ("2026062600", "2026062612", "2026062700", "2026062712"):
        _write_complete_cycle(object_store_root, "GFS", compact_cycle)
        _write_complete_cycle(object_store_root, "IFS", compact_cycle)
    for compact_cycle in ("2026070200", "2026070212"):
        _write_complete_cycle(object_store_root, "GFS", compact_cycle)
        _write_complete_cycle(object_store_root, "IFS", compact_cycle)

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
    monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    _clear_selection_overrides(monkeypatch)
    _write_complete_cycle(object_store_root, "GFS", "2026062600")
    _write_complete_cycle(object_store_root, "IFS", "2026062600")
    _write_complete_cycle(object_store_root, "GFS", "2026062612")

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
    monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    _clear_selection_overrides(monkeypatch)
    _write_complete_cycle(object_store_root, "GFS", "2026062600")
    _write_complete_cycle(object_store_root, "IFS", "2026062600")
    _write_complete_cycle(object_store_root, "GFS", "2026062612")
    raw_source_dirs = {
        object_store_root / "raw" / "GFS",
        object_store_root / "raw" / "gfs",
        object_store_root / "raw" / "IFS",
    }
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


@pytest.mark.parametrize("prefix", ["", "s3://nhms"])
@pytest.mark.parametrize("source", ["GFS", "IFS"])
def test_complete_cycle_fixture_is_ready_under_raw_manifest_readiness_rule(
    tmp_path: Path, source: str, prefix: str
) -> None:
    _write_complete_cycle(tmp_path, source, LATEST, prefix=prefix)

    readiness = nfs_raw_manifest_readiness(
        source_id=source,
        cycle_time=datetime(2026, 7, 2, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        object_store_prefix=prefix,
    )

    assert (readiness["status"], readiness["reason"]) == ("ready", None)


@pytest.mark.parametrize("damage", ["removed", "emptied"])
def test_automatic_selection_re_drives_older_cycle_with_incomplete_raw_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_2, LATEST)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_1)
    ifs_files = _write_complete_cycle(object_store_root, "IFS", LATEST_MINUS_1)
    if damage == "removed":
        ifs_files[1].unlink()
    else:
        ifs_files[1].write_bytes(b"")

    assert _select() == _iso(LATEST_MINUS_1)
    selected, detail = _select_with_detail()
    assert selected == _iso(LATEST_MINUS_1)
    reason = "raw_files_missing" if damage == "removed" else "raw_files_empty"
    assert detail["not_ready_cycles"] == [
        {"cycle_time": _iso(LATEST_MINUS_1), "sources": {"GFS": READY, "IFS": {"status": "invalid", "reason": reason}}}
    ]


def test_automatic_selection_serves_not_ready_latest_before_older_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST_MINUS_1)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_2)
    for source in ("GFS", "IFS"):
        for path in _write_complete_cycle(object_store_root, source, LATEST):
            path.unlink()

    assert _select() == _iso(LATEST)


def test_automatic_selection_serves_oldest_not_ready_cycle_once_latest_is_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_2)
    _write_complete_cycle(object_store_root, "IFS", LATEST_MINUS_2)[0].unlink()
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_1)

    assert _select() == _iso(LATEST_MINUS_2)


def test_automatic_selection_cycle_without_manifest_does_not_starve_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST_MINUS_1)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_2)

    assert _select() == _iso(LATEST)


def test_automatic_selection_complete_chain_selects_latest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST_MINUS_2, LATEST_MINUS_1, LATEST)

    assert _select() == _iso(LATEST)
    assert _select_with_detail() == (_iso(LATEST), {"latest_allowed_cycle_time": _iso(LATEST), "not_ready_cycles": []})


def _write_older_cycle_not_ready_for_ifs_only(object_store_root: Path) -> None:
    _write_complete_for_both(object_store_root, LATEST_MINUS_2, LATEST)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_1)
    _write_complete_cycle(object_store_root, "IFS", LATEST_MINUS_1)[0].unlink()


def test_automatic_selection_older_cycle_not_ready_for_one_source_is_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)

    assert _select() == _iso(LATEST_MINUS_1)
    _selected, detail = _select_with_detail()
    assert detail["not_ready_cycles"] == [
        {
            "cycle_time": _iso(LATEST_MINUS_1),
            "sources": {"GFS": READY, "IFS": {"status": "invalid", "reason": "raw_files_missing"}},
        }
    ]


def test_automatic_selection_ignores_sources_that_are_not_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)

    assert _select(("GFS",)) == _iso(LATEST)
    assert _select_with_detail(("GFS",))[1]["not_ready_cycles"] == []


def _write_latest_not_ready_for_ifs_only(object_store_root: Path) -> None:
    _write_complete_for_both(object_store_root, LATEST_MINUS_1)
    _write_complete_cycle(object_store_root, "GFS", LATEST)
    _write_complete_cycle(object_store_root, "IFS", LATEST)[0].unlink()


def test_automatic_selection_latest_not_ready_for_one_source_is_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_latest_not_ready_for_ifs_only(object_store_root)

    assert _select() == _iso(LATEST)
    _selected, detail = _select_with_detail()
    assert detail["not_ready_cycles"] == [
        {
            "cycle_time": _iso(LATEST),
            "sources": {"GFS": READY, "IFS": {"status": "invalid", "reason": "raw_files_missing"}},
        }
    ]


def test_automatic_selection_judges_manifest_uri_with_environment_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path, object_store_prefix="s3://nhms-test")
    _write_complete_for_both(object_store_root, LATEST, prefix="s3://nhms-test")
    _write_complete_for_both(object_store_root, LATEST_MINUS_1, prefix="s3://other-bucket")

    assert _select() == _iso(LATEST_MINUS_1)
    mismatch = {"status": "invalid", "reason": "manifest_uri_mismatch"}
    _selected, detail = _select_with_detail()
    assert detail["not_ready_cycles"] == [
        {"cycle_time": _iso(LATEST_MINUS_1), "sources": {"GFS": mismatch, "IFS": mismatch}}
    ]


def test_automatic_selection_accepts_cycles_written_with_environment_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path, object_store_prefix="s3://nhms-test")
    _write_complete_for_both(object_store_root, LATEST_MINUS_1, LATEST, prefix="s3://nhms-test")

    assert _select() == _iso(LATEST)
    assert _select_with_detail()[1]["not_ready_cycles"] == []


def _write_gap_before_explicit_continuity_start(object_store_root: Path) -> None:
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST_MINUS_1, LATEST)
    _write_complete_cycle(object_store_root, "GFS", LATEST_MINUS_2)
    _write_complete_cycle(object_store_root, "IFS", LATEST_MINUS_2)[0].unlink()


def test_automatic_selection_explicit_continuity_start_skips_older_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_gap_before_explicit_continuity_start(object_store_root)
    monkeypatch.setenv("NODE27_DOWNLOAD_CONTINUITY_START_TIME", _iso(LATEST_MINUS_1))

    assert _select() == _iso(LATEST)
    probed: list[datetime] = []

    def recording_readiness(**kwargs: Any) -> dict[str, Any]:
        probed.append(kwargs["cycle_time"])
        return nfs_raw_manifest_readiness(**kwargs)

    monkeypatch.setattr(downloader, "nfs_raw_manifest_readiness", recording_readiness)
    _selected, detail = _select_with_detail()
    assert detail["not_ready_cycles"] == []
    assert sorted(set(probed)) == [datetime(2026, 7, 2, 0, tzinfo=UTC), datetime(2026, 7, 2, 12, tzinfo=UTC)]


def test_automatic_selection_readiness_error_counts_as_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_2, LATEST_MINUS_1, LATEST)
    broken_cycle = datetime(2026, 7, 2, 0, tzinfo=UTC)

    def readiness_raising_for_one_cycle(**kwargs: Any) -> dict[str, Any]:
        if kwargs["cycle_time"] == broken_cycle and kwargs["source_id"] == "IFS":
            raise OSError("stale NFS handle")
        return nfs_raw_manifest_readiness(**kwargs)

    monkeypatch.setattr(downloader, "nfs_raw_manifest_readiness", readiness_raising_for_one_cycle)

    assert _select() == _iso(LATEST_MINUS_1)
    _selected, detail = _select_with_detail()
    assert detail["not_ready_cycles"] == [
        {
            "cycle_time": _iso(LATEST_MINUS_1),
            "sources": {"GFS": READY, "IFS": {"status": "error", "reason": "readiness_error"}},
        }
    ]


SELECTION_DETAIL_KEY = "cycle_time_selection_detail"


def _automatic_main_args(tmp_path: Path, object_store_root: Path, *extra: str) -> list[str]:
    return [
        "--source",
        "GFS",
        "--source",
        "IFS",
        "--database-url",
        "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
        "--object-store-root",
        str(object_store_root),
        "--workspace-root",
        str(tmp_path / "workspace"),
        "--log-root",
        str(tmp_path / "logs"),
        "--lock-path",
        str(tmp_path / "download.lock"),
        "--summary-path",
        str(tmp_path / "logs" / "last-summary.json"),
        *extra,
    ]


def _pin_latest_allowed_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = datetime(2026, 7, 2, 12, tzinfo=UTC)
    monkeypatch.setattr(downloader, "_latest_allowed_cycle", lambda _env, *, now=None, sources=None: latest)


def _fake_download_ok(source: str, cycle_time: str, _env: dict[str, str]) -> downloader.SourceDownloadResult:
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


ONE_NOT_READY_DETAIL = {
    "latest_allowed_cycle_time": "2026-07-02T12:00:00Z",
    "not_ready_cycle_count": 1,
    "not_ready_cycles": [
        {
            "cycle_time": "2026-07-02T00:00:00Z",
            "sources": {
                "GFS": {"status": "ready", "reason": None},
                "IFS": {"status": "invalid", "reason": "raw_files_missing"},
            },
        }
    ],
}


def _last_summary(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "logs" / "last-summary.json").read_text(encoding="utf-8"))


def test_pass_summary_lists_not_ready_cycles_of_automatic_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)
    _pin_latest_allowed_cycle(monkeypatch)
    downloaded: list[tuple[str, str]] = []

    def fake_download(source: str, cycle_time: str, env: dict[str, str]) -> downloader.SourceDownloadResult:
        downloaded.append((source, cycle_time))
        return _fake_download_ok(source, cycle_time, env)

    monkeypatch.setattr(downloader, "run_source_download", fake_download)

    rc, summary, _rendered = _run_main(capsys, _automatic_main_args(tmp_path, object_store_root))

    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["cycle_time"] == "2026-07-02T00:00:00Z"
    assert summary["cycle_time_selection"] == "automatic"
    assert downloaded == [("GFS", "2026-07-02T00:00:00Z"), ("IFS", "2026-07-02T00:00:00Z")]
    assert summary[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL
    assert _last_summary(tmp_path)[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL


def test_pass_summary_of_complete_chain_lists_no_not_ready_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_complete_for_both(object_store_root, LATEST_MINUS_3, LATEST_MINUS_2, LATEST_MINUS_1, LATEST)
    _pin_latest_allowed_cycle(monkeypatch)
    monkeypatch.setattr(downloader, "run_source_download", _fake_download_ok)

    rc, summary, _rendered = _run_main(capsys, _automatic_main_args(tmp_path, object_store_root))

    expected_detail = {
        "latest_allowed_cycle_time": "2026-07-02T12:00:00Z",
        "not_ready_cycle_count": 0,
        "not_ready_cycles": [],
    }
    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["cycle_time"] == "2026-07-02T12:00:00Z"
    assert summary["cycle_time_selection"] == "automatic"
    assert summary["downloads"]["processed"] == 2
    assert summary[SELECTION_DETAIL_KEY] == expected_detail
    assert _last_summary(tmp_path)[SELECTION_DETAIL_KEY] == expected_detail


def test_preflight_only_summary_lists_not_ready_cycles_of_automatic_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)
    _pin_latest_allowed_cycle(monkeypatch)
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    rc, summary, _rendered = _run_main(capsys, _automatic_main_args(tmp_path, object_store_root, "--preflight-only"))

    assert rc == 0
    assert summary["status"] == "preflight_ready"
    assert summary["cycle_time"] == "2026-07-02T00:00:00Z"
    assert summary[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL
    assert _last_summary(tmp_path)[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL


def test_lock_blocked_summary_lists_not_ready_cycles_of_automatic_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)
    _pin_latest_allowed_cycle(monkeypatch)
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    @contextmanager
    def fake_lock(_lock_path: str) -> Iterator[bool]:
        yield False

    monkeypatch.setattr(downloader, "download_lock", fake_lock)

    rc, summary, _rendered = _run_main(capsys, _automatic_main_args(tmp_path, object_store_root))

    assert rc == downloader.LOCK_BLOCKED_RC
    assert summary["status"] == "lock_blocked"
    assert summary[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL
    assert _last_summary(tmp_path)[SELECTION_DETAIL_KEY] == ONE_NOT_READY_DETAIL


def test_summary_lists_at_most_twenty_not_ready_cycles_oldest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    # Chain start 13 days before the latest allowed cycle, inside the 14-day
    # lookback window; the 25 cycles between them have no manifest at all.
    _write_complete_for_both(object_store_root, "2026061912", LATEST)
    _pin_latest_allowed_cycle(monkeypatch)
    monkeypatch.setattr(downloader, "run_source_download", _fail_if_called)

    rc, summary, _rendered = _run_main(capsys, _automatic_main_args(tmp_path, object_store_root, "--preflight-only"))

    first_gap = datetime(2026, 6, 20, 0, tzinfo=UTC)
    expected_cycles = [(first_gap + timedelta(hours=12 * index)).strftime("%Y-%m-%dT%H:%M:%SZ") for index in range(20)]
    missing = {"status": "missing", "reason": "manifest_not_found"}
    detail = summary[SELECTION_DETAIL_KEY]
    assert rc == 0
    assert summary["cycle_time"] == "2026-06-20T00:00:00Z"
    assert detail["not_ready_cycle_count"] == 25
    assert [item["cycle_time"] for item in detail["not_ready_cycles"]] == expected_cycles
    assert all(item["sources"] == {"GFS": missing, "IFS": missing} for item in detail["not_ready_cycles"])
    assert expected_cycles[-1] == "2026-06-29T12:00:00Z"


def test_explicit_cycle_time_summary_has_no_selection_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    _write_older_cycle_not_ready_for_ifs_only(object_store_root)
    _pin_latest_allowed_cycle(monkeypatch)
    monkeypatch.setattr(downloader, "run_source_download", _fake_download_ok)

    rc, summary, _rendered = _run_main(
        capsys, _automatic_main_args(tmp_path, object_store_root, "--cycle-time", "2026-06-30T12:00:00Z")
    )

    assert rc == 0
    assert summary["cycle_time"] == "2026-06-30T12:00:00Z"
    assert summary["cycle_time_selection"] == "explicit"
    assert SELECTION_DETAIL_KEY not in summary
    assert SELECTION_DETAIL_KEY not in _last_summary(tmp_path)


def test_missing_cycle_time_defaults_to_automatic_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, workspace_root, log_root, _fake_bin = _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader,
        "_select_automatic_cycle",
        lambda _env, **_kwargs: (
            "2026-06-29T12:00:00Z",
            {"latest_allowed_cycle_time": "2026-06-29T12:00:00Z", "not_ready_cycles": []},
        ),
    )

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
    # The pass runs the default source list; a shell value must not narrow it.
    monkeypatch.delenv("NHMS_NODE27_DOWNLOAD_SOURCES", raising=False)

    def fake_download(source: str, cycle_time: str, _env: dict[str, str]) -> downloader.SourceDownloadResult:
        if source == "GFS":
            return downloader.SourceDownloadResult(
                source=source,
                cycle_time=cycle_time,
                status="failed",
                return_code=1,
                command=["nhms-gfs", "download"],
                result={
                    "error_code": "HTTP_403",
                    r"api\u005fkey": "download-result-secret",
                },
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
    assert "download-result-secret" not in rendered
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
    assert "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS=8" in env_example.splitlines()
    assert "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_GFS=6" in env_example.splitlines()
    assert "127.0.0.1:55432" in env_example
    assert "55433" in env_example
    (exec_start,) = [line for line in service.splitlines() if line.startswith("ExecStart=")]
    assert exec_start.endswith("scripts/node27_download_once.sh %i")
    assert "OnUnitActiveSec=30min" in timer.splitlines()
    assert "Unit=nhms-node27-download@%i.service" in timer.splitlines()
    # No unit left that runs both sources in one pass.
    assert not (REPO_ROOT / "infra" / "systemd" / "nhms-node27-download.service").exists()
    assert not (REPO_ROOT / "infra" / "systemd" / "nhms-node27-download.timer").exists()


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        pytest.param(b"partial progress\n", b"f000.grib2: 100%\xe2\x96\x88\n", id="bytes"),
        pytest.param(None, None, id="none"),
        pytest.param("text out", "text err", id="str"),
    ],
)
def test_download_timeout_yields_structured_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes | str | None, stderr: bytes | str | None
) -> None:
    """#3075: CPython's TimeoutExpired carries BYTES output even under text=True."""

    def time_out(command, **kwargs):
        raise downloader.subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout, stderr=stderr)

    monkeypatch.setattr(downloader.subprocess, "run", time_out)

    result = downloader.run_source_download(
        "IFS", "2026-10-13T00:00:00Z", {"NODE27_DOWNLOAD_COMMAND_TIMEOUT_SECONDS": "5"}
    )

    assert result.status == "failed"
    assert result.return_code == 124
    assert result.result == {"error_code": "DOWNLOAD_TIMEOUT"}
    assert isinstance(result.stdout_tail, str) and isinstance(result.stderr_tail, str)
    if stderr is not None:
        assert "f000.grib2" in result.stderr_tail or "text err" in result.stderr_tail
    json.dumps(result.as_dict())


# ---------------------------------------------------------------------------
# #3237: one pass per source, each under its own delay and its own lock.
# ---------------------------------------------------------------------------

GFS_DELAY_KEY = "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_GFS"
IFS_DELAY_KEY = "NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_IFS"
# With hours 0,12 this clock is six hours and one minute after PER_SOURCE_C3:
# a 6 h delay allows C3, an 8 h delay allows C2.
PER_SOURCE_NOW = datetime(2026, 7, 2, 18, 1, tzinfo=UTC)
PER_SOURCE_C1 = "2026070112"
PER_SOURCE_C2 = "2026070200"
PER_SOURCE_C3 = "2026070212"


def _latest_allowed_for(sources: tuple[str, ...]) -> str:
    _selected, detail = downloader._select_automatic_cycle(dict(os.environ), now=PER_SOURCE_NOW, sources=sources)
    return detail["latest_allowed_cycle_time"]


@pytest.mark.parametrize(
    ("gfs_delay", "shared_delay", "expected"),
    [("6", "8", PER_SOURCE_C3), ("8", "6", PER_SOURCE_C2)],
    ids=["shorter-source-key", "longer-source-key"],
)
def test_source_delay_key_wins_over_shared_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gfs_delay: str, shared_delay: str, expected: str
) -> None:
    _prepare_selection_env(monkeypatch, tmp_path, delay_hours=shared_delay)
    monkeypatch.setenv(GFS_DELAY_KEY, gfs_delay)

    assert _latest_allowed_for(("GFS",)) == _iso(expected)


@pytest.mark.parametrize("gfs_delay", [None, ""], ids=["absent", "empty"])
def test_shared_delay_key_applies_without_source_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gfs_delay: str | None
) -> None:
    _prepare_selection_env(monkeypatch, tmp_path, delay_hours="6")
    if gfs_delay is not None:
        monkeypatch.setenv(GFS_DELAY_KEY, gfs_delay)

    assert _latest_allowed_for(("GFS",)) == _iso(PER_SOURCE_C3)


def test_delay_defaults_to_eight_hours_without_either_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_selection_env(monkeypatch, tmp_path)
    monkeypatch.delenv("NODE27_DOWNLOAD_CYCLE_DELAY_HOURS", raising=False)

    assert _latest_allowed_for(("GFS",)) == _iso(PER_SOURCE_C2)


@pytest.mark.parametrize("gfs_delay", ["soon", "73"], ids=["non-numeric", "out-of-range"])
def test_invalid_source_delay_key_raises_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gfs_delay: str
) -> None:
    _prepare_selection_env(monkeypatch, tmp_path)
    monkeypatch.setenv(GFS_DELAY_KEY, gfs_delay)

    with pytest.raises(ValueError, match=GFS_DELAY_KEY):
        _latest_allowed_for(("GFS",))


def test_invalid_delay_key_of_unselected_source_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_selection_env(monkeypatch, tmp_path)
    monkeypatch.setenv(IFS_DELAY_KEY, "soon")

    assert _latest_allowed_for(("GFS",)) == _iso(PER_SOURCE_C2)


def test_pass_with_several_sources_uses_the_largest_delay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_selection_env(monkeypatch, tmp_path)
    monkeypatch.setenv(GFS_DELAY_KEY, "6")

    assert _latest_allowed_for(("GFS", "IFS")) == _iso(PER_SOURCE_C2)


@pytest.mark.parametrize(
    ("sources", "selected", "latest_allowed"),
    [
        (("GFS",), PER_SOURCE_C3, PER_SOURCE_C3),
        (("IFS",), PER_SOURCE_C1, PER_SOURCE_C2),
        (("GFS", "IFS"), PER_SOURCE_C1, PER_SOURCE_C2),
    ],
    ids=["gfs-alone", "ifs-alone", "both"],
)
def test_selection_across_per_source_passes_on_one_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sources: tuple[str, ...],
    selected: str,
    latest_allowed: str,
) -> None:
    object_store_root = _prepare_selection_env(monkeypatch, tmp_path)
    monkeypatch.setenv(GFS_DELAY_KEY, "6")
    _write_complete_for_both(object_store_root, PER_SOURCE_C2)
    _write_complete_cycle(object_store_root, "GFS", PER_SOURCE_C1)
    _write_complete_cycle(object_store_root, "IFS", PER_SOURCE_C1)[0].unlink()

    cycle_time, detail = downloader._select_automatic_cycle(dict(os.environ), now=PER_SOURCE_NOW, sources=sources)

    assert (cycle_time, detail["latest_allowed_cycle_time"]) == (_iso(selected), _iso(latest_allowed))


def _lock_main_args(tmp_path: Path, *sources: str) -> list[str]:
    args = ["--cycle-time", "2026-07-02T12:00:00Z"]
    for source in sources:
        args.extend(["--source", source])
    return [
        *args,
        "--database-url",
        "postgresql://node27_download_rw:writer-secret@127.0.0.1:55432/nhms",
        "--object-store-root",
        str(tmp_path / "object-store"),
        "--workspace-root",
        str(tmp_path / "workspace"),
        "--log-root",
        str(tmp_path / "logs"),
        "--lock-path",
        str(tmp_path / "download.lock"),
    ]


def _source_lock_file(tmp_path: Path, source: str) -> Path:
    return tmp_path / f"download.lock.{source}"


@contextmanager
def _held_lock(path: Path) -> Iterator[None]:
    """Hold a real flock on ``path`` for the duration of the block."""
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _lock_is_free(path: Path) -> bool:
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def _recording_download(calls: list[str]) -> Any:
    def fake_download(source: str, cycle_time: str, env: dict[str, str]) -> downloader.SourceDownloadResult:
        calls.append(source)
        return _fake_download_ok(source, cycle_time, env)

    return fake_download


def _assert_lock_blocked(rc: int, summary: dict[str, Any]) -> None:
    assert rc == downloader.LOCK_BLOCKED_RC
    assert summary["status"] == "lock_blocked"
    assert summary["blockers"] == [
        {
            "code": "NODE27_DOWNLOAD_LOCK_HELD",
            "env_var": "NODE27_DOWNLOAD_LOCK_PATH",
            "message": "A previous node-27 download pass is still active.",
        }
    ]


def test_source_pass_is_blocked_by_its_own_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare_env(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(downloader, "run_source_download", _recording_download(calls))

    with _held_lock(_source_lock_file(tmp_path, "gfs")):
        rc, summary, _rendered = _run_main(capsys, _lock_main_args(tmp_path, "GFS"))

    _assert_lock_blocked(rc, summary)
    assert calls == []


@pytest.mark.parametrize("held", ["gfs", "ifs"])
def test_multi_source_pass_is_blocked_by_either_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], held: str
) -> None:
    _prepare_env(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(downloader, "run_source_download", _recording_download(calls))

    with _held_lock(_source_lock_file(tmp_path, held)):
        rc, summary, _rendered = _run_main(capsys, _lock_main_args(tmp_path, "GFS", "IFS"))

    _assert_lock_blocked(rc, summary)
    assert calls == []


def test_pass_of_other_source_runs_while_one_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare_env(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(downloader, "run_source_download", _recording_download(calls))

    with _held_lock(_source_lock_file(tmp_path, "gfs")):
        rc, summary, _rendered = _run_main(capsys, _lock_main_args(tmp_path, "IFS"))

    assert (rc, summary["status"]) == (0, "completed")
    assert calls == ["IFS"]


def test_blocked_multi_source_pass_releases_the_locks_it_took(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare_env(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(downloader, "run_source_download", _recording_download(calls))

    with _held_lock(_source_lock_file(tmp_path, "ifs")):
        rc, summary, _rendered = _run_main(capsys, _lock_main_args(tmp_path, "GFS", "IFS"))
        gfs_lock_free = _lock_is_free(_source_lock_file(tmp_path, "gfs"))

    _assert_lock_blocked(rc, summary)
    assert gfs_lock_free
    assert calls == []


# The wrapper, run with bash against a temporary repository whose interpreter
# is a stub that records its arguments and whether the sources key is set.
WRAPPER_STUB_PYTHON = """#!/bin/sh
printf '%s\\n' "$@" > "$0.argv"
if [ "${NHMS_NODE27_DOWNLOAD_SOURCES+set}" = set ]; then
  printf 'set:%s\\n' "$NHMS_NODE27_DOWNLOAD_SOURCES" > "$0.sources"
else
  printf 'unset\\n' > "$0.sources"
fi
exit 0
"""


@dataclass(frozen=True)
class WrapperRun:
    return_code: int
    argv: list[str] | None
    sources_key: str | None
    logs: Path
    env_sourced: bool
    bootstrap_log: str


def _run_wrapper(
    tmp_path: Path,
    *args: str,
    env_lines: tuple[str, ...] = (),
    ambient: dict[str, str] | None = None,
) -> WrapperRun:
    repo = tmp_path / "repo"
    stub = repo / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    stub.write_text(WRAPPER_STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)
    logs = tmp_path / "logs"
    sourced_marker = tmp_path / "env-sourced"
    env_file = tmp_path / "node27-download.env"
    env_file.write_text(
        "\n".join(
            [
                "NHMS_NODE27_DOWNLOAD_ROLE=node27_data_plane_download",
                "DATABASE_URL=postgresql://placeholder",
                f"OBJECT_STORE_ROOT={tmp_path / 'object-store'}",
                f"WORKSPACE_ROOT={tmp_path / 'workspace'}",
                f"NODE27_DOWNLOAD_LOG_ROOT={logs}",
                *env_lines,
                f': > "{sourced_marker}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    bootstrap_log = tmp_path / "bootstrap.log"
    env = {
        "PATH": "/usr/bin:/bin",
        "NODE27_DOWNLOAD_REPO": str(repo),
        "NODE27_DOWNLOAD_ENV_FILE": str(env_file),
        "NODE27_DOWNLOAD_BOOTSTRAP_LOG": str(bootstrap_log),
        **(ambient or {}),
    }
    completed = subprocess.run(
        ["/bin/bash", str(WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    argv_file = Path(f"{stub}.argv")
    sources_file = Path(f"{stub}.sources")
    return WrapperRun(
        return_code=completed.returncode,
        argv=argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else None,
        sources_key=sources_file.read_text(encoding="utf-8").strip() if sources_file.exists() else None,
        logs=logs,
        env_sourced=sourced_marker.exists(),
        bootstrap_log=bootstrap_log.read_text(encoding="utf-8") if bootstrap_log.exists() else "",
    )


def _runner_argv(run: WrapperRun, *extra: str, summary: str) -> list[str]:
    return [
        str(run.logs.parent / "repo" / "scripts" / "node27_download_cycles.py"),
        "--cycle-time",
        "",
        "--summary-path",
        str(run.logs / summary),
        *extra,
    ]


@pytest.mark.parametrize(
    ("argument", "source"),
    [("GFS", "GFS"), ("ifs", "IFS")],
    ids=["a-GFS", "b-ifs"],
)
def test_wrapper_runs_one_source_with_per_source_files(tmp_path: Path, argument: str, source: str) -> None:
    run = _run_wrapper(tmp_path, argument)

    suffix = source.lower()
    assert run.return_code == 0
    assert run.argv == _runner_argv(run, "--source", source, summary=f"last-summary-{suffix}.json")
    log = run.logs / f"download-{suffix}.log"
    assert log.is_file()
    assert not (run.logs / "download.log").exists()
    start, done = log.read_text(encoding="utf-8").splitlines()
    assert start.endswith(f"node27-download: start source={source} cycle=auto")
    summary = run.logs / f"last-summary-{suffix}.json"
    assert done.endswith(f"node27-download: done source={source} rc=0 summary={summary}")


def test_wrapper_without_argument_keeps_the_configured_paths(tmp_path: Path) -> None:
    run = _run_wrapper(tmp_path)

    assert run.return_code == 0
    assert run.env_sourced
    assert run.argv == _runner_argv(run, summary="last-summary.json")
    start, done = (run.logs / "download.log").read_text(encoding="utf-8").splitlines()
    assert start.endswith("] node27-download: start cycle=auto")
    assert done.endswith(f"] node27-download: done rc=0 summary={run.logs / 'last-summary.json'}")
    assert sorted(path.name for path in run.logs.iterdir()) == ["download.log"]


@pytest.mark.parametrize("args", [("ERA5",), ("GFS", "IFS")], ids=["d-unknown", "e-two-arguments"])
def test_wrapper_refuses_bad_arguments_before_sourcing_env(tmp_path: Path, args: tuple[str, ...]) -> None:
    run = _run_wrapper(tmp_path, *args)

    assert run.return_code == 2
    assert run.argv is None
    assert not run.env_sourced
    assert "BLOCKED rc=2 reason=DOWNLOAD_SOURCE_ARGUMENT_INVALID" in run.bootstrap_log


def test_wrapper_strict_env_clears_the_sources_key(tmp_path: Path) -> None:
    run = _run_wrapper(tmp_path, ambient={"NHMS_NODE27_DOWNLOAD_SOURCES": "GFS,IFS"})

    assert run.return_code == 0
    assert run.sources_key == "unset"


def test_wrapper_appends_source_to_summary_path_without_extension(tmp_path: Path) -> None:
    run = _run_wrapper(tmp_path, "GFS", env_lines=(f"NODE27_DOWNLOAD_SUMMARY_PATH={tmp_path / 'logs' / 'summary'}",))

    assert run.return_code == 0
    assert run.argv == _runner_argv(run, "--source", "GFS", summary="summary-gfs")


def test_wrapper_inserts_source_into_explicit_log_file(tmp_path: Path) -> None:
    run = _run_wrapper(tmp_path, "GFS", env_lines=(f"NODE27_DOWNLOAD_LOG_FILE={tmp_path / 'logs' / 'custom.log'}",))

    assert run.return_code == 0
    assert (run.logs / "custom-gfs.log").is_file()
    assert not (run.logs / "custom.log").exists()
