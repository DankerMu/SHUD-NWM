from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "node27_autopipe_cron.sh"
RUN_QHH = "fcst_gfs_2026062012_basins_qhh_shud"
RUN_QHH_NEXT = "fcst_gfs_2026062112_basins_qhh_shud"
RUN_HEIHE = "fcst_gfs_2026062112_basins_heihe_shud"


def _prepare_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", str(work_root))
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", str(log_root))
    monkeypatch.setenv("NHMS_NODE27_INGEST_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_NODE27_INGEST_CONFIG_SOURCE", "pytest")
    return object_store_root, basins_root, work_root, log_root


def _write_run(object_store_root: Path, run_id: str, *, basin: str) -> None:
    input_dir = object_store_root / "runs" / run_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "identity": {
            "run_id": run_id,
            "source_id": "gfs",
            "model_id": f"basins_{basin}_shud",
            "basin_id": f"basins_{basin}",
            "basin_version_id": f"basins_{basin}_v2026_06",
            "model_package_uri": f"s3://nhms/models/basins_{basin}_shud/v2026_06/package/",
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


def _args(object_store_root: Path, basins_root: Path, database_url: str | None = None) -> list[str]:
    args = [
        "--object-store-root",
        str(object_store_root),
        "--basins-root",
        str(basins_root),
    ]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def _run_main(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, dict[str, Any], str]:
    rc = autopipe.main(args)
    rendered = capsys.readouterr().out
    return rc, json.loads(rendered), rendered


def _fail_if_called(name: str) -> Callable[..., Any]:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(f"{name} must not be called before ingest preflight passes")

    return fail


def _blocker_codes(summary: dict[str, Any]) -> set[str]:
    return {blocker["code"] for blocker in summary["ingest"]["preflight"]["blockers"]}


def test_missing_database_url_blocks_before_seed_run_publish_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SECRET_TOKEN", "preflight-secret-token")
    for name in ("_basin_seeded", "_already_ingested_runs", "_seed_basin", "_process_run", "_publish_display_runs"):
        monkeypatch.setattr(autopipe, name, _fail_if_called(name))

    rc, summary, rendered = _run_main(capsys, _args(object_store_root, basins_root))

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert summary["status"] == "preflight_blocked"
    assert summary["return_code"] == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"DATABASE_URL_MISSING"}
    assert summary["seed"] == autopipe._empty_seed_summary()
    assert summary["runs"] == autopipe._empty_runs_summary()
    assert "preflight-secret-token" not in rendered


def test_missing_direct_ingest_role_blocks_before_seed_run_publish_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    monkeypatch.delenv("NHMS_NODE27_INGEST_ROLE", raising=False)
    monkeypatch.setenv("SECRET_TOKEN", "preflight-secret-token")
    for name in ("_basin_seeded", "_already_ingested_runs", "_seed_basin", "_process_run", "_publish_display_runs"):
        monkeypatch.setattr(autopipe, name, _fail_if_called(name))

    rc, summary, rendered = _run_main(
        capsys,
        _args(object_store_root, basins_root, "postgresql://node27_writer:writer-secret@db.example/nhms"),
    )

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert summary["status"] == "preflight_blocked"
    assert summary["return_code"] == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"INGEST_ROLE_REQUIRED"}
    assert summary["ingest"]["preflight"]["role"]["ingest_role_env"] is None
    assert summary["seed"] == autopipe._empty_seed_summary()
    assert summary["runs"] == autopipe._empty_runs_summary()
    assert "preflight-secret-token" not in rendered
    assert "writer-secret" not in rendered


def test_preflight_reports_stable_blocker_codes_for_unsafe_ingest_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _object_store_root, _basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "display_readonly")
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", "relative-work")
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", "relative-log")

    rc, summary, rendered = _run_main(
        capsys,
        [
            "--object-store-root",
            "",
            "--basins-root",
            "",
            "--database-url",
            "postgresql://nhms_display_ro:readonly-secret@db.example/nhms",
        ],
    )

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {
        "INGEST_DISPLAY_READONLY_ROLE_FORBIDDEN",
        "DATABASE_URL_READONLY_IDENTITY",
        "OBJECT_STORE_ROOT_MISSING",
        "BASINS_ROOT_MISSING",
        "AUTOPIPE_WORK_ROOT_UNSAFE",
        "AUTOPIPE_LOG_ROOT_UNSAFE",
    }
    assert "readonly-secret" not in rendered


