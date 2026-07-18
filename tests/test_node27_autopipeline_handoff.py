from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe

RUN_A = "fcst_gfs_2026062012_basins_qhh_shud"
RUN_B = "fcst_gfs_2026062112_basins_qhh_shud"
DIRECT_GRID_RUN = "fcst_gfs_2026070600_dg_0123456789abcdef"
NODE27_DATABASE_URL = "postgresql://node27_writer:secret@127.0.0.1:55432/nhms"


def _write_run(object_store_root: Path, run_id: str, *, handoff: bool = True) -> None:
    input_dir = object_store_root / "runs" / run_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "identity": {
            "run_id": run_id,
            "source_id": "gfs",
            "model_id": "basins_qhh_shud",
            "basin_id": "basins_qhh",
            "basin_version_id": "basins_qhh_v2026_06",
            "model_package_uri": "s3://nhms/models/basins_qhh_shud/v2026_06/package/",
            "forcing_version_id": f"forc_{run_id}",
        },
        "cycle_time": "2026-06-20T12:00:00Z",
        "start_time": "2026-06-20T12:00:00Z",
        "end_time": "2026-06-30T12:00:00Z",
        "forcing": {"forcing_package_uri": f"s3://nhms/forcing/{run_id}/"},
        "output_uri": f"s3://nhms/runs/{run_id}/output/",
        "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
    }
    (input_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    if handoff:
        (input_dir / "forcing_domain_handoff.json").write_text("{}\n", encoding="utf-8")


def _prepare_autopipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runs: Mapping[str, bool],
    apply_reports: Mapping[str, dict[str, Any] | BaseException] | None = None,
    command_handler: Callable[[list[str], dict[str, str]], tuple[int, str, str]] | None = None,
) -> tuple[Path, list[list[str]], list[str]]:
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    basins_root.mkdir()
    work_root.mkdir()
    log_root.mkdir()
    for run_id, has_handoff in runs.items():
        _write_run(object_store_root, run_id, handoff=has_handoff)

    calls: list[list[str]] = []
    published_calls: list[str] = []

    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        autopipe,
        "_ensure_seeded_basin_display_ready",
        lambda _database_url, model_id: {
            "model_id": model_id,
            "river_network_version_id": f"{model_id}_rivnet",
            "output_geometry_backfilled": 0,
            "model_activated_rows": 1,
        },
    )
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", str(work_root))
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", str(log_root))
    monkeypatch.setenv("NHMS_NODE27_INGEST_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_NODE27_INGEST_CONFIG_SOURCE", "pytest")
    monkeypatch.setenv("DATABASE_URL", NODE27_DATABASE_URL)

    def fake_publish(database_url: str) -> int:
        published_calls.append(database_url)
        return 7

    monkeypatch.setattr(autopipe, "_publish_display_runs", fake_publish)

    reports = dict(apply_reports or {})

    def fake_apply_path(manifest_path: str | Path, **_kwargs: object) -> dict[str, Any]:
        run_id = Path(manifest_path).parents[1].name
        report = reports.get(run_id, _handoff_success(run_id))
        if isinstance(report, BaseException):
            raise report
        return report

    monkeypatch.setattr(autopipe, "_apply_object_store_forcing_handoff", fake_apply_path)

    def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        calls.append(argv)
        if command_handler is not None:
            return command_handler(argv, env)
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            return 0, json.dumps({"status": "registered"}) + "\n", ""
        if "workers.output_parser.cli" in command:
            run_id = argv[-1]
            return 0, json.dumps({"status": "parsed", "rows_written": len(run_id)}) + "\n", ""
        if "node27_refresh_coverage.py" in command:
            return 0, json.dumps({"refreshed": True}) + "\n", ""
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(autopipe, "_run", fake_run)
    monkeypatch.setattr(autopipe, "_refresh_coverage_script", lambda: Path("/fake/node27_refresh_coverage.py"))

    return object_store_root, calls, published_calls


def _run_main(capsys: pytest.CaptureFixture[str], object_store_root: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    rc = autopipe.main(
        [
            "--object-store-root",
            str(object_store_root),
            "--basins-root",
            str(object_store_root.parent / "Basins"),
            *extra,
        ]
    )
    return rc, json.loads(capsys.readouterr().out)


def _handoff_success(run_id: str) -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "applied",
        "available": True,
        "ready": True,
        "row_counts": {
            "met.forcing_version": 1,
            "met.met_station": 2,
            "met.forcing_station_timeseries": 8,
            "met.interp_weight": 4,
        },
        "identity": {"run_id": run_id, "source_id": "gfs"},
        "unavailable_reasons": [],
    }


