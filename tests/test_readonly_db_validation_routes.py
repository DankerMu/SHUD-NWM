"""Display-route, secret-source, and timeout contracts for readonly DB validation."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI

from services.production_closure import readonly_db_validation
from services.production_closure.readonly_db_validation import (
    ProbeTarget,
    PsycopgReadonlyDbProbeAdapter,
    ReadonlyDbValidationConfig,
    RouteHttpResponse,
    run_display_manual_action_probes,
    run_display_route_smoke,
)
from tests.test_readonly_db_validation import (
    REPO_ROOT,
    STAT_DIALECT_SUBSTITUTIONS,
    _bare_404_route_requester,
    _bsd_stat_available,
    _evidence_root,
    _gnu_stat_available,
    _mixed_route_requester,
    _portable_stat_script,
    _route_name_for_path,
    _run_id,
    _stat_output,
)


def test_display_route_smoke_pass_requires_success_and_fixture_misses_are_blocked_not_pass() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("routes"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    results = run_display_route_smoke(config, identity, route_requester=_mixed_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    models = next(item for item in results if item["name"] == "models")
    assert latest["status"] == "BLOCKED"
    assert latest["http_status"] == 404
    assert models["status"] == "FAIL"
    assert models["http_status"] == 500


def test_bare_missing_route_404_is_fail_but_allowlisted_fixture_error_is_blocked() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-404"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    results = run_display_route_smoke(config, identity, route_requester=_bare_404_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    logs = next(item for item in results if item["name"] == "job_logs")
    assert latest["status"] == "FAIL"
    assert latest["http_status"] == 404
    assert logs["status"] == "BLOCKED"
    assert logs["error_code"] == "JOB_LOG_NOT_PUBLISHED"


def test_display_route_smoke_constructs_strict_identity_paths() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-strict"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }
    observed_paths: dict[str, str] = {}

    def strict_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        if name:
            observed_paths[name] = path
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            response_identity = {key: identity[key] for key in ("source", "cycle_time", "run_id", "model_id")}
            if name == "job_logs":
                response_identity["job_id"] = identity["job_id"]
            body["data"] = {"identity": response_identity}
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=strict_route_requester)

    assert all(item["status"] == "PASS" for item in results)
    for name in ("latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"):
        assert name in observed_paths
        query = parse_qs(urlsplit(observed_paths[name]).query)
        assert query["source"] == ["GFS"]
        assert query["cycle_time"] == ["2026-05-03T00:00:00+00:00"]
        assert query["run_id"] == ["run_routes"]
        assert query["model_id"] == ["model_routes"]
    assert urlsplit(observed_paths["jobs"]).path == "/api/v1/jobs"
    assert parse_qs(urlsplit(observed_paths["jobs"]).query)["limit"] == ["1"]
    assert urlsplit(observed_paths["job_logs"]).path == "/api/v1/jobs/job_routes/logs"


def test_display_route_smoke_blocks_2xx_identity_mismatch() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-response-mismatch"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def mismatched_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            response_identity = {key: identity[key] for key in ("source", "cycle_time", "run_id", "model_id")}
            response_identity["model_id"] = "wrong-model"
            if name == "job_logs":
                response_identity["job_id"] = identity["job_id"]
            body["data"] = {"identity": response_identity}
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=mismatched_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    assert latest["status"] == "BLOCKED"
    assert latest["reason"] == "display_read_route_response_identity_invalid"
    assert latest["identity_blockers"][0]["code"] == "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH"


def test_display_route_smoke_blocks_fragmented_identity_across_response_objects() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-fragmented-object"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def fragmented_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            body["data"] = {
                "identity": {"source_id": identity["source"]},
                "item": {"cycle_time": identity["cycle_time"], "run_id": identity["run_id"]},
                "metadata": {"model_id": identity["model_id"], "job_id": identity["job_id"]},
            }
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=fragmented_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    assert latest["status"] == "BLOCKED"
    assert latest["reason"] == "display_read_route_response_identity_invalid"
    assert "response_identity" not in latest
    assert {blocker["code"] for blocker in latest["identity_blockers"]} == {
        "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING"
    }


def test_display_route_smoke_blocks_fragmented_identity_across_list_rows() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-fragmented-list"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def fragmented_list_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            body["data"] = [
                {"source_id": identity["source"], "cycle_time": identity["cycle_time"]},
                {"run_id": identity["run_id"], "model_id": identity["model_id"], "job_id": identity["job_id"]},
            ]
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=fragmented_list_requester)

    jobs = next(item for item in results if item["name"] == "jobs")
    assert jobs["status"] == "BLOCKED"
    assert jobs["reason"] == "display_read_route_response_identity_invalid"
    assert "response_identity" not in jobs
    assert {blocker["code"] for blocker in jobs["identity_blockers"]} == {"READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING"}


def test_display_route_smoke_blocks_identity_bound_routes_when_strict_identity_missing() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-missing-strict"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    observed_paths: list[str] = []

    def route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        observed_paths.append(path)
        return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})

    results = run_display_route_smoke(
        config,
        {
            "source": "GFS",
            "cycle_time": "2026-05-03T00:00:00+00:00",
            "run_id": "run_routes",
        },
        route_requester=route_requester,
    )

    blocked_by_name = {
        item["name"]: item
        for item in results
        if item["name"] in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}
    }
    assert set(blocked_by_name) == {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}
    assert all(item["status"] == "BLOCKED" for item in blocked_by_name.values())
    assert blocked_by_name["jobs"]["missing_identity_fields"] == ["model_id"]
    assert blocked_by_name["job_logs"]["missing_identity_fields"] == ["model_id", "job_id"]
    assert "/api/v1/jobs?limit=1" not in observed_paths


def test_default_api_probe_adapter_uses_fixed_api_owned_module(monkeypatch: pytest.MonkeyPatch) -> None:
    imported_modules: list[str] = []
    sentinel = object()

    def fake_import_module(module_name: str) -> object:
        imported_modules.append(module_name)
        return sentinel

    monkeypatch.setattr(readonly_db_validation.importlib, "import_module", fake_import_module)

    assert readonly_db_validation._default_api_probe_adapter() is sentinel
    assert imported_modules == [readonly_db_validation.DEFAULT_API_PROBE_ADAPTER_MODULE]
    assert readonly_db_validation.DEFAULT_API_PROBE_ADAPTER_MODULE == "apps.api.readonly_validation_probe"


def test_display_route_smoke_forces_safe_env_and_bounded_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_env: list[dict[str, str | None]] = []

    def fake_create_app(env: dict[str, str] | None = None) -> FastAPI:
        app = FastAPI()
        app.state.create_app_env = env

        @app.api_route("/{path:path}", methods=["GET"])
        def catch_all(path: str) -> dict[str, Any]:
            observed_env.append(
                {
                    "local_logs": os.environ.get("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS"),
                    "database_url": os.environ.get("DATABASE_URL"),
                    "pgoptions": os.environ.get("PGOPTIONS"),
                    "service_role": os.environ.get("NHMS_SERVICE_ROLE"),
                }
            )
            name = _route_name_for_path(f"/{path}")
            body: dict[str, Any] = {"status": "ok"}
            if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
                identity = {
                    "source": "GFS",
                    "cycle_time": "2026-05-03T00:00:00+00:00",
                    "run_id": "run_routes",
                    "model_id": "model_routes",
                }
                if name == "job_logs":
                    identity["job_id"] = "job_routes"
                body["data"] = {"identity": identity}
            return body

        return app

    monkeypatch.setenv("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS", "true")
    monkeypatch.setattr("apps.api.main.create_app", fake_create_app)
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("safe-env"),
        database_url="postgresql://readonly:secret@db.example/nhms?connect_timeout=0&sslmode=require",
    )

    results = run_display_route_smoke(
        config,
        {
            "source": "GFS",
            "cycle_time": "2026-05-03T00:00:00+00:00",
            "run_id": "run_routes",
            "model_id": "model_routes",
            "job_id": "job_routes",
        },
    )

    assert all(item["status"] == "PASS" for item in results)
    assert observed_env
    assert {item["local_logs"] for item in observed_env} == {"false"}
    assert {item["service_role"] for item in observed_env} == {"display_readonly"}
    assert all("connect_timeout=5" in str(item["database_url"]) for item in observed_env)
    assert all("statement_timeout%3D10000" in str(item["database_url"]) for item in observed_env)
    assert {item["pgoptions"] for item in observed_env} == {
        "-c statement_timeout=10000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
    }


def test_runbook_command_uses_evidence_root_without_double_nested_run_id() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "two-node-production-e2e-plan.md").read_text(encoding="utf-8")

    assert 'EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"' in runbook
    assert 'EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"' in runbook
    assert '--evidence-root "$EVIDENCE_PARENT"' in runbook
    assert '--run-id "$EVIDENCE_RUN_ID"' in runbook
    assert '--evidence-root "artifacts/two-node-e2e/$EVIDENCE_RUN_ID"' not in runbook

    create_command = 'install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"'
    mode_check = 'if [ "$readonly_secret_mode" != "600" ]; then'
    source_command = '. "$READONLY_SECRET_SOURCE"'
    validator_command = "uv run python scripts/validate_readonly_db_boundary.py"
    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in runbook
    assert "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" in runbook
    assert create_command in runbook
    assert mode_check in runbook
    assert runbook.index(create_command) < runbook.index(source_command)
    assert runbook.index(mode_check) < runbook.index(source_command)
    assert runbook.index(source_command) < runbook.index(validator_command)


def test_stat_dialect_substitution_table_is_guard_equivalent(tmp_path: Path) -> None:
    """The shim's three pairs answer identically inside the guards' input domain.

    On a platform carrying only one dialect the available one is exercised
    (both sides are asserted against the same independently-known expected
    values, so a cross-dialect divergence reds wherever it can be observed).

    The one recorded boundary: BSD ``%Lp`` drops setuid/setgid/sticky, so the
    pair is guard-equivalent on the permission bits only — pinned below with a
    04600 scratch file (0o4600 specifically: setuid survives ``chmod``
    regardless of group membership, while macOS silently clears the setgid bit
    when the file's group is one the caller does not belong to).  The guards
    themselves only ever chmod high-bit-free modes (0600/0644/0664), where the
    pair is exact.
    """
    assert STAT_DIALECT_SUBSTITUTIONS == (
        ("stat -c '%a'", "stat -f '%Lp'"),
        ("stat -c '%U'", "stat -f '%Su'"),
        ("stat -c '%A'", "stat -f '%Sp'"),
    )
    gnu_stat = _gnu_stat_available()
    bsd_stat = _bsd_stat_available()
    assert gnu_stat or bsd_stat, "neither the GNU nor the BSD stat dialect is available"

    secret_like = tmp_path / "mode-0600.env"
    secret_like.write_text("x\n", encoding="utf-8")
    secret_like.chmod(0o600)
    unit_like = tmp_path / "mode-0664.service"
    unit_like.write_text("[Service]\n", encoding="utf-8")
    unit_like.chmod(0o664)
    owner = secret_like.owner()

    if gnu_stat:
        assert _stat_output("-c", "%a", secret_like) == "600"
        assert _stat_output("-c", "%a", unit_like) == "664"
        assert _stat_output("-c", "%U", secret_like) == owner
        gnu_symbolic = _stat_output("-c", "%A", unit_like)
        assert gnu_symbolic == "-rw-rw-r--"
        assert gnu_symbolic[5] == "w"  # the guard's ${perms:5:1} group-write slot
        assert gnu_symbolic[8] == "-"  # the guard's ${perms:8:1} world-write slot

    if bsd_stat:
        assert _stat_output("-f", "%Lp", secret_like) == "600"
        assert _stat_output("-f", "%Lp", unit_like) == "664"
        assert _stat_output("-f", "%Su", secret_like) == owner
        bsd_symbolic = _stat_output("-f", "%Sp", unit_like)
        assert bsd_symbolic == "-rw-rw-r--"
        assert bsd_symbolic[5] == "w"
        assert bsd_symbolic[8] == "-"

        high_bit_file = tmp_path / "mode-04600.env"
        high_bit_file.write_text("x\n", encoding="utf-8")
        high_bit_file.chmod(0o4600)
        assert high_bit_file.stat().st_mode & 0o7777 == 0o4600
        assert _stat_output("-f", "%Lp", high_bit_file) == "600"
        if gnu_stat:
            assert _stat_output("-c", "%a", high_bit_file) == "4600"


def test_readonly_secret_source_guard_blocks_readable_file_before_source_or_validator(tmp_path: Path) -> None:
    secret_source = tmp_path / "display-readonly-secrets.env"
    secret_source.write_text("touch sourced-sentinel\n", encoding="utf-8")
    secret_source.chmod(0o644)
    tmp_path_q = shlex.quote(str(tmp_path))
    secret_source_q = shlex.quote(str(secret_source))

    script = _portable_stat_script(
        f"""
set -u
cd {tmp_path_q}
READONLY_SECRET_SOURCE={secret_source_q}
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {{
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
set -a
. "$READONLY_SECRET_SOURCE"
set +a
touch validator-sentinel
"""
    )

    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    # The named branch is the 0644 mode refusal — not the "cannot stat" nor the
    # "not a regular file" refusal, which would also satisfy a bare "BLOCKED:".
    assert f"BLOCKED: {secret_source} must be mode 0600 before sourcing" in result.stderr
    assert f"BLOCKED: cannot stat {secret_source} before sourcing" not in result.stderr
    assert "must be a regular 0600 file before sourcing" not in result.stderr
    assert not (tmp_path / "sourced-sentinel").exists()
    assert not (tmp_path / "validator-sentinel").exists()


def test_readonly_secret_source_guard_preserves_existing_secret_file(tmp_path: Path) -> None:
    secret_source = tmp_path / "display-readonly-secrets.env"
    secret_content = "NHMS_DISPLAY_READONLY_DATABASE_URL=postgresql://readonly:secret@db.example/nhms\n"
    secret_source.write_text(secret_content, encoding="utf-8")
    secret_source.chmod(0o600)
    secret_source_q = shlex.quote(str(secret_source))

    script = _portable_stat_script(
        f"""
set -u
READONLY_SECRET_SOURCE={secret_source_q}
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {{
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
"""
    )

    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert secret_source.read_text(encoding="utf-8") == secret_content


def test_operator_auth_source_guard_blocks_readable_file_before_source_or_header(tmp_path: Path) -> None:
    env_dir = tmp_path / "infra" / "env"
    env_dir.mkdir(parents=True)
    operator_auth = env_dir / "operator-auth.env"
    operator_auth.write_text("touch sourced-auth-sentinel\nOPERATOR_AUTH_TOKEN=secret\n", encoding="utf-8")
    operator_auth.chmod(0o644)
    secret_dir = tmp_path / "operator-secret"
    tmp_path_q = shlex.quote(str(tmp_path))
    secret_dir_q = shlex.quote(str(secret_dir))

    script = _portable_stat_script(
        f"""
set -u
cd {tmp_path_q}
OPERATOR_SECRET_DIR={secret_dir_q}
block_operator_auth_source() {{
  echo "BLOCKED: $*" >&2
  exit 1
}}

if [ -f infra/env/operator-auth.env ]; then
  operator_auth_mode="$(stat -c '%a' infra/env/operator-auth.env)" || \\
    block_operator_auth_source "cannot stat infra/env/operator-auth.env before sourcing"
  if [ "$operator_auth_mode" != "600" ]; then
    block_operator_auth_source "infra/env/operator-auth.env must be mode 0600 before sourcing"
  fi
  . infra/env/operator-auth.env
else
  OPERATOR_AUTH_TOKEN=interactive-token
fi
: "${{OPERATOR_AUTH_TOKEN:?operator auth token required}}"

mkdir -p "$OPERATOR_SECRET_DIR"
OPERATOR_CURL_HEADER="$(mktemp "$OPERATOR_SECRET_DIR/operator-auth-header.XXXXXX")"
touch header-sentinel
"""
    )

    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    # The named branch is the 0644 mode refusal, not the "cannot stat" refusal.
    assert "BLOCKED: infra/env/operator-auth.env must be mode 0600 before sourcing" in result.stderr
    assert "BLOCKED: cannot stat infra/env/operator-auth.env before sourcing" not in result.stderr
    assert not (tmp_path / "sourced-auth-sentinel").exists()
    assert not secret_dir.exists()
    assert not (tmp_path / "header-sentinel").exists()


def test_docs_secret_source_snippets_use_fail_closed_guards() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "two-node-production-e2e-plan.md").read_text(encoding="utf-8")
    docker_readme = (REPO_ROOT / "infra" / "README.two-node-docker.md").read_text(encoding="utf-8")
    env_readme = (REPO_ROOT / "infra" / "env" / "README.md").read_text(encoding="utf-8")

    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in runbook
    assert 'readonly_secret_mode="$(stat -c \'%a\' "$READONLY_SECRET_SOURCE")" || {' in runbook
    assert 'if [ "$readonly_secret_mode" != "600" ]; then' in runbook
    assert "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" in runbook
    assert "test -f infra/env/display-readonly-secrets.env || install -m 0600" not in runbook
    assert 'test "$(stat -c \'%a\' infra/env/display-readonly-secrets.env)" = "600"' not in runbook

    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in docker_readme
    assert "block_operator_auth_source()" in docker_readme
    assert "operator_auth_mode=\"$(stat -c '%a' infra/env/operator-auth.env)\" || \\" in docker_readme
    assert 'if [ "$operator_auth_mode" != "600" ]; then' in docker_readme
    assert 'block_operator_auth_source "infra/env/operator-auth.env must be mode 0600 before sourcing"' in docker_readme
    assert 'test "$(stat -c \'%a\' infra/env/operator-auth.env)" = "600"' not in docker_readme
    assert docker_readme.index("block_operator_auth_source()") < docker_readme.index(". infra/env/operator-auth.env")
    assert docker_readme.index(". infra/env/operator-auth.env") < docker_readme.index("OPERATOR_CURL_HEADER")

    assert "use a fail-closed guard" in env_readme
    assert "prints `BLOCKED:`" in env_readme
    assert "exits before `source`" in env_readme


def test_systemd_source_trust_preflight_is_checked_in_and_authoritative() -> None:
    docker_readme = (REPO_ROOT / "infra" / "README.two-node-docker.md").read_text(encoding="utf-8")

    assert "scripts/validate_two_node_docker_source_trust.py" in docker_readme
    assert "--trusted-owner root --trusted-owner nhms-deploy" in docker_readme
    assert "--role compute" in docker_readme
    assert "--role display" in docker_readme
    assert "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service" in docker_readme
    assert "$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service" in docker_readme
    assert 'sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service"' in docker_readme
    assert 'sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service"' in docker_readme
    assert "source-trust preflight 是 authoritative gate" in docker_readme
    assert 'namei -l "$CHECKOUT_ROOT/infra" | tee "$NAMEI_EVIDENCE"' not in docker_readme
    assert "block_systemd_preflight()" not in docker_readme
    assert 'check_systemd_source_path "$path"' not in docker_readme
    assert 'test -z "$(find' not in docker_readme
    assert "sudo install -m 0644 infra/systemd/" not in docker_readme


def test_systemd_namei_awk_rejects_untrusted_owner_and_group_writable_components(tmp_path: Path) -> None:
    awk_script = r"""
BEGIN {
  split(trusted, trusted_users, /[[:space:]]+/)
  for (i in trusted_users) {
    if (trusted_users[i] != "") {
      allowed[trusted_users[i]] = 1
    }
  }
}
$1 ~ /^[bcdlps-]/ {
  owner = $2
  if (substr($1, 1, 1) == "l") {
    printf "BLOCKED: symlink path component rejected: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
  if (!(owner in allowed)) {
    printf "BLOCKED: untrusted owner on path component: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
  if (substr($1, 6, 1) == "w" || substr($1, 9, 1) == "w") {
    printf "BLOCKED: group/world-writable path component: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
}
END { exit bad }
"""
    namei_evidence = tmp_path / "systemd-checkout-namei.txt"
    namei_evidence.write_text(
        "\n".join(
            [
                "f: /opt/SHUD-NWM/infra",
                "drwxr-xr-x root root /",
                "drwxr-xr-x alice staff opt",
                "drwxrwxr-x root root SHUD-NWM",
                "drwxr-xr-x nhms-deploy docker infra",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["awk", "-v", "trusted=root nhms-deploy", awk_script, str(namei_evidence)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BLOCKED: untrusted owner on path component" in result.stderr
    assert "alice" in result.stderr
    assert "BLOCKED: group/world-writable path component" in result.stderr


def test_systemd_source_path_guard_rejects_group_world_writable_sources(tmp_path: Path) -> None:
    unit_source = tmp_path / "infra" / "systemd" / "nhms-display-compose.service"
    unit_source.parent.mkdir(parents=True)
    unit_source.write_text("[Service]\n", encoding="utf-8")
    unit_source.chmod(0o664)
    unit_source_q = shlex.quote(str(unit_source))
    sentinel_q = shlex.quote(str(tmp_path / "systemctl-sentinel"))

    script = _portable_stat_script(
        f"""
set -euo pipefail
TRUSTED_DOCKER_OPERATORS="$(id -un)"
block_systemd_preflight() {{
  echo "BLOCKED: $*" >&2
  exit 1
}}
is_trusted_docker_operator() {{
  case " $TRUSTED_DOCKER_OPERATORS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}}
check_systemd_source_path() {{
  path="$1"
  owner="$(stat -c '%U' "$path")" || block_systemd_preflight "cannot stat owner for $path"
  perms="$(stat -c '%A' "$path")" || block_systemd_preflight "cannot stat permissions for $path"
  if ! is_trusted_docker_operator "$owner"; then
    block_systemd_preflight "untrusted owner $owner on systemd source $path"
  fi
  if [ "${{perms:5:1}}" = "w" ] || [ "${{perms:8:1}}" = "w" ]; then
    block_systemd_preflight "group/world-writable systemd source $path has permissions $perms"
  fi
}}
check_systemd_source_path {unit_source_q}
touch {sentinel_q}
"""
    )

    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "BLOCKED: group/world-writable systemd source" in result.stderr
    assert not (tmp_path / "systemctl-sentinel").exists()


def test_psycopg_adapter_uses_bounded_validation_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    connect_calls: list[dict[str, Any]] = []

    class FakeCursor:
        rowcount = 0

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: object) -> None:
            del query

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_connect(*args: object, **kwargs: Any) -> FakeConnection:
        del args
        connect_calls.append(kwargs)
        return FakeConnection()

    monkeypatch.setattr(readonly_db_validation.psycopg2, "connect", fake_connect)
    adapter = PsycopgReadonlyDbProbeAdapter("postgresql://readonly:secret@db.example/nhms", ddl_suffix="timeouts")

    result = adapter.execute_probe(
        readonly_db_validation.PermissionProbeSpec(
            operation="DELETE",
            target=ProbeTarget("ops", "pipeline_event", "pipeline_event_audit"),
            command="DELETE FROM ops.pipeline_event WHERE FALSE",
        )
    )

    assert result.outcome == "succeeded"
    assert connect_calls
    assert connect_calls[0]["connect_timeout"] == 5
    assert connect_calls[0]["options"] == (
        "-c statement_timeout=10000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
    )


def test_display_retry_cancel_manual_action_ordering_does_not_construct_write_dependencies() -> None:
    results = run_display_manual_action_probes("run-display-ordering")

    assert {item["name"] for item in results} == {"display_retry_manual_action", "display_cancel_manual_action"}
    assert all(item["status"] == "PASS" for item in results)
    assert all(item["http_status"] == 409 for item in results)
    assert all(item["observed_error_code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED" for item in results)
    assert all(item["write_dependency_constructed"] is False for item in results)
    assert all(item["write_executed"] is False for item in results)