def test_preflight_reports_malformed_database_url_with_stable_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)

    rc, summary, _rendered = _run_main(capsys, _args(object_store_root, basins_root, "not-a-postgres-url"))

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"DATABASE_URL_INVALID"}


def test_preflight_rejects_database_url_without_username_before_libpq_ambient_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:55432/nhms")
    monkeypatch.setenv("PGUSER", "nhms_display_ro")
    monkeypatch.setenv("PGPASSWORD", "readonly-secret")
    for name in ("_basin_seeded", "_already_ingested_runs", "_seed_basin", "_process_run", "_publish_display_runs"):
        monkeypatch.setattr(autopipe, name, _fail_if_called(name))

    rc, summary, rendered = _run_main(capsys, _args(object_store_root, basins_root))

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"DATABASE_URL_USERNAME_MISSING"}
    assert summary["ingest"]["preflight"]["database"]["username_present"] is False
    assert summary["ingest"]["preflight"]["database"]["username_class"] == "missing"
    assert "readonly-secret" not in rendered


def test_preflight_rejects_database_url_without_password_before_libpq_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    pgpass_file = tmp_path / "ambient-pgpass"
    pgpass_file.write_text("*:*:*:*:readonly-secret\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://node27_writer@127.0.0.1:55432/nhms")
    monkeypatch.setenv("PGPASSWORD", "readonly-secret")
    monkeypatch.setenv("PGPASSFILE", str(pgpass_file))
    for name in ("_basin_seeded", "_already_ingested_runs", "_seed_basin", "_process_run", "_publish_display_runs"):
        monkeypatch.setattr(autopipe, name, _fail_if_called(name))

    rc, summary, rendered = _run_main(capsys, _args(object_store_root, basins_root))

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"DATABASE_URL_PASSWORD_MISSING"}
    assert summary["ingest"]["preflight"]["database"]["username_present"] is True
    assert summary["ingest"]["preflight"]["database"]["username_class"] == "writer_candidate"
    assert summary["ingest"]["preflight"]["database"]["password_present"] is False
    assert summary["seed"] == autopipe._empty_seed_summary()
    assert summary["runs"] == autopipe._empty_runs_summary()
    assert "readonly-secret" not in rendered
    assert str(pgpass_file) not in rendered


@pytest.mark.parametrize(
    ("database_url", "expected_database", "forbidden_fragments"),
    [
        (
            "postgresql://db.example/nhms?user=nhms_display_ro",
            {
                "host": "db.example",
                "port": None,
                "database": "nhms",
                "username_class": "display_readonly_like",
                "password_present": False,
            },
            ("nhms_display_ro",),
        ),
        (
            "postgresql://node27_writer:writer-secret@db.example/nhms?host=other.example&port=55433&dbname=otherdb",
            {
                "host": "other.example",
                "port": 55433,
                "database": "otherdb",
                "username_class": "writer_candidate",
                "password_present": True,
            },
            ("writer-secret",),
        ),
        (
            "postgresql://node27_writer:writer-secret@db.example/nhms?passfile=/tmp/node27-writer.pgpass",
            {
                "host": "db.example",
                "port": None,
                "database": "nhms",
                "username_class": "writer_candidate",
                "password_present": True,
            },
            ("writer-secret", "/tmp/node27-writer.pgpass"),
        ),
        (
            "postgresql://node27_writer:writer-secret@db.example/nhms?service=ambient",
            {
                "host": "db.example",
                "port": None,
                "database": "nhms",
                "username_class": "writer_candidate",
                "password_present": True,
            },
            ("writer-secret", "ambient"),
        ),
        (
            "postgresql://node27_writer@db.example/nhms?password=query-secret",
            {
                "host": "db.example",
                "port": None,
                "database": "nhms",
                "username_class": "writer_candidate",
                "password_present": True,
            },
            ("query-secret",),
        ),
    ],
)
def test_preflight_rejects_database_url_query_overrides_before_work_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    database_url: str,
    expected_database: dict[str, Any],
    forbidden_fragments: tuple[str, ...],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    for name in ("_basin_seeded", "_already_ingested_runs", "_seed_basin", "_process_run", "_publish_display_runs"):
        monkeypatch.setattr(autopipe, name, _fail_if_called(name))

    rc, summary, rendered = _run_main(capsys, _args(object_store_root, basins_root, database_url))

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert autopipe.DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN in _blocker_codes(summary)
    database_evidence = summary["ingest"]["preflight"]["database"]
    for key, value in expected_database.items():
        assert database_evidence[key] == value
    assert summary["seed"] == autopipe._empty_seed_summary()
    assert summary["runs"] == autopipe._empty_runs_summary()
    for forbidden in forbidden_fragments:
        assert forbidden not in rendered