def _handoff_unavailable(code: str = "HANDOFF_FIELD_MISSING") -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "unavailable",
        "available": False,
        "ready": False,
        "row_counts": {},
        "unavailable_reasons": [{"code": code, "detail": "redacted"}],
    }


def _handoff_failed() -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "failed",
        "available": False,
        "ready": False,
        "row_counts": {},
        "unavailable_reasons": [{"code": "HANDOFF_APPLY_SQL_FAILURE", "detail": "redacted"}],
    }


def test_direct_grid_run_discovery_uses_manifest_basin_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={DIRECT_GRID_RUN: True},
    )

    rc, summary = _run_main(capsys, object_store_root, "--only-basin", "qhh")

    assert rc == 0
    assert summary["discovered_runs"] == 1
    assert summary["runs"]["processed"] == 1
    assert summary["runs"]["details"][0]["run_id"] == DIRECT_GRID_RUN
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]


def _command_kinds(calls: list[list[str]]) -> list[str]:
    kinds: list[str] = []
    for argv in calls:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            kinds.append("register")
        elif "workers.output_parser.cli" in command:
            kinds.append("parse")
        elif "node27_refresh_coverage.py" in command:
            kinds.append("coverage")
    return kinds


def test_declared_handoff_success_records_run_details_without_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["stage"] == "coverage"
    assert detail["forcing_stage"] == {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "applied",
        "ready": True,
        "row_counts": {
            "met.forcing_version": 1,
            "met.met_station": 2,
            "met.forcing_station_timeseries": 8,
            "met.interp_weight": 4,
        },
        "reason_codes": [],
    }
    assert detail["parse_status"] == "parsed"
    assert detail["coverage_refresh"] == "refreshed"
    assert summary["runs"]["published"] == 7


def test_missing_handoff_degrades_forcing_stage_without_blocking_qdown_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: False})

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["stage"] == "coverage"
    assert detail["forcing_stage"] == {
        "mode": autopipe.NO_FORCING_HANDOFF_MODE,
        "status": "skipped",
        "ready": False,
        "row_counts": {},
        "reason_codes": [autopipe.NO_FORCING_HANDOFF_REASON],
    }
    assert detail["parse_status"] == "parsed"
    assert detail["coverage_refresh"] == "refreshed"
    assert summary["runs"]["published"] == 7
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_declared_handoff_unavailable_does_not_fallback_to_node22_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable("HANDOFF_PAYLOAD_CHECKSUM_MISMATCH")},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register"]
    assert published_calls == []
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "failed"
    assert detail["stage"] == "forcing_handoff"
    assert detail["forcing_stage"]["mode"] == autopipe.OBJECT_STORE_HANDOFF_MODE
    assert detail["forcing_stage"]["reason_codes"] == ["HANDOFF_PAYLOAD_CHECKSUM_MISMATCH"]


def test_declared_handoff_apply_exception_isolated_without_node22_db_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        apply_reports={
            RUN_A: RuntimeError(
                'apply exploded with {"p\\u0061ssword": "n22-secret"}'
            ),
            RUN_B: _handoff_success(RUN_B),
        },
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register", "register", "parse", "coverage"]
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "failed"
    assert details[RUN_A]["stage"] == "forcing_handoff"
    assert details[RUN_A]["forcing_stage"] == {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "failed",
        "ready": False,
        "row_counts": {},
        "reason_codes": [autopipe.FORCING_HANDOFF_FAILED_REASON],
    }
    assert details[RUN_B]["outcome"] == "ingested"
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_parse_and_coverage_failures_preserve_forcing_evidence_and_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        run_id = argv[-1]
        if "node27_ingest_run.py" in command:
            return 0, "{}", ""
        if "workers.output_parser.cli" in command and run_id == RUN_A:
            return 1, "", "parse exploded"
        if "workers.output_parser.cli" in command:
            return 0, json.dumps({"status": "parsed", "rows_written": 11}) + "\n", ""
        if "node27_refresh_coverage.py" in command and run_id == RUN_B:
            return 7, "", "coverage exploded"
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        command_handler=handler,
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register", "parse", "register", "parse", "coverage"]
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "failed"
    assert details[RUN_A]["stage"] == "parse"
    assert details[RUN_A]["forcing_stage"]["mode"] == autopipe.OBJECT_STORE_HANDOFF_MODE
    assert details[RUN_B]["outcome"] == "ingested"
    assert details[RUN_B]["stage"] == "coverage"
    assert details[RUN_B]["coverage_refresh"] == "refresh_failed_rc7"
