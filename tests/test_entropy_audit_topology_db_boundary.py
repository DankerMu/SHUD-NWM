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
import re
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from scripts.governance.entropy_audit import topology_predicates
from scripts.governance.entropy_audit.check_topology import _check_production_topology_drift
from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_gate_eligible_finding,
    _findings_by_check,
    _setup_clean_hard_gate_fixture,
    _write,
)

# #3349: git runs here (in-process or in children inheriting os.environ) only
# against tmp fixture repositories, never REPO_ROOT, so the whole module opts in.
pytestmark = pytest.mark.usefixtures("isolated_git_environ")

# The allowed negative sentences of the database-writer check. The #3358 cases
# below reuse them with the codename replaced by a compute host name.
NODE22_EXPLICIT_NEGATIVE_DB_ACCESS_SENTENCES = (
    "Current production readiness runs without node-22 DB access to any active primary database writer.",
    "Current display readiness runs without querying an active node-22 database writer.",
)
NODE22_COORDINATED_NEGATIVE_DB_ACCESS_SENTENCES = (
    "No production env/unit/DB mutation or node22 access.",
    "without database mutation or node-22 access.",
    "No DB mutation nor node22 access.",
    "without production database writes or node-22 access.",
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
        "\n".join(NODE22_EXPLICIT_NEGATIVE_DB_ACCESS_SENTENCES) + "\n",
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

@pytest.mark.parametrize("statement", NODE22_COORDINATED_NEGATIVE_DB_ACCESS_SENTENCES)
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


# --- #3358: the compute role named by its hosts -----------------------------
# The database-writer check and the compute-host leg of the local-PostgreSQL
# check recognise the compute role by the codename and by the two host names.

COMPUTE_HOSTS = ("w3", "w2")
COMPUTE_HOST_PAIRS = ("w3/w2", "w3 / w2", "w2/w3")
CURRENT_RUNBOOK = "docs/runbooks/current-production-ops.md"
DB_WRITER_CHECK = "production-topology-node22-db-writer"
LOCAL_POSTGRES_CHECK = "production-topology-node22-local-postgres"


def _topology_findings_for_lines(root: Path, *lines: str) -> list[tuple[str, str, int | None]]:
    _write(root / CURRENT_RUNBOOK, "\n".join(lines) + "\n")
    return [
        (finding.check_id, finding.evidence_path, finding.line)
        for finding in _check_production_topology_drift(root)
        if finding.check_id.startswith("production-topology-")
    ]


def _with_host(template: str, names: tuple[str, ...]) -> list[str]:
    return [template.format(h=name) for name in names]


@pytest.mark.parametrize(
    "statement",
    [
        *_with_host("Current NHMS production says {h} is the active database writer.", COMPUTE_HOSTS),
        *_with_host("Current NHMS production says {h} hosts the active primary PostgreSQL.", COMPUTE_HOSTS),
        *_with_host("当前生产 {h} 是主库，写入数据库。", COMPUTE_HOSTS),
        *_with_host("当前生产{h}是主库，写入数据库。", COMPUTE_HOSTS),
        *_with_host("nwm-{h} is the active database writer.", COMPUTE_HOSTS),
        *_with_host("{h} own the active primary database.", COMPUTE_HOST_PAIRS),
        *_with_host("slurmdbd on {h} is the active primary PostgreSQL writer.", COMPUTE_HOSTS),
        *_with_host("{h} runs slurmdbd and is the active database writer.", COMPUTE_HOSTS),
    ],
)
def test_entropy_audit_topology_guardrails_flag_compute_host_db_writer_claims(
    tmp_path: Path,
    statement: str,
) -> None:
    findings = _topology_findings_for_lines(tmp_path, statement)

    assert [finding for finding in findings if finding[0] == DB_WRITER_CHECK] == [
        (DB_WRITER_CHECK, CURRENT_RUNBOOK, 1)
    ]


@pytest.mark.parametrize("host", COMPUTE_HOSTS)
def test_entropy_audit_topology_guardrails_keep_codename_and_host_claims_apart(
    tmp_path: Path,
    host: str,
) -> None:
    findings = _topology_findings_for_lines(
        tmp_path,
        "Current NHMS production says node-22 is the active database writer.",
        f"Current NHMS production says {host} is the active database writer.",
    )

    assert [finding for finding in findings if finding[0] == DB_WRITER_CHECK] == [
        (DB_WRITER_CHECK, CURRENT_RUNBOOK, 1),
        (DB_WRITER_CHECK, CURRENT_RUNBOOK, 2),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        *_with_host("Operators should connect to {h} local PostgreSQL for active DB checks.", COMPUTE_HOSTS),
        *_with_host(
            "{h} local PostgreSQL is historical, do-not-connect for current NHMS production state, "
            "archived, and stopped rollback-only.",
            COMPUTE_HOSTS,
        ),
        *_with_host("使用 {h} 本地 PostgreSQL 做检查。", COMPUTE_HOSTS),
        *_with_host("Connect to {h} local PostgreSQL for current checks; do not use node-27.", COMPUTE_HOSTS),
        *_with_host("{h} local PostgreSQL never goes down.", COMPUTE_HOSTS),
        *_with_host("{h} 本地 PostgreSQL 没有密码。", COMPUTE_HOSTS),
        *_with_host(
            "node-22 local PostgreSQL on :55433 is historical, do-not-connect, archived and stopped; "
            "connect to {h} local PostgreSQL.",
            COMPUTE_HOSTS,
        ),
    ],
)
def test_entropy_audit_topology_guardrails_flag_compute_host_local_postgres(
    tmp_path: Path,
    statement: str,
) -> None:
    findings = _topology_findings_for_lines(tmp_path, statement)

    assert [finding for finding in findings if finding[0] == LOCAL_POSTGRES_CHECK] == [
        (LOCAL_POSTGRES_CHECK, CURRENT_RUNBOOK, 1)
    ]