def test_direct_entry_without_basins_root_blocks_even_when_default_path_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, _basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    monkeypatch.delenv("BASINS_ROOT", raising=False)

    rc, summary, _rendered = _run_main(
        capsys,
        [
            "--object-store-root",
            str(object_store_root),
            "--database-url",
            "postgresql://node27_writer:writer-secret@db.example/nhms",
        ],
    )

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"BASINS_ROOT_MISSING"}


def test_canonical_root_resolution_blocks_work_and_log_roots_resolving_to_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    root_link = tmp_path / "root-link"
    root_link.symlink_to("/")
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", str(root_link))
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", str(root_link))

    rc, summary, _rendered = _run_main(
        capsys,
        _args(object_store_root, basins_root, "postgresql://node27_writer:writer-secret@db.example/nhms"),
    )

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC
    assert _blocker_codes(summary) == {"AUTOPIPE_WORK_ROOT_UNSAFE", "AUTOPIPE_LOG_ROOT_UNSAFE"}


def test_ready_summary_exposes_ingest_role_and_already_ingested_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    _write_run(object_store_root, RUN_QHH, basin="qhh")
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_QHH})
    monkeypatch.setattr(autopipe, "_process_run", _fail_if_called("_process_run"))
    published_calls: list[str] = []

    def fake_publish(database_url: str) -> int:
        published_calls.append(database_url)
        return 5

    monkeypatch.setattr(autopipe, "_publish_display_runs", fake_publish)

    rc, summary, rendered = _run_main(
        capsys,
        _args(object_store_root, basins_root, "postgresql://node27_writer:writer-secret@db.example:55432/nhms"),
    )

    assert rc == 0
    assert summary["schema"] == autopipe.INGEST_SUMMARY_SCHEMA
    assert summary["status"] == "completed"
    assert summary["return_code"] == 0
    assert summary["ingest"]["role"] == autopipe.INGEST_ROLE
    assert summary["ingest"]["display_api_health_separate"] is True
    assert summary["ingest"]["preflight"]["status"] == "ready"
    assert summary["ingest"]["preflight"]["database"] == {
        "configured": True,
        "database": "nhms",
        "host": "db.example",
        "port": 55432,
        "scheme": "postgresql",
        "password_present": True,
        "username_class": "writer_candidate",
        "username_present": True,
    }
    assert summary["discovered_runs"] == 1
    assert summary["runs"]["already_ingested"] == 1
    assert summary["runs"]["processed"] == 0
    assert summary["runs"]["published"] == 5
    assert published_calls == ["postgresql://node27_writer:writer-secret@db.example:55432/nhms"]
    assert "writer-secret" not in rendered


