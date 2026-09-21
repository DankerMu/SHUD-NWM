"""Retired-path returns, allowlist reasons and family coverage (#1823 partition).

The archive-marker allowlist for retired path tokens (complete, incomplete and
absent), the tracked/untracked and force-added variants, the false-positive
floors (non-git roots, unavailable git metadata, active underscore paths), the
Slurm gateway route-leakage and OpenAPI drift signals, the agent-artifact
ownership scan, and the per-check-id hard-gate matrix.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_report_only_finding,
    _complete_archive_status_front_matter,
    _findings_by_check,
    _init_git,
    _setup_agent_artifact_drift,
    _setup_clean_hard_gate_fixture,
    _setup_placeholder_path_drift,
    _track_generated_artifact,
    _write,
)


def test_complete_archive_marker_allowlists_archived_retired_path_tokens_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "archived" / "m22.md",
        _complete_archive_status_front_matter("Historical evidence mentions apps/web.\n"),
    )
    _write(
        tmp_path / "docs" / "governance" / "LEGACY_DEAD_CODE_INVENTORY.md",
        "Inventory keeps workers/sbatch_templates as retired evidence.\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    archived = findings["docs/archived/m22.md"]
    assert archived["allowlist_reason"] == audit_repo_entropy.COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON
    assert archived["allowlist_key"] == "placeholder-path-token:complete-archive-status-marker"
    assert archived["allowlist_state"] == "allowlisted"
    assert archived["budget_counted"] is False
    assert archived["gate_eligible"] is False

    inventory = findings["docs/governance/LEGACY_DEAD_CODE_INVENTORY.md"]
    assert inventory["allowlist_key"] == "placeholder-path-token:governance-retired-placeholder-inventory"
    assert inventory["budget_counted"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_archived_retired_path_tokens_without_marker_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "archived" / "m22.md", "Historical evidence mentions apps/web.\n")

    findings = _findings_by_check(tmp_path, "placeholder-path-token")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/archived/m22.md"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_incomplete_archive_marker_retired_path_tokens_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "archived" / "m22.md",
        """
        ---
        status: archived
        superseded_by: none
        status_since: 2026-06-24
        archive_scope: whole-document
        retained_for: audit evidence
        ---
        Historical evidence mentions apps/web.
        """,
    )

    findings = _findings_by_check(tmp_path, "placeholder-path-token")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/archived/m22.md"
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])


def test_completed_governance_2_openspec_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path
        / "openspec"
        / "changes"
        / "governance-2-legacy-dead-code-retirement"
        / "tasks.md",
        "Completed evidence keeps apps/web as a retired placeholder path.\n",
    )
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    governed = findings["openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md"]
    assert (
        governed["allowlist_reason"]
        == "governed completed OpenSpec evidence documents retired placeholder paths"
    )
    assert governed["allowlist_key"] == (
        "placeholder-path-token:governed-completed-openspec-retired-placeholder-evidence"
    )
    assert governed["allowlist_state"] == "allowlisted"
    assert governed["budget_counted"] is False
    assert governed["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_key"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True
    assert active_doc["gate_eligible"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_governance_5_e1_fixture_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path
        / "openspec"
        / "changes"
        / "governance-5-e1-entropy-baseline-burndown"
        / "tasks.md",
        "Fixture evidence keeps apps/web and workers/sbatch_templates as retired path examples.\n",
    )
    _write(tmp_path / "docs" / "active.md", "Current docs still mention services/tile-publisher.\n")

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    governed = findings[
        "openspec/changes/governance-5-e1-entropy-baseline-burndown/tasks.md"
    ]
    assert (
        governed["allowlist_reason"]
        == "governed Governance-5 E1 fixture evidence documents retired placeholder paths"
    )
    assert governed["allowlist_key"] == (
        "placeholder-path-token:governed-governance-5-e1-fixture-evidence-documents-retired-placeholder-paths"
    )
    assert governed["allowlist_state"] == "allowlisted"
    assert governed["budget_counted"] is False
    assert governed["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_key"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True
    assert active_doc["gate_eligible"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_active_doc_retired_path_tokens_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = _findings_by_check(tmp_path, "placeholder-path-token")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/active.md"
    assert findings[0]["allowlist_reason"] is None
    assert findings[0]["allowlist_key"] is None
    assert findings[0]["allowlist_state"] == "unallowlisted"
    assert findings[0]["budget_counted"] is True
    assert findings[0]["gate_eligible"] is False


def test_slurm_gateway_retired_template_source_comment_is_allowlisted(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "services" / "slurm_gateway" / "config.py",
        "`workers/sbatch_templates/` was retired from the active tree in #363.\n",
    )
    _write(
        tmp_path / "docs" / "active.md",
        "Current docs still mention workers/sbatch_templates.\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    source_comment = findings["services/slurm_gateway/config.py"]
    assert source_comment["allowlist_reason"] == "source comment documents retired Slurm template path"
    assert source_comment["allowlist_key"] == (
        "placeholder-path-token:source-comment-documents-retired-slurm-template-path"
    )
    assert source_comment["allowlist_state"] == "allowlisted"
    assert source_comment["budget_counted"] is False
    assert source_comment["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True


def test_tracked_apps_web_file_emits_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "retired placeholder returned\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=tmp_path, check=True)

    report = audit_repo_entropy.build_report(tmp_path)
    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "placeholder-path-exists"
    ]

    assert len(findings) == 1
    finding = findings[0]
    assert finding["title"] == "Tracked retired path returned to active tree"
    assert finding["evidence_path"] == "apps/web/README.md"
    assert finding["allowlist_reason"] is None
    assert finding["allowlist_key"] is None
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert summary_counts["by_check_id"]["placeholder-path-exists"] == 1


@pytest.mark.parametrize("retired_prefix", audit_repo_entropy.RETIRED_ACTIVE_TREE_PREFIXES)
def test_tracked_file_under_each_retired_prefix_emits_retired_path_return_finding(
    tmp_path: Path,
    retired_prefix: str,
) -> None:
    _init_git(tmp_path)
    tracked_file = f"{retired_prefix}/README.md"
    _write(tmp_path / tracked_file, "tracked retired path returned\n")
    subprocess.run(["git", "add", tracked_file], cwd=tmp_path, check=True)

    report = audit_repo_entropy.build_report(tmp_path)
    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "placeholder-path-exists"
    ]

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == tracked_file
    assert findings[0]["description"] == (
        f"Tracked file `{tracked_file}` returned under retired active-tree prefix "
        f"`{retired_prefix}`."
    )
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert summary_counts["by_check_id"]["placeholder-path-exists"] == 1


def test_force_added_ignored_retired_worker_path_emits_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "workers/\n")
    _write(tmp_path / "workers" / "shud-runtime" / "README.md", "ignored but tracked\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", "workers/shud-runtime/README.md"], cwd=tmp_path, check=True)

    findings = _findings_by_check(tmp_path, "placeholder-path-exists")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "workers/shud-runtime/README.md"
    assert findings[0]["allowlist_state"] == "unallowlisted"
    assert findings[0]["budget_counted"] is True
    assert findings[0]["gate_eligible"] is False


def test_untracked_filesystem_retired_path_does_not_emit_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "untracked retired placeholder\n")

    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_active_underscore_paths_do_not_emit_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "workers" / "shud_runtime" / "__init__.py", "\n")
    _write(tmp_path / "workers" / "output_parser" / "__init__.py", "\n")
    _write(tmp_path / "services" / "tile_publisher" / "__init__.py", "\n")
    subprocess.run(
        [
            "git",
            "add",
            "workers/shud_runtime/__init__.py",
            "workers/output_parser/__init__.py",
            "services/tile_publisher/__init__.py",
        ],
        cwd=tmp_path,
        check=True,
    )

    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_non_git_root_does_not_emit_retired_path_return_false_positive(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "apps" / "web" / "README.md", "filesystem-only retired placeholder\n")

    report = audit_repo_entropy.build_report(tmp_path)

    assert not any(
        finding["check_id"] == "placeholder-path-exists"
        for finding in report["findings"]
    )


def test_unavailable_git_metadata_does_not_crash_or_emit_retired_path_return_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "tracked but git unavailable\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=tmp_path, check=True)
    real_run = subprocess.run

    def unavailable_git_ls_files(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and command[:2] == ["git", "ls-files"]:
            raise OSError("git metadata unavailable")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(audit_repo_entropy.subprocess, "run", unavailable_git_ls_files)

    report = audit_repo_entropy.build_report(tmp_path)

    assert not any(
        finding["check_id"] == "placeholder-path-exists"
        for finding in report["findings"]
    )

def test_slurm_gateway_route_leakage_finds_direct_business_route_decorators_and_path_literals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "services" / "slurm_gateway" / "app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/api/v1/models")
        def list_models():
            return []

        FRONTEND_PATH = "/static/assets"
        FORECAST_SERIES = "/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"
        """,
    )

    descriptions = {
        str(finding["description"])
        for finding in _findings_by_check(tmp_path, "slurm-gateway-route-leakage")
    }

    assert any("direct route decorator" in description for description in descriptions)
    assert any("path literal `/static/assets`" in description for description in descriptions)
    assert any("forecast-series" in description for description in descriptions)


