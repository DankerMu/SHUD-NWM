"""Topology guardrails: the node-22/node-27 database boundary (#1823 partition).

Active drift categories, node-27 reading node-22's active primary, the
coordinated/explicit negative forms that must be allowed, the writer-claim
wordings that must still be flagged, and the display-env authority chain
(indirect sources, normalized source paths, writer entrypoints and psql
mutations after a source). The archive-marker and declared-authority half is in
``tests/test_entropy_audit_topology_authority.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_gate_eligible_finding,
    _findings_by_check,
    _setup_clean_hard_gate_fixture,
    _write,
)


def test_entropy_audit_topology_guardrails_flag_active_drift_categories(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433 for active DB checks.
        """,
    )
    _write(
        tmp_path / "scripts/run-ingest.sh",
        """
        source infra/env/display.env
        uv run python scripts/node27_autopipeline.py
        """,
    )

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    findings_by_check = {
        str(finding["check_id"]): finding
        for finding in report["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    }

    assert set(findings_by_check) == {
        "production-topology-node22-db-writer",
        "production-topology-node22-local-postgres",
        "production-topology-display-env-writer",
    }
    assert all(finding["gate_eligible"] is True for finding in findings_by_check.values())
    assert report["metadata"]["hard_gate_status"] == "fail"
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_topology_guardrails_flag_node27_reads_node22_active_primary_db(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Current production operations.
        node-27 reads node-22 active primary database for display readiness.
        """,
    )

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "production-topology-node22-db-writer"
    ]

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 2)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])
    assert report["metadata"]["hard_gate_status"] == "fail"
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_topology_guardrails_allow_explicit_negative_node22_db_access(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Current production readiness runs without node-22 DB access to any active primary database writer.
        Current display readiness runs without querying an active node-22 database writer.
        """,
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_allow_json_coordinated_negative_node22_limit(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    evidence_path = tmp_path / "openspec" / "changes" / "check" / "evidence" / "receipt.json"
    _write(
        evidence_path,
        json.dumps(
            {"limits": ["No production env/unit/DB mutation or node22 access."]},
            indent=2,
        )
        + "\n",
    )

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    topology_findings = [
        finding for finding in report["findings"] if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []
    assert report["metadata"]["hard_gate_status"] == "pass"
    assert audit_repo_entropy._exit_code_for_report(report) == 0

@pytest.mark.parametrize(
    "statement",
    [
        "No production env/unit/DB mutation or node22 access.",
        "without database mutation or node-22 access.",
        "No DB mutation nor node22 access.",
        "without production database writes or node-22 access.",
    ],
)
def test_entropy_audit_topology_guardrails_allow_coordinated_negative_node22_db_access(
    tmp_path: Path,
    statement: str,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "docs/runbooks/current-production-ops.md", f"{statement}\n")

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    topology_findings = [
        finding for finding in report["findings"] if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []
    assert report["metadata"]["hard_gate_status"] == "pass"
    assert audit_repo_entropy._exit_code_for_report(report) == 0

@pytest.mark.parametrize(
    "statement",
    [
        "node22 is the active DB writer.",
        "No doubt: node22 is the active DB writer.",
        "No safeguards against node22 DB mutation.",
        "No production env/unit/DB mutation or node22 access node22 is the active DB writer.",
    ],
)
def test_entropy_audit_topology_guardrails_flag_non_negating_node22_writer_text(
    tmp_path: Path,
    statement: str,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "docs/runbooks/current-production-ops.md", f"{statement}\n")

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    findings = [
        finding for finding in report["findings"] if finding["check_id"] == "production-topology-node22-db-writer"
    ]

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])
    assert report["metadata"]["hard_gate_status"] == "fail"
    assert audit_repo_entropy._exit_code_for_report(report) == 1

def test_entropy_audit_topology_guardrails_do_not_allow_json_positive_after_coordinated_negative(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(
        tmp_path / "openspec" / "changes" / "check" / "evidence" / "receipt.json",
        json.dumps(
            {
                "limits": [
                    "No production env/unit/DB mutation or node22 access.",
                    "node22 is the active DB writer.",
                ]
            },
            indent=2,
        )
        + "\n",
    )

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    findings = [
        finding for finding in report["findings"] if finding["check_id"] == "production-topology-node22-db-writer"
    ]

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("openspec/changes/check/evidence/receipt.json", 4)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])
    assert report["metadata"]["hard_gate_status"] == "fail"
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_topology_guardrails_do_not_allow_active_claim_after_neighbor_negative(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Do not use node-22 as the active database writer.
        node-22 is the active database writer.
        Do not use node-22 local PostgreSQL on :55433 for current checks.
        Use node-22 local PostgreSQL on :55433 for current checks.
        """,
    )

    writer_findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")
    local_pg_findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in writer_findings] == [
        ("docs/runbooks/current-production-ops.md", 2)
    ]
    assert [(finding["evidence_path"], finding["line"]) for finding in local_pg_findings] == [
        ("docs/runbooks/current-production-ops.md", 3),
        ("docs/runbooks/current-production-ops.md", 4)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(writer_findings[0])
    _assert_unallowlisted_budget_counted_gate_eligible_finding(local_pg_findings[0])


def test_entropy_audit_topology_guardrails_flag_terse_node22_db_writer_text(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 writes DB state.
        22 写入 met.forcing_version 到 PostgreSQL。
        node-22 hosts active primary PostgreSQL.
        22 is the active DB writer.
        22 owns database mutation.
        node-22 writes PG state.
        node-22 是当前主库。
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [finding["line"] for finding in findings] == [1, 2, 3, 4, 5, 6, 7]
    assert all(finding["gate_eligible"] is True for finding in findings)


def test_entropy_audit_topology_guardrails_flag_wrapped_node22_writer_claim(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "active-topology" / "tasks.md",
        """
        node-22 is the active
        database writer for current NHMS production.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("openspec/changes/active-topology/tasks.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_flag_standalone_node22_wrapped_claims(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22
        is the active database writer for current NHMS production.

        | node-22 |
        | is the active database writer for current NHMS production |
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1),
        ("docs/runbooks/current-production-ops.md", 4),
    ]
    assert all(finding["gate_eligible"] is True for finding in findings)


def test_entropy_audit_topology_guardrails_flag_display_env_authority_and_indirect_source(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        "| job | authority |\n"
        "|---|---|\n"
        "| node-27 ingest | DATABASE_URL from infra/env/display.env for node-27 ingest |\n",
    )
    _write(
        tmp_path / "scripts/indirect-ingest.sh",
        """
        ENV_FILE=infra/env/display.env
        . "$ENV_FILE"
        uv run python scripts/node27_autopipeline.py
        """,
    )
    _write(
        tmp_path / "scripts/separated-mirror.sh",
        """
        source infra/env/display.env
        echo ready
        uv run python scripts/node27_mirror_forcing.py --run-id demo
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert {finding["evidence_path"] for finding in findings} == {
        "docs/runbooks/current-production-ops.md",
        "scripts/indirect-ingest.sh",
        "scripts/separated-mirror.sh",
    }
    assert all(finding["gate_eligible"] is True for finding in findings)


@pytest.mark.parametrize(
    ("relative_path", "source_lines", "expected_line"),
    [
        (
            "scripts/source-dot-slash.sh",
            ["source ./infra/env/display.env"],
            1,
        ),
        (
            "scripts/source-repo-root.sh",
            ['source "$REPO_ROOT/infra/env/display.env"'],
            1,
        ),
        (
            "scripts/source-split-quoted-repo-root.sh",
            ['source "${REPO_ROOT}"/infra/env/display.env'],
            1,
        ),
        (
            "scripts/source-checkout-root.sh",
            ['. "${CHECKOUT_ROOT}/infra/env/display.env"'],
            1,
        ),
        (
            "scripts/source-absolute.sh",
            ["source /home/nwm/NWM/infra/env/display.env"],
            1,
        ),
        (
            "scripts/source-parent-relative.sh",
            ["source ../NWM/infra/env/display.env"],
            1,
        ),
        (
            "scripts/source-alias.sh",
            ['DISPLAY_ENV="$REPO_ROOT/infra/env/display.env"', '. "$DISPLAY_ENV"'],
            2,
        ),
        (
            "scripts/source-split-quoted-alias.sh",
            ['DISPLAY_ENV="${REPO_ROOT}"/infra/env/display.env', '. "$DISPLAY_ENV"'],
            2,
        ),
    ],
)
def test_entropy_audit_topology_guardrails_normalize_display_env_source_paths(
    tmp_path: Path,
    relative_path: str,
    source_lines: list[str],
    expected_line: int,
) -> None:
    filler = [f"echo step-{index}" for index in range(8)]
    _write(
        tmp_path / relative_path,
        "\n".join([*source_lines, *filler, "uv run python scripts/node27_autopipeline.py\n"]),
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        (relative_path, expected_line)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_do_not_suppress_writer_command_with_negative_comment(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "scripts/run-ingest.sh",
        """
        source infra/env/display.env
        uv run python scripts/node27_autopipeline.py # must not fall back to display.env
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("scripts/run-ingest.sh", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_ignore_comment_only_display_env_shell_lines(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "scripts/commented-example.sh",
        """
        # source infra/env/display.env
        # uv run python scripts/node27_autopipeline.py
        echo "documented but inactive"
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert findings == []


@pytest.mark.parametrize(
    "writer_command",
    [
        "uv run python scripts/node27_ingest_run.py",
        "uv run python scripts/node27_refresh_coverage.py",
        "uv run python -m scripts.node27_ingest_run",
        "uv run python -m scripts.node27_refresh_coverage",
        "uv run python -m workers.model_registry.cli import-basins-registry",
        "nhms-model import-basins-registry",
    ],
)
def test_entropy_audit_topology_guardrails_flag_node27_writer_entrypoints_after_display_env_source(
    tmp_path: Path,
    writer_command: str,
) -> None:
    _write(
        tmp_path / "scripts/run-writer.sh",
        f"""
        source infra/env/display.env
        {writer_command}
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("scripts/run-writer.sh", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_flag_psql_mutation_after_display_env_source(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "scripts/psql-mutation.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" -c "insert into met.forcing_version(version_id) values ('demo')"
        """,
    )
    _write(
        tmp_path / "scripts/psql-ddl.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" -c "drop table if exists met.tmp_demo"
        """,
    )
    _write(
        tmp_path / "scripts/psql-file.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" -f mutate.sql
        """,
    )
    _write(
        tmp_path / "scripts/psql-heredoc.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" <<SQL
        delete from met.forcing_version where version_id = 'demo';
        SQL
        """,
    )
    _write(
        tmp_path / "scripts/psql-select.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" -c "select * from met.forcing_version limit 1"
        """,
    )
    _write(
        tmp_path / "scripts/psql-heredoc-select.sh",
        """
        source infra/env/display.env
        psql "$DATABASE_URL" <<SQL
        select * from met.forcing_version limit 1;
        SQL
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert {(finding["evidence_path"], finding["line"]) for finding in findings} == {
        ("scripts/psql-mutation.sh", 1),
        ("scripts/psql-ddl.sh", 1),
        ("scripts/psql-file.sh", 1),
        ("scripts/psql-heredoc.sh", 1),
    }
    assert all(finding["gate_eligible"] is True for finding in findings)


def test_entropy_audit_topology_guardrails_do_not_allow_display_env_source_after_prohibition(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Do not source infra/env/display.env for node-27 ingest.
        source infra/env/display.env
        uv run python scripts/node27_autopipeline.py
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-display-env-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 2)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_allow_non_current_and_readonly_contexts(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 local PostgreSQL :55433 is historical, do-not-connect for current NHMS
        production state, archived, and stopped rollback-only.
        The archived node-22 rollback mirror is compatibility-only, requires explicit
        DSN via N22_DSN or owner-only --node22-dsn-file plus an archived-rollback
        allow flag, is archived/stopped rollback-only, and has a sunset/removal path.
        """,
    )
    _write(
        tmp_path / "docs/runbooks/display-readonly-live-mvt.md",
        """
        Display API readonly runtime sources infra/env/display.env through
        scripts/ops/start-display-api.sh, serves display_readonly checks only,
        and has no writer credentials.
        """,
    )
    _write(
        tmp_path / "docs/runbooks/receipts/old.md",
        "Current NHMS production says node-22 is the active database writer on :55433.\n",
    )
    _write(
        tmp_path / "artifacts/drift.md",
        "Current NHMS production says node-22 is the active database writer on :55433.\n",
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_reject_pending_removal_after_retirement(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 local PostgreSQL :55433 is historical, do-not-connect for current NHMS
        production state, and pending removal.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_reject_incomplete_local_pg_boundary(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 local PostgreSQL :55433 is historical and archived.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_reject_do_not_connect_without_archive_stop(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 local PostgreSQL :55433 is historical and do not connect for current NHMS production.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_reject_negated_archived_stopped_boundary(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        node-22 local PostgreSQL :55433 is historical and do-not-connect, but not archived or stopped yet.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


@pytest.mark.parametrize("action", ["query :55433", "connect to :55433", "read :55433"])
def test_entropy_audit_topology_guardrails_reject_current_actions_inside_pre_cutover_context(
    tmp_path: Path,
    action: str,
) -> None:
    _write(
        tmp_path / "openspec/changes/node22-db-free-scheduler-state/design.md",
        f"""
        Initial context before #836/#837: the historical do-not-connect node-22
        PostgreSQL `:55433` rollback listener was not yet archived/stopped only
        because the scheduler still used it for lock/state/model reads.

        Current operator steps should {action} for production state checks.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("openspec/changes/node22-db-free-scheduler-state/design.md", 5)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_scan_current_runbook_after_historical_banner(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/display-readonly-live-mvt.md",
        """
        # display_readonly Live PostGIS MVT Runbook

        > Current topology warning: this runbook preserves historical receipt context;
        > do not treat node-22 `210.77.77.22:55433` as current display DB config;
        > it is historical do-not-connect archived/stopped rollback-only state.
        > Current active primary PostgreSQL is node-27 local `:55432`.

        ## Current operator steps

        Current NHMS production says node-22 is the active database writer.
        """,
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert [(finding["check_id"], finding["evidence_path"], finding["line"]) for finding in topology_findings] == [
        (
            "production-topology-node22-db-writer",
            "docs/runbooks/display-readonly-live-mvt.md",
            10,
        )
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(topology_findings[0])


def test_entropy_audit_topology_guardrails_do_not_allow_current_local_pg_use_after_retirement_banner(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/node22-db-retirement-runbook.md",
        """
        # Node-22 Historical DB Retirement Runbook

        This runbook tracks retirement of the historical do-not-connect PostgreSQL listener on
        node-22 `:55433`, archived and stopped as rollback-only state.

        ## Current operator steps

        Use node-22 local PostgreSQL on :55433 for current production state checks.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/node22-db-retirement-runbook.md", 8)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


@pytest.mark.parametrize(
    "relative_path",
    [
        "infra/env/node27-ingest.example",
        "scripts/node27_autopipeline.py",
        "scripts/node27_mirror_forcing.py",
    ],
)
def test_entropy_audit_topology_guardrails_do_not_file_allow_key_compatibility_surfaces(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _write(
        tmp_path / relative_path,
        """
        Compatibility-only archived node-22 rollback mirror requires explicit DSN via
        N22_DSN or owner-only --node22-dsn-file plus an archived-rollback allow flag,
        is archived/stopped rollback-only, and has a sunset/removal path.
        Current production DB checks should use :55433 for active state.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        (relative_path, 4)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_flag_current_use_after_compatibility_paragraph(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        "The archived node-22 rollback mirror is compatibility-only, requires explicit DSN via "
        "N22_DSN or owner-only --node22-dsn-file plus an archived-rollback allow flag, "
        "is archived/stopped rollback-only, and has a sunset/removal path.\n\n"
        "Current production DB checks should use :55433 for active state.\n",
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["check_id"], finding["line"]) for finding in findings] == [
        ("production-topology-node22-local-postgres", 3)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_reject_mirror_without_real_sunset_removal(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        "Compatibility-only archived/stopped node-22 rollback mirror requires explicit DSN via "
        "N22_DSN and the archived-rollback allow flag for pre-contract handoff packages.\n",
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/current-production-ops.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_flag_unmarked_rollback_mirror(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        "Current runbook: run the archived node-22 rollback mirror before node-27 ingest.\n",
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert len(findings) == 1
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


# #1707: the four lines the pre-narrowing fallback reported are inlined verbatim, not read
# from their source files, so these cases survive those files being reformatted.