def test_seed_registry_import_uses_database_url_env_not_subprocess_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    (basins_root / "qhh").mkdir()
    _write_run(object_store_root, RUN_QHH, basin="qhh")
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(autopipe, "_backfill_output_geometry", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(autopipe, "_activate_model", lambda *_args, **_kwargs: 1)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        calls.append((list(argv), dict(env)))
        if "import-basins-registry" in argv:
            return 0, json.dumps({"status": "ok", "river_network_version_id": "rnv-qhh"}) + "\n", ""
        if "discover-basins" in argv or "publish-basins" in argv:
            return 0, "{}\n", ""
        raise AssertionError(f"unexpected seed command: {argv}")

    monkeypatch.setattr(autopipe, "_run", fake_run)
    database_url = "postgresql://node27_writer:writer-secret@db.example/nhms"

    rc, summary, rendered = _run_main(
        capsys,
        [*_args(object_store_root, basins_root, database_url), "--seed-only"],
    )

    assert rc == 0
    assert summary["seed"]["seeded"] == ["qhh"]
    import_calls = [(argv, env) for argv, env in calls if "import-basins-registry" in argv]
    assert len(import_calls) == 1
    import_argv, import_env = import_calls[0]
    assert "--database-url" not in import_argv
    argv_text = "\n".join(argument for argv, _env in calls for argument in argv)
    assert "postgresql://" not in argv_text
    assert "writer-secret" not in argv_text
    assert import_env["DATABASE_URL"] == database_url
    assert "writer-secret" not in rendered


def test_basin_seed_failure_isolated_after_valid_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    _write_run(object_store_root, RUN_QHH, basin="qhh")
    _write_run(object_store_root, RUN_HEIHE, basin="heihe")
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda _database_url, basin_id: basin_id == "basins_heihe")

    def fake_seed(**kwargs: object) -> dict[str, Any]:
        return {"basin": kwargs["basin"], "outcome": "seed_failed", "stage": "import", "error": "secret=seed-token"}

    processed: list[str] = []
    monkeypatch.setattr(autopipe, "_seed_basin", fake_seed)
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(
        autopipe,
        "_process_run",
        lambda run_id, *_args, **_kwargs: processed.append(run_id)
        or {"run_id": run_id, "outcome": "ingested", "stage": "coverage"},
    )
    monkeypatch.setattr(autopipe, "_publish_display_runs", lambda _database_url: 1)

    rc, summary, rendered = _run_main(
        capsys,
        _args(object_store_root, basins_root, "postgresql://node27_writer:writer-secret@db.example/nhms"),
    )

    assert rc == 1
    assert summary["status"] == "completed_with_failures"
    assert summary["return_code"] == 1
    assert summary["seed"]["failed"] == [{"basin": "qhh", "error": "secret=[redacted]", "stage": "import"}]
    assert processed == [RUN_HEIHE]
    assert summary["runs"]["processed"] == 1
    assert "seed-token" not in rendered
    assert "writer-secret" not in rendered


def test_one_run_failure_isolated_after_valid_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, basins_root, _work_root, _log_root = _prepare_roots(monkeypatch, tmp_path)
    _write_run(object_store_root, RUN_QHH, basin="qhh")
    _write_run(object_store_root, RUN_QHH_NEXT, basin="qhh")
    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())

    def fake_process_run(run_id: str, *_args: object, **_kwargs: object) -> dict[str, Any]:
        if run_id == RUN_QHH:
            return {"run_id": run_id, "outcome": "failed", "stage": "parse", "error": "parse failed"}
        return {"run_id": run_id, "outcome": "ingested", "stage": "coverage"}

    monkeypatch.setattr(autopipe, "_process_run", fake_process_run)
    monkeypatch.setattr(autopipe, "_publish_display_runs", lambda _database_url: 1)

    rc, summary, _rendered = _run_main(
        capsys,
        _args(object_store_root, basins_root, "postgresql://node27_writer:writer-secret@db.example/nhms"),
    )

    assert rc == 1
    assert summary["runs"]["processed"] == 2
    assert summary["runs"]["failed"] == 1
    assert summary["runs"]["ingested"] == 1
    assert [detail["run_id"] for detail in summary["runs"]["details"]] == [RUN_QHH, RUN_QHH_NEXT]


def test_wrapper_contract_has_ingest_env_without_writer_default_or_display_env_source() -> None:
    script = WRAPPER.read_text(encoding="utf-8")

    assert "node27-ingest.env" in script
    assert "postgresql://nhms:nhms_dev" not in script
    assert '. "$REPO/infra/env/display.env"' not in script
    assert "INGEST_ENV_DISPLAY_RUNTIME_FORBIDDEN" in script
    for required_key in (
        "DATABASE_URL",
        "NHMS_NODE27_INGEST_ROLE",
        "NHMS_SERVICE_ROLE",
        "OBJECT_STORE_ROOT",
        "BASINS_ROOT",
        "AUTOPIPE_WORK_ROOT",
        "AUTOPIPE_LOG_ROOT",
        "N22_DSN",
        "PGUSER",
        "PGPASSWORD",
        "PGPASSFILE",
        "PGSERVICE",
        "PGSERVICEFILE",
    ):
        assert f"unset {required_key}" in script
    assert "--database-url" not in script


def test_direct_script_entry_resolves_repo_imports() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "node27_autopipeline.py"), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "Basin-agnostic node-27 autopipeline" in proc.stdout