def test_openapi_frontend_type_drift_emits_delegated_and_fingerprint_signals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )
    _write(
        tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts",
        """
        export interface paths {}
        export interface components {}
        """,
    )
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-delegated")
    signal_findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "tests/test_openapi_drift.py"
    assert findings[0]["allowlist_reason"] == "existing OpenAPI drift tests are the enforced contract oracle"
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-delegated:existing-contract-oracle-delegation"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False
    assert len(signal_findings) == 1
    assert signal_findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(signal_findings[0]["allowlist_reason"]).startswith("report-only fingerprint ")
    assert signal_findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-record"
    assert signal_findings[0]["budget_counted"] is False
    assert signal_findings[0]["gate_eligible"] is False


def test_openapi_frontend_type_drift_emits_presence_signal_when_artifact_missing(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-presence")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "openapi/nhms.v1.yaml"
    assert findings[0]["severity"] == "high"
    assert findings[0]["priority"] == "P1"


def test_agent_artifact_ownership_skips_doc_status_symlink_to_outside_file(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("entropy-outside") / "DOC_STATUS.md"
    outside.write_text(
        "\n".join(
            [
                ".agents/skills/**",
                ".codex/tmp/",
                ".codex/cache/",
                ".codex/evidence/",
                "apps/frontend/artifacts/**",
                "Root `artifacts/`",
                ".dockerignore",
            ]
        ),
        encoding="utf-8",
    )
    doc_status = tmp_path / "docs" / "governance" / "DOC_STATUS.md"
    doc_status.parent.mkdir(parents=True, exist_ok=True)
    doc_status.symlink_to(outside)

    findings = _findings_by_check(tmp_path, "agent-artifact-ownership-policy")

    assert {finding["description"] for finding in findings} == {
        f"`DOC_STATUS.md` does not mention expected ownership term `{term}`."
        for term in (
            ".agents/skills/**",
            ".codex/tmp/",
            ".codex/cache/",
            ".codex/evidence/",
            "apps/frontend/artifacts/**",
            "Root `artifacts/`",
            ".dockerignore",
        )
    }


@pytest.mark.parametrize(
    ("check_ids", "setup"),
    [
        (("qhh-diagnostic-token",), lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            ("paused-workflow-condition",),
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            ("broad-e2e-api-mock",),
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            ("stale-display-route-token",),
            lambda root: _write(root / "apps/frontend/src/routes.ts", 'const oldRoute = "/hydro-met";\n'),
        ),
        (
            ("placeholder-path-token", "placeholder-path-exists"),
            lambda root: _setup_placeholder_path_drift(root),
        ),
        (
            ("makefile-toolchain-discipline",),
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            ("slurm-gateway-route-leakage",),
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi.staticfiles import StaticFiles\n"
                "from apps.api.routes.forecast import router as forecast_router\n"
                "def attach(app):\n"
                "    app.include_router(forecast_router)\n",
            ),
        ),
        (
            (
                "agent-artifact-ownership-policy",
                "agent-artifact-ignore-policy",
                "tracked-generated-artifact",
            ),
            lambda root: _setup_agent_artifact_drift(root),
        ),
        (
            ("apps-api-layer-inversion",),
            lambda root: _write(root / "packages/common/bad_import.py", "from apps.api.main import create_app\n"),
        ),
        (
            (
                "production-topology-node22-db-writer",
                "production-topology-node22-local-postgres",
                "production-topology-display-env-writer",
            ),
            lambda root: (
                _write(
                    root / "docs/runbooks/current-production-ops.md",
                    """
                    Current NHMS production says node-22 is the active database writer.
                    Operators should connect to node-22 local PostgreSQL on :55433 for current checks.
                    """,
                ),
                _write(
                    root / "scripts/run-ingest.sh",
                    """
                    source infra/env/display.env
                    uv run python scripts/node27_autopipeline.py
                    """,
                ),
            ),
        ),
    ],
)
def test_entropy_audit_required_families_emit_positive_signals(
    tmp_path: Path,
    check_ids: tuple[str, ...],
    setup: Callable[[Path], object],
) -> None:
    setup(tmp_path)

    emitted = {str(finding["check_id"]) for finding in audit_repo_entropy.build_report(tmp_path)["findings"]}

    assert set(check_ids) <= emitted


@pytest.mark.parametrize(
    ("check_id", "setup"),
    [
        (
            "role-env-boundary",
            lambda root: _write(
                root / "apps" / "frontend" / ".env.example",
                "SLURM_GATEWAY_URL=http://gateway:8000\n",
            ),
        ),
        ("qhh-diagnostic-token", lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            "broad-e2e-api-mock",
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            "slurm-gateway-route-leakage",
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/api/v1/models')\n"
                "def list_models():\n"
                "    return []\n",
            ),
        ),
        ("openapi-frontend-types-presence", lambda root: (root / "apps/frontend/src/api/types.ts").unlink()),
        (
            "paused-workflow-condition",
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            "makefile-toolchain-discipline",
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            "agent-artifact-ownership-policy",
            lambda root: _write(root / "docs/governance/DOC_STATUS.md", "Governed docs placeholder.\n"),
        ),
        (
            "agent-artifact-ignore-policy",
            lambda root: _write(root / ".gitignore", "# missing generated artifact ignores\n"),
        ),
        (
            "production-topology-node22-db-writer",
            lambda root: _write(
                root / "docs/runbooks/current-production-ops.md",
                "Current NHMS production says node-22 is the active DB writer for hydro/met state.\n",
            ),
        ),
        (
            "production-topology-node22-local-postgres",
            lambda root: _write(
                root / "docs/runbooks/current-production-ops.md",
                "Use node-22 local PostgreSQL on :55433 for current production state checks.\n",
            ),
        ),
        (
            "production-topology-display-env-writer",
            lambda root: _write(
                root / "scripts/run-ingest.sh",
                "source infra/env/display.env\nuv run python scripts/node27_autopipeline.py\n",
            ),
        ),
        ("tracked-generated-artifact", lambda root: _track_generated_artifact(root)),
    ],
)
def test_entropy_audit_hard_gate_fails_for_each_gated_check_id(
    tmp_path: Path,
    check_id: str,
    setup: Callable[[Path], object],
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    setup(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]
    gated_findings = [finding for finding in report["findings"] if finding["check_id"] == check_id]

    assert gated_findings
    assert metadata["hard_gate_status"] == "fail"
    assert metadata["hard_gate_failing_count"] == len(
        [
            finding
            for finding in report["findings"]
            if finding["gate_eligible"]
        ]
    )
    assert all(
        finding["check_id"] in audit_repo_entropy.HARD_GATE_CHECK_IDS
        for finding in report["findings"]
        if finding["gate_eligible"]
    )
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_hard_gate_keeps_delegated_and_fingerprint_openapi_signals_report_only(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]

    assert {
        "openapi-frontend-types-delegated",
        "openapi-frontend-types-signal",
    } <= {finding["check_id"] for finding in report["findings"]}
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert "openapi-frontend-types-delegated" not in metadata["hard_gate_gated_check_ids"]
    assert "openapi-frontend-types-signal" not in metadata["hard_gate_gated_check_ids"]
    for finding in report["findings"]:
        if finding["check_id"] in {"openapi-frontend-types-delegated", "openapi-frontend-types-signal"}:
            assert finding["allowlist_state"] == "allowlisted"
            assert finding["budget_counted"] is False
            assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/common/synthetic_api_import.py",
        "services/production_closure/synthetic_api_import.py",
    ],
)
def test_entropy_audit_apps_api_layer_inversion_remains_standalone_report_only_finding(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / relative_path, "from apps.api.routes.forecast import router\n")

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]
    layer_findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "apps-api-layer-inversion"
    ]

    assert len(layer_findings) == 1
    finding = layer_findings[0]
    assert finding["evidence_path"] == relative_path
    assert finding["axis"] == "structure"
    assert finding["governance_face"] == "role boundary"
    assert finding["role"] == "shared_contract"
    assert finding["owner_area"] == "layering"
    assert finding["priority"] == "P1"
    assert finding["severity"] == "high"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False
    assert "`apps.api.routes.forecast`" in finding["description"]
    assert "apps-api-layer-inversion" not in audit_repo_entropy.HARD_GATE_CHECK_IDS
    assert "apps-api-layer-inversion" not in metadata["hard_gate_gated_check_ids"]
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
