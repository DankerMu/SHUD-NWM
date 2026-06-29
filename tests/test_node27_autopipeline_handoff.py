from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe

RUN_A = "fcst_gfs_2026062012_basins_qhh_shud"
RUN_B = "fcst_gfs_2026062112_basins_qhh_shud"


def _argv_run_id(argv: list[str]) -> str:
    return argv[argv.index("--run-id") + 1]


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
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", str(work_root))
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", str(log_root))
    monkeypatch.setenv("NHMS_NODE27_INGEST_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_NODE27_INGEST_CONFIG_SOURCE", "pytest")

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
        if "node27_mirror_forcing.py" in command:
            return 0, json.dumps(_mirror_success()) + "\n", ""
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
            "--database-url",
            "postgresql://node27-writer:secret@db.example/nhms",
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


def _mirror_success() -> dict[str, Any]:
    return {
        "run_id": RUN_A,
        "mirror_boundary": {
            "mode": autopipe.TRANSITIONAL_MIRROR_MODE,
            "dsn": {"source": "env:N22_DSN", "printed": False, "dsn_redacted": True},
        },
        "forcing_version": {"local_rows": 1},
        "met_stations": {"local_rows": 2},
        "station_timeseries": {"local_rows": 8},
        "interp_weight": {"local_rows": 4},
    }


def _command_kinds(calls: list[list[str]]) -> list[str]:
    kinds: list[str] = []
    for argv in calls:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            kinds.append("register")
        elif "node27_mirror_forcing.py" in command:
            kinds.append("mirror")
        elif "workers.output_parser.cli" in command:
            kinds.append("parse")
        elif "node27_refresh_coverage.py" in command:
            kinds.append("coverage")
    return kinds


def test_declared_handoff_success_bypasses_mirror_and_records_run_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == ["postgresql://node27-writer:secret@db.example/nhms"]
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
    assert "publish" not in {item["stage"] for item in summary["runs"]["details"]}
    assert summary["runs"]["published"] == 7


def test_no_declared_handoff_without_explicit_mirror_parses_hydro_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("N22_DSN", raising=False)
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: False},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == ["postgresql://node27-writer:secret@db.example/nhms"]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["stage"] == "coverage"
    assert detail["forcing_stage"]["mode"] == autopipe.NO_FORCING_HANDOFF_MODE
    assert detail["forcing_stage"]["reason_codes"] == [autopipe.NO_FORCING_HANDOFF_AND_MIRROR_DSN_REASON]
    assert detail["parse_status"] == "parsed"
    assert detail["coverage_refresh"] == "refreshed"


def test_configured_node22_dsn_without_archived_rollback_allowance_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.delenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, raising=False)
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: False},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert not [argv for argv in calls if "node27_mirror_forcing.py" in " ".join(argv)]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["forcing_stage"]["mode"] == autopipe.NO_FORCING_HANDOFF_MODE
    assert detail["forcing_stage"]["reason_codes"] == [autopipe.NODE22_MIRROR_ROLLBACK_NOT_ALLOWED_REASON]
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_no_declared_handoff_uses_explicit_mirror_fallback_and_normalizes_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.setenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: False},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    mirror_calls = [argv for argv in calls if "node27_mirror_forcing.py" in " ".join(argv)]
    assert len(mirror_calls) == 1
    assert "--node22-url" not in mirror_calls[0]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["forcing_stage"]["mode"] == autopipe.TRANSITIONAL_MIRROR_MODE
    assert detail["forcing_stage"]["row_counts"] == {
        "met.forcing_version": 1,
        "met.met_station": 2,
        "met.forcing_station_timeseries": 8,
        "met.interp_weight": 4,
    }
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_node22_url_is_passed_to_explicit_mirror_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("N22_DSN", raising=False)
    node22_url = "postgresql://n22_user:n22-secret@node22.example/nhms"
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: False},
    )

    rc, summary = _run_main(
        capsys,
        object_store_root,
        "--node22-url",
        node22_url,
        "--allow-archived-node22-db-rollback-mirror",
    )

    assert rc == 0
    mirror_call = next(argv for argv in calls if "node27_mirror_forcing.py" in " ".join(argv))
    assert "--allow-archived-node22-db-rollback-mirror" in mirror_call
    node22_index = mirror_call.index("--node22-url")
    assert mirror_call[node22_index : node22_index + 2] == ["--node22-url", node22_url]
    assert summary["runs"]["details"][0]["forcing_stage"]["mode"] == autopipe.TRANSITIONAL_MIRROR_MODE
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_declared_handoff_unavailable_does_not_fallback_to_configured_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.setenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable("HANDOFF_PAYLOAD_CHECKSUM_MISMATCH")},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register"]
    assert published_calls == ["postgresql://node27-writer:secret@db.example/nhms"]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "failed"
    assert detail["stage"] == "forcing_handoff"
    assert detail["forcing_stage"]["mode"] == autopipe.OBJECT_STORE_HANDOFF_MODE
    assert detail["forcing_stage"]["reason_codes"] == ["HANDOFF_PAYLOAD_CHECKSUM_MISMATCH"]


def test_declared_handoff_failed_does_not_fallback_and_unrelated_run_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.setenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        apply_reports={RUN_A: _handoff_failed(), RUN_B: _handoff_success(RUN_B)},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register", "register", "parse", "coverage"]
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "failed"
    assert details[RUN_A]["stage"] == "forcing_handoff"
    assert details[RUN_B]["outcome"] == "ingested"


def test_declared_handoff_apply_exception_isolated_without_mirror_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.setenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        apply_reports={
            RUN_A: RuntimeError("apply exploded with password=n22-secret"),
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


def test_configured_mirror_rc2_skip_and_nonzero_failure_are_run_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example/nhms")
    monkeypatch.setenv(autopipe.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")

    def handler(argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        run_id = _argv_run_id(argv)
        if "node27_ingest_run.py" in command:
            return 0, "{}", ""
        if "node27_mirror_forcing.py" in command and run_id == RUN_A:
            return 2, json.dumps({"reason": "FORCING_NOT_ON_NODE22"}) + "\n", ""
        if "node27_mirror_forcing.py" in command and run_id == RUN_B:
            return 1, json.dumps({"reason": "NODE22_TRANSITIONAL_MIRROR_FAILED"}) + "\n", ""
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, _calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: False, RUN_B: False},
        command_handler=handler,
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "skipped"
    assert details[RUN_A]["forcing_stage"]["reason_codes"] == ["FORCING_NOT_ON_NODE22"]
    assert details[RUN_B]["outcome"] == "failed"
    assert details[RUN_B]["forcing_stage"]["reason_codes"] == ["NODE22_TRANSITIONAL_MIRROR_FAILED"]


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