def test_entropy_audit_topology_guardrails_codename_boundary_line_alone_is_allowed(
    tmp_path: Path,
) -> None:
    # The retired-host half of the one-finding row above, on its own: the
    # finding there comes from the compute-host clause, not from this wording.
    findings = _topology_findings_for_lines(
        tmp_path,
        "node-22 local PostgreSQL on :55433 is historical, do-not-connect, archived and stopped.",
    )

    assert findings == []


def _host_versions_of_allowed_negative_sentences() -> list[str]:
    sentences = (*NODE22_EXPLICIT_NEGATIVE_DB_ACCESS_SENTENCES, *NODE22_COORDINATED_NEGATIVE_DB_ACCESS_SENTENCES)
    return [
        re.sub(r"node[-_ ]?22", name, sentence)
        for sentence in sentences
        for name in (*COMPUTE_HOSTS, *COMPUTE_HOST_PAIRS)
    ]


@pytest.mark.parametrize(
    "statement",
    [
        *_host_versions_of_allowed_negative_sentences(),
        *_with_host("without database mutation or {h} access.", COMPUTE_HOST_PAIRS),
        "Reviewer W2: the writer test misses the database mutation path.",
        "Reviewer W2: the local PostgreSQL fixture is missing.",
        *_with_host("See https://www.{h}.org/TR/ for the database writer vocabulary.", COMPUTE_HOSTS),
        *_with_host("The {h}c validator is the active database writer of that report.", COMPUTE_HOSTS),
        "show30s is the active database writer flag.",
        *_with_host("slurmdbd on {h} writes Slurm accounting to its local database.", COMPUTE_HOSTS),
        *_with_host("{h} hosts slurmdbd with a local MariaDB database for Slurm accounting.", COMPUTE_HOSTS),
        *_with_host("{h} run no local PostgreSQL.", COMPUTE_HOST_PAIRS),
        *_with_host("Do not connect to a {h} local PostgreSQL; there is none.", COMPUTE_HOSTS),
        *_with_host("{h} 没有本地 PostgreSQL。", COMPUTE_HOSTS),
        *_with_host("{h} never connect to node-27 local PostgreSQL.", COMPUTE_HOST_PAIRS),
        *_with_host("The {h}-scheduler-journal mirror is not the active database writer.", COMPUTE_HOSTS),
        "docs/runbooks/w2-w3-compute-topology.md describes the active DB writer boundary.",
    ],
)
def test_entropy_audit_topology_guardrails_allow_compute_host_negatives_and_lookalikes(
    tmp_path: Path,
    statement: str,
) -> None:
    assert _topology_findings_for_lines(tmp_path, statement) == []


def _rewrite_compute_hosts(line: str) -> str:
    # Attribute access per call: a missing function fails each case, not the module import.
    return topology_predicates._topology_rewrite_compute_hosts(line)


@pytest.mark.parametrize("host", COMPUTE_HOSTS)
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{h} is up", " node-22  is up"),
        ("jobs run on {h}", "jobs run on  node-22 "),
        ("jobs run on {h}.", "jobs run on  node-22 ."),
        ("Slurm runs on ({h}) only", "Slurm runs on ( node-22 ) only"),
        ("ssh `{h}` first", "ssh ` node-22 ` first"),
        ("alias nwm-{h} reaches it", "alias nwm- node-22  reaches it"),
        ("当前生产{h}是主库", "当前生产 node-22 是主库"),
        ("当前生产 {h} 是主库", "当前生产  node-22  是主库"),
        ("{h}: control host", " node-22 : control host"),
        ("{h} - the control host", " node-22  - the control host"),
    ],
)
def test_topology_rewrite_compute_hosts_rewrites_host_tokens(host: str, template: str, expected: str) -> None:
    assert _rewrite_compute_hosts(template.format(h=host)) == expected


@pytest.mark.parametrize(
    "line",
    [
        "w3/w2 run Slurm",
        "w3 / w2 run Slurm",
        "w2/w3 run Slurm",
        "w3, w2 run Slurm",
        "w3、w2 run Slurm",
        "w3 and w2 run Slurm",
        "w3 or w2 run Slurm",
        "w3 和 w2 run Slurm",
    ],
)
def test_topology_rewrite_compute_hosts_rewrites_a_host_pair_to_one_codename(line: str) -> None:
    assert _rewrite_compute_hosts(line) == " node-22  run Slurm"


@pytest.mark.parametrize("host", COMPUTE_HOSTS)
@pytest.mark.parametrize(
    "template",
    [
        "Reviewer {H} notes",
        "See www.{h}.org for the vocabulary",
        "resolve {h}.internal first",
        "resolve host.{h} first",
        "the {h}c validator",
        "the {h}_dir path",
        "{h}-scheduler-journal",
        "nhms-{h}-journal-backup",
    ],
)
def test_topology_rewrite_compute_hosts_leaves_lookalikes_unchanged(host: str, template: str) -> None:
    line = template.format(h=host, H=host.upper())
    assert _rewrite_compute_hosts(line) == line


@pytest.mark.parametrize(
    "line",
    ["show30s flag", "no compute host is named here", "see docs/runbooks/w2-w3-compute-topology.md"],
)
def test_topology_rewrite_compute_hosts_leaves_lines_without_a_host_unchanged(line: str) -> None:
    assert _rewrite_compute_hosts(line) == line


# #1707: the four lines the pre-narrowing fallback reported are inlined verbatim, not read
# from their source files, so these cases survive those files being reformatted.