def test_wrapper_missing_ingest_env_blocks_before_python_and_backstop(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    invocations = tmp_path / "invocations.txt"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text(f"#!/bin/sh\necho \"$@\" >> {invocations}\nexit 99\n", encoding="utf-8")
    python_bin.chmod(0o755)
    log = tmp_path / "bootstrap.log"

    env = {
        **os.environ,
        "NODE27_AUTOPIPE_REPO": str(fake_repo),
        "NODE27_AUTOPIPE_ENV_FILE": str(tmp_path / "missing-node27-ingest.env"),
        "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(log),
        "NODE27_AUTOPIPE_LOCK_PATH": str(tmp_path / "autopipe.lock"),
    }
    env.pop("NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV", None)

    proc = subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 2
    assert "INGEST_ENV_MISSING" in proc.stderr
    assert "INGEST_ENV_MISSING" in log.read_text(encoding="utf-8")
    assert not invocations.exists()


def test_wrapper_env_file_missing_database_url_ignores_ambient_without_override(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    scripts = fake_repo / "scripts"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (scripts, python_bin.parent, object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    (scripts / "node27_autopipeline.py").write_text("# fake autopipeline\n", encoding="utf-8")
    (scripts / "node27_refresh_coverage.py").write_text("# fake coverage\n", encoding="utf-8")
    invocations = tmp_path / "invocations.txt"
    python_bin.write_text(f"#!/bin/sh\necho \"$@\" >> {invocations}\nexit 0\n", encoding="utf-8")
    python_bin.chmod(0o755)
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(
        "\n".join(
            [
                "NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest",
                f"OBJECT_STORE_ROOT={object_store_root}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"BASINS_ROOT={basins_root}",
                f"AUTOPIPE_WORK_ROOT={work_root}",
                f"AUTOPIPE_LOG_ROOT={log_root}",
                f"AUTOPIPE_LOG_FILE={log_root / 'autopipe.log'}",
                f"AUTOPIPE_LOCK_PATH={tmp_path / 'autopipe.lock'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    bootstrap_log = tmp_path / "bootstrap.log"
    env = {
        **os.environ,
        "NODE27_AUTOPIPE_REPO": str(fake_repo),
        "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
        "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(bootstrap_log),
        "DATABASE_URL": "postgresql://node27_writer:ambient-secret@db.example/nhms",
    }
    env.pop("NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV", None)

    proc = subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 2
    assert "DATABASE_URL_MISSING" in proc.stderr
    assert "DATABASE_URL_MISSING" in bootstrap_log.read_text(encoding="utf-8")
    assert not invocations.exists()
    assert not (log_root / "autopipe.log").exists()


def test_wrapper_env_file_passwordless_database_url_does_not_inherit_libpq_credentials(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    scripts = fake_repo / "scripts"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (scripts, python_bin.parent, object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    (scripts / "node27_autopipeline.py").write_text("# fake autopipeline\n", encoding="utf-8")
    (scripts / "node27_refresh_coverage.py").write_text("# fake coverage\n", encoding="utf-8")
    invocations = tmp_path / "invocations.txt"
    python_bin.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"echo \"$@\" >> {invocations}",
                'for key in PGUSER PGPASSWORD PGPASSFILE PGSERVICE PGSERVICEFILE; do',
                '  eval "value=\\${$key+x}"',
                '  if [ "$value" = "x" ]; then',
                '    echo "libpq credential env leaked: $key" >&2',
                "    exit 99",
                "  fi",
                "done",
                'echo \'{"status":"preflight_blocked","return_code":2,'
                '"blockers":[{"code":"DATABASE_URL_PASSWORD_MISSING"}]}\'',
                "exit 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://node27_writer@db.example/nhms",
                "NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest",
                f"OBJECT_STORE_ROOT={object_store_root}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"BASINS_ROOT={basins_root}",
                f"AUTOPIPE_WORK_ROOT={work_root}",
                f"AUTOPIPE_LOG_ROOT={log_root}",
                f"AUTOPIPE_LOG_FILE={log_root / 'autopipe.log'}",
                f"AUTOPIPE_LOCK_PATH={tmp_path / 'autopipe.lock'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    pgpass_file = tmp_path / "ambient.pgpass"
    pgservice_file = tmp_path / "ambient.pgservice"
    pgpass_file.write_text("*:*:*:*:ambient-secret\n", encoding="utf-8")
    pgservice_file.write_text("[ambient]\npassword=ambient-secret\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    env = {
        **os.environ,
        "NODE27_AUTOPIPE_REPO": str(fake_repo),
        "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
        "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "PGUSER": "ambient-user",
        "PGPASSWORD": "ambient-secret",
        "PGPASSFILE": str(pgpass_file),
        "PGSERVICE": "ambient-service",
        "PGSERVICEFILE": str(pgservice_file),
    }
    env.pop("NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV", None)

    proc = subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 2
    assert proc.stderr == ""
    calls = invocations.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "node27_autopipeline.py" in calls[0]
    assert "node27_refresh_coverage.py" not in calls[0]
    log_text = (log_root / "autopipe.log").read_text(encoding="utf-8")
    assert "DATABASE_URL_PASSWORD_MISSING" in log_text
    assert "coverage backstop" not in log_text
    for forbidden in ("ambient-secret", str(pgpass_file), str(pgservice_file)):
        assert forbidden not in log_text
        assert forbidden not in proc.stderr


def test_wrapper_env_file_missing_ingest_role_ignores_ambient_without_override(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    scripts = fake_repo / "scripts"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (scripts, python_bin.parent, object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    (scripts / "node27_autopipeline.py").write_text("# fake autopipeline\n", encoding="utf-8")
    (scripts / "node27_refresh_coverage.py").write_text("# fake coverage\n", encoding="utf-8")
    invocations = tmp_path / "invocations.txt"
    python_bin.write_text(f"#!/bin/sh\necho \"$@\" >> {invocations}\nexit 0\n", encoding="utf-8")
    python_bin.chmod(0o755)
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://node27_writer:writer-secret@db.example/nhms",
                f"OBJECT_STORE_ROOT={object_store_root}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"BASINS_ROOT={basins_root}",
                f"AUTOPIPE_WORK_ROOT={work_root}",
                f"AUTOPIPE_LOG_ROOT={log_root}",
                f"AUTOPIPE_LOG_FILE={log_root / 'autopipe.log'}",
                f"AUTOPIPE_LOCK_PATH={tmp_path / 'autopipe.lock'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    bootstrap_log = tmp_path / "bootstrap.log"
    env = {
        **os.environ,
        "NODE27_AUTOPIPE_REPO": str(fake_repo),
        "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
        "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(bootstrap_log),
        "NHMS_NODE27_INGEST_ROLE": autopipe.INGEST_ROLE,
    }
    env.pop("NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV", None)

    proc = subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 2
    assert "INGEST_ROLE_REQUIRED" in proc.stderr
    assert "INGEST_ROLE_REQUIRED" in bootstrap_log.read_text(encoding="utf-8")
    assert not invocations.exists()
    assert not (log_root / "autopipe.log").exists()


def test_wrapper_rejects_display_env_file_before_source(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    display_env = tmp_path / "display.env"
    poison = tmp_path / "poison.txt"
    display_env.write_text(f"touch {poison}\n", encoding="utf-8")
    log = tmp_path / "bootstrap.log"

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(fake_repo),
            "NODE27_AUTOPIPE_ENV_FILE": str(display_env),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "INGEST_ENV_DISPLAY_RUNTIME_FORBIDDEN" in proc.stderr
    assert not poison.exists()


def test_wrapper_requires_explicit_ingest_role(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir()
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(f"AUTOPIPE_LOG_ROOT={log_root}\n", encoding="utf-8")
    env_file.chmod(0o600)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(tmp_path / "repo"),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "INGEST_ROLE_REQUIRED" in proc.stderr


def test_wrapper_rejects_world_readable_ingest_env(tmp_path: Path) -> None:
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text("NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest\n", encoding="utf-8")
    env_file.chmod(0o644)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(tmp_path / "repo"),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "INGEST_ENV_MODE_UNSAFE" in proc.stderr


def test_wrapper_rejects_ingest_env_symlink_before_source(tmp_path: Path) -> None:
    target_env = tmp_path / "node27-ingest-target.env"
    env_link = tmp_path / "node27-ingest.env"
    poison = tmp_path / "poison.txt"
    target_env.write_text(f"touch {poison}\n", encoding="utf-8")
    target_env.chmod(0o600)
    env_link.symlink_to(target_env)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(tmp_path / "repo"),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_link),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "INGEST_ENV_SYMLINK_FORBIDDEN" in proc.stderr
    assert not poison.exists()


def test_wrapper_blocks_ingest_env_source_failure(tmp_path: Path) -> None:
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text("return 7\n", encoding="utf-8")
    env_file.chmod(0o600)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(tmp_path / "repo"),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "INGEST_ENV_SOURCE_FAILED" in proc.stderr


def test_wrapper_rejects_log_root_symlink_to_filesystem_root(tmp_path: Path) -> None:
    env_file = tmp_path / "node27-ingest.env"
    root_link = tmp_path / "root-link"
    root_link.symlink_to("/")
    env_file.write_text(
        "\n".join(
            [
                "NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest",
                f"AUTOPIPE_LOG_ROOT={root_link}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(tmp_path / "repo"),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
            "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "AUTOPIPE_LOG_ROOT_UNSAFE" in proc.stderr


def test_wrapper_keeps_writer_database_url_out_of_child_argv(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    scripts = fake_repo / "scripts"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (scripts, python_bin.parent, object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    (scripts / "node27_autopipeline.py").write_text("# fake autopipeline\n", encoding="utf-8")
    argv_capture = tmp_path / "argv.txt"
    env_capture = tmp_path / "env.txt"
    python_bin.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"printf '%s\\n' \"$@\" > {argv_capture}",
                f"printf '%s\\n' \"$DATABASE_URL\" > {env_capture}",
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://node27_writer:writer-secret@db.example/nhms",
                "NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest",
                f"OBJECT_STORE_ROOT={object_store_root}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"BASINS_ROOT={basins_root}",
                f"AUTOPIPE_WORK_ROOT={work_root}",
                f"AUTOPIPE_LOG_ROOT={log_root}",
                f"AUTOPIPE_LOG_FILE={log_root / 'autopipe.log'}",
                f"AUTOPIPE_LOCK_PATH={tmp_path / 'autopipe.lock'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)
    env = {
        **os.environ,
        "NODE27_AUTOPIPE_REPO": str(fake_repo),
        "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
        "NODE27_AUTOPIPE_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    env.pop("NODE27_AUTOPIPE_ALLOW_AMBIENT_ENV", None)

    proc = subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    argv_text = argv_capture.read_text(encoding="utf-8")
    assert "node27_autopipeline.py" in argv_text
    assert "--database-url" not in argv_text
    assert "writer-secret" not in argv_text
    assert "postgresql://" not in argv_text
    assert env_capture.read_text(encoding="utf-8").strip() == (
        "postgresql://node27_writer:writer-secret@db.example/nhms"
    )
    assert "writer-secret" not in (log_root / "autopipe.log").read_text(encoding="utf-8")


def test_wrapper_preflight_blocked_rc2_skips_coverage_backstop(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    scripts = fake_repo / "scripts"
    python_bin = fake_repo / ".venv" / "bin" / "python"
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    for path in (scripts, python_bin.parent, object_store_root, basins_root, work_root, log_root):
        path.mkdir(parents=True, exist_ok=True)
    (scripts / "node27_autopipeline.py").write_text("# fake autopipeline\n", encoding="utf-8")
    (scripts / "node27_refresh_coverage.py").write_text("# fake coverage\n", encoding="utf-8")
    invocations = tmp_path / "invocations.txt"
    python_bin.write_text(f"#!/bin/sh\necho \"$@\" >> {invocations}\nexit 2\n", encoding="utf-8")
    python_bin.chmod(0o755)
    env_file = tmp_path / "node27-ingest.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://node27_writer:writer-secret@db.example/nhms",
                "NHMS_NODE27_INGEST_ROLE=node27_data_plane_ingest",
                f"OBJECT_STORE_ROOT={object_store_root}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"BASINS_ROOT={basins_root}",
                f"AUTOPIPE_WORK_ROOT={work_root}",
                f"AUTOPIPE_LOG_ROOT={log_root}",
                f"AUTOPIPE_LOG_FILE={log_root / 'autopipe.log'}",
                f"AUTOPIPE_LOCK_PATH={tmp_path / 'autopipe.lock'}",
                f"INVOCATIONS={invocations}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            **os.environ,
            "NODE27_AUTOPIPE_REPO": str(fake_repo),
            "NODE27_AUTOPIPE_ENV_FILE": str(env_file),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    calls = invocations.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "node27_autopipeline.py" in calls[0]
    assert "node27_refresh_coverage.py" not in calls[0]
    log_text = (log_root / "autopipe.log").read_text(encoding="utf-8")
    assert "coverage backstop" not in log_text
    assert "writer-secret" not in log_text
