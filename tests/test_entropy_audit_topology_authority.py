"""Topology guardrails: archive markers and declared authority (#1823 partition).

Non-current banners, superseded documents, the whole-document historical
baseline marker and its incomplete forms, the rollback-mirror contract, the
openspec/infra scan boundaries, and the dynamic-authority parser -- including
the cases proving a malformed or incomplete marker cannot declare authority or
self-exempt. The database-boundary half is in
``tests/test_entropy_audit_topology_db_boundary.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_gate_eligible_finding,
    _complete_archive_status_front_matter,
    _complete_historical_baseline_front_matter,
    _findings_by_check,
    _write,
)


@pytest.mark.parametrize(
    "line",
    [
        # docs/runbooks/production-ops/recalibration-and-archive.md:15
        # (#1103 moved it out of docs/runbooks/current-production-ops.md)
        "node-22 本地 scratch mirror 的机器；node-22 本身 DB-free，recalibration 模式只写",
        # openspec/specs/production-scheduler-orchestration/spec.md:103
        (
            "The manual cleanup CLI SHALL NOT delete cycle-scoped artifacts with less protection than "
            "the pass-side frontier exemption, and SHALL fail closed rather than fall back to "
            "unprotected wall-clock deletion when the frontier is unknown. It SHALL derive its active "
            "lower bound from the most recent scheduler pass evidence receipt's retention frontier block "
            "— excluding pre-execution reservation artifacts, selected by the receipt's recorded start "
            "time with a filename tie-break, and subject to a configurable freshness cap applied in both "
            "directions; a fresh receipt's explicit null bound SHALL be mirrored verbatim (the pass "
            "itself ran pure wall-clock, and the CLI is not stricter than the pass); a missing, "
            "unreadable, malformed, or stale receipt, a fresh receipt whose retention did not run "
            "(disabled or errored, leaving no frontier block), or any error while resolving or reading "
            "receipts SHALL force the cleanup into dry-run regardless of the execute flag, deleting "
            "nothing and recording a machine-readable frontier blocker reason in the cleanup CLI's "
            "output payload, with no bypass flag offered. The cleanup payload SHALL disclose which "
            "evidence directory was consulted: the frontier blocker carries an `evidence_dir` key "
            "holding the absolute path actually probed (explicitly null only when the directory itself "
            "could not be resolved), and the ok path carries the same key at the payload top level "
            "alongside the frontier-source field — so a silently mis-resolved workspace root (the "
            "relative default under a wrong working directory) is distinguishable from genuinely missing "
            "evidence without reading source code. The node-27 daily raw-retention process is an "
            "explicitly recorded exception to the protection-parity rule: it SHALL NOT adopt the receipt "
            "source — the pass receipts and journal live on node-22 private storage it cannot reach, and "
            "a cross-node frontier publication surface is out of this change's scope by recorded "
            "decision — and SHALL instead keep its display-watermark anchor while disclosing that "
            "decision: its summary SHALL carry an anchor block naming the anchor mode, the recorded "
            "decision, and the residual risk (backfill cycles older than the watermark minus the "
            "retention window are unprotected), and the process SHALL gain enabled and dry-run "
            "environment gates whose defaults preserve the current execute-only behaviour byte for byte "
            "(the removed dry-run CLI flags stay removed). The retention module's own no-bound "
            "wall-clock fallback is unchanged: the existing direct-invocation scenario continues to "
            "describe the module API's contract, and this requirement constrains the out-of-pass "
            "callers, which must now supply a bound or fail closed. The pass-side frontier requirement "
            "and receipt shape are unchanged by this requirement."
        ),
        # scripts/node22_clone_direct_grid_cutover_states.py:34
        "    node-22-local scratch mirror -- in one invocation.",
        # scripts/node22_clone_direct_grid_cutover_states.py:37
        "NFS canonical index and the node-22-local ``/scratch`` mirror, and it is",
        # The rollback leg recognises a lexeme, not a bare substring: scrollback is a
        # CI/terminal log buffer, and the leg bypasses DB-absence stripping, so an
        # over-match here cannot be suppressed by a same-line no-database disclaimer.
        "node-22 mirror 的 CI scrollback 缓冲区调大到 5000 行",
        # A frozen snapshot of the pre-#2028 wording of Requirement 1 in
        # openspec/changes/display-v2-national-timeline-precip-overlay/specs/
        # canonical-precip-copyback/spec.md. It byte-matched that file's line 4 when
        # #2044 (d92910bd) added it here; #2028 (07132645) then rewrote that line on
        # master and stranded the snapshot, and #2034 has since rewritten the
        # post-#2028 text again.
        # Inlined like the four above, but retained deliberately as corpus rather than as
        # a copy of the live spec: it is NOT expected to track that file and must not be
        # refreshed from it. An object-store copyback names a real rollback
        # (_copyback_object_tree_with_rollback) over files, not a database rollback
        # mirror, so the rollback leg must stand down when the line is explicitly an
        # object-store copy and names no database.
        (
            "After a successful q_down publish for `(source, cycle)`, the publisher on node-22 SHALL "
            "mirror `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/*.nc` and the "
            "referenced `canonical/<storage_source>/grid/<grid_id>/grid.json` (storage source "
            "`gfs`/`IFS` via `normalize_source_id`, cycle token `%Y%m%d%H`) from `OBJECT_STORE_ROOT` "
            "to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same keyspace using the existing "
            "temp-tree + rollback copy pattern. The mirror MUST be idempotent (a destination file "
            "with identical size is skipped) and MUST NOT fail the q_down publish when the source "
            "products are missing; the failure MUST be recorded in copyback lineage as "
            "`precip_mirror: failed` with the missing path."
        ),
        # The same carve-out in its Chinese and hyphenated surface forms.
        "node-22 的对象存储 copyback 镜像沿用 temp-tree + 回滚 拷贝模式",
        "the node-22 object-store rollback mirror copies canonical products only",
    ],
    ids=[
        "runbook-state-index-mirror",
        "spec-mirrored-verbatim",
        "clone-script-scratch-mirror",
        "clone-script-canonical-index",
        "ci-scrollback-buffer-not-rollback",
        "object-store-copyback-rollback-copy-pattern",
        "chinese-object-store-copyback-rollback-mirror",
        "hyphenated-object-store-rollback-mirror",
    ],
)
def test_entropy_audit_topology_mirror_fallback_ignores_non_database_mirror_lines(line: str) -> None:
    assert audit_repo_entropy._topology_line_has_node22_local_postgres_or_mirror_drift(line) is False


@pytest.mark.parametrize(
    "line",
    [
        "Rollback drill still points at the node-22 mirror on :55433 for current state.",
        "Operators should connect to node-22 local PostgreSQL mirror for current DB checks.",
        "Export N22_DSN before running the node-22 mirror sync.",
        "Pass owner-only --node22-dsn-file to the node-22 mirror helper.",
        "Current runbook: run the node-22 rollback mirror before node-27 ingest.",
        "Current runbook: run the node-22 roll-back mirror before node-27 ingest.",
        "当前手册：node-27 ingest 前先跑 node-22 回滚 mirror。",
        "node-22 hosts the active primary postgresql mirror, and that subsystem is DB-free.",
        "The node-22 rollback mirror is DB-free and takes no DB handle.",
        "node-22 本地库通过 mirror 实时同步给 node-27，生产查询直接读取该镜像",
        "node-22 hosts the warm standby that mirrors production writes from node-27",
        "node-22's local instance mirrors node-27 and is queried when the primary is busy",
        "Operators roll back via the node-22 mirror on demand",
        "State was rolled back from the node-22 mirror last cycle",
        "node-27 ingest 前先从 node-22 mirror 回退",
        "node-22 hosts a read replica that mirrors production writes from node-27",
        "node-22 的从库通过 mirror 对外提供读服务",
        "node-22 的备库通过 mirror 同步给 node-27",
        # 主库 is already a _topology_mentions_database token, so the database leg catches
        # this before the fallback tuple is reached. Pinned anyway: the behaviour must hold
        # whichever leg fires, so a future narrowing of that helper cannot silently drop it.
        "node-22 的主库通过 mirror 同步给 node-27",
        # The rest of the local-postgres tuple and the rest of the explicit-DSN family: the
        # object-store carve-out on the rollback leg must not reach any of these legs.
        "Operators still hit the node-22 local pg for current state.",
        "node-22 本地 pg 仍是当前查询入口",
        "node-22 本机 pg 仍是当前查询入口",
        "node-22 本地 postgresql 仍是当前查询入口",
        "node-22 本机 postgresql 仍是当前查询入口",
        "Set NODE22-URL before running the node-22 mirror sync.",
        "Pass node22_dsn_file=/owner-only/path to the node-22 mirror helper.",
        # rollback + mirror + a database token that survives DB-absence stripping, with no
        # :55433 to catch it earlier.
        "Run the node-22 rollback mirror database sync before node-27 ingest.",
        # The carve-out is bounded: object-store wording suppresses only the rollback leg,
        # so a real database token on the same line still reports.
        "The node-22 object_store rollback mirror also syncs its production database to node-27.",
        "node-22 object_store rollback mirror still exposes :55433 for current reads",
    ],
    ids=[
        "archived-port",
        "local-postgresql",
        "n22-dsn-with-mirror",
        "node22-dsn-file-with-mirror",
        "bare-rollback-mirror",
        "bare-roll-back-mirror",
        "bare-chinese-rollback-mirror",
        "fused-clause-real-db-token-plus-db-absence",
        "rollback-plus-db-absence",
        "chinese-local-db-mirror-read-as-production",
        "warm-standby-mirrors-production-writes",
        "local-instance-mirrors-and-is-queried",
        "spaced-roll-back-mirror",
        "rolled-back-from-mirror",
        "chinese-rollback-huitui-mirror",
        "read-replica-mirrors-production-writes",
        "chinese-congku-mirror-serves-reads",
        "chinese-beiku-mirror-syncs-to-node27",
        "chinese-zhuku-mirror-syncs-to-node27",
        "local-pg",
        "chinese-bendi-pg",
        "chinese-benji-pg",
        "chinese-bendi-postgresql",
        "chinese-benji-postgresql",
        "node22-url-with-mirror",
        "node22-dsn-file-underscore-with-mirror",
        "rollback-mirror-plus-database-token",
        "object-store-rollback-mirror-plus-database-token",
        "object-store-rollback-mirror-plus-archived-port",
    ],
)
def test_entropy_audit_topology_mirror_fallback_still_reports_rollback_and_database_lines(
    line: str,
) -> None:
    assert audit_repo_entropy._topology_line_has_node22_local_postgres_or_mirror_drift(line) is True


def test_entropy_audit_topology_guardrails_flag_node22_database_url_scan_runbook(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/forcing-copyback-backfill.md",
        """
        # Current Forcing Copyback Backfill

        In the node-22 checkout root:

        ```bash
        cd /scratch/frd_muziyao/NWM
        source infra/env/compute.host.env
        DATABASE_URL=<writer-or-readable-production-dsn>
        ```

        dry-run scans hydro.hydro_run and met.forcing_version.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("docs/runbooks/forcing-copyback-backfill.md", 8)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_flag_incomplete_mirror_implementation_contract(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "scripts/node27_mirror_forcing.py",
        """
        parser.add_argument("--node22-url", help="Explicit node-22 mirror DSN")
        mirror.extend(["--node22-url", node22_url])
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-local-postgres")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("scripts/node27_mirror_forcing.py", 1),
        ("scripts/node27_mirror_forcing.py", 2),
    ]
    assert all(finding["gate_eligible"] is True for finding in findings)


def test_entropy_audit_topology_guardrails_scan_active_openspec_but_skip_archive(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "active-topology" / "tasks.md",
        "Current NHMS production says node-22 is the active database writer.\n",
    )
    _write(
        tmp_path / "openspec" / "changes" / "archive" / "old-topology" / "tasks.md",
        "Current NHMS production says node-22 is the active database writer.\n",
    )
    _write(
        tmp_path / "openspec" / "specs" / "production-topology-contract" / "spec.md",
        "Current NHMS production says node-22 is the active database writer.\n",
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("openspec/changes/active-topology/tasks.md", 1),
        ("openspec/specs/production-topology-contract/spec.md", 1),
    ]


def test_entropy_audit_topology_guardrails_do_not_allow_active_openspec_meta_headings(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "active-topology" / "spec.md",
        """
        ## MODIFIED Requirements
        ### Requirement: node-22 is the active database writer.
        #### Scenario: 22 owns database mutation.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("openspec/changes/active-topology/spec.md", 2),
        ("openspec/changes/active-topology/spec.md", 3),
    ]
    assert all(finding["gate_eligible"] is True for finding in findings)


def test_entropy_audit_topology_guardrails_scan_infra_readme_without_non_current_banner(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "infra" / "README.two-node-docker.md",
        """
        # Two-node operator runbook

        Current NHMS production says node-22 is the active database writer.
        """,
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("infra/README.two-node-docker.md", 3)
    ]


def test_entropy_audit_topology_guardrails_skip_infra_readme_with_non_current_banner(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "infra" / "README.two-node-docker.md",
        """
        # Two-node Docker runbook

        This document preserves M22 design intent only and is not current
        production topology. Current deployment facts differ.

        Current NHMS production says node-22 is the active database writer.
        """,
    )

    findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert findings == []


def test_entropy_audit_topology_guardrails_allow_non_archive_superseded_documents(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "superseded-topology" / "tasks.md",
        """
        Historical / superseded: not current topology; retained for audit evidence.

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_allow_chinese_superseded_runbook_banner(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "two-node-production-e2e-plan.md",
        """
        # Two-Node Production-Like E2E Plan

        > 2026-06-22 status: historical / superseded M22 evidence plan.
        > 本文保留 M22 设计时代的两节点 E2E 证据边界，不是当前生产拓扑操作手册。

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_allow_historical_baseline_whole_document_marker_without_superseded_by(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/node22-legacy-bringup.md",
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: current node-22/node-27 production authority
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # Node-22 legacy bring-up

        > 当前生产与值守入口见 docs/runbooks/current-production-ops.md。

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    # The marker's own current_authority.section literal ("Current production
    # operations") sits inside the top region; the whole-document marker must
    # win over the current-production title heuristic, not the reverse.
    assert audit_repo_entropy._topology_document_is_non_current(
        "docs/runbooks/node22-legacy-bringup.md",
        (tmp_path / "docs/runbooks/node22-legacy-bringup.md").read_text(encoding="utf-8").splitlines(),
    ) is True

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_reject_current_looking_text_with_incomplete_historical_baseline_marker(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/node22-legacy-bringup.md",
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: current node-22/node-27 production authority
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # Node-22 legacy bring-up

        > 当前生产与值守入口见 docs/runbooks/current-production-ops.md。

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
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
            "docs/runbooks/node22-legacy-bringup.md",
            15,
        ),
        (
            "production-topology-node22-local-postgres",
            "docs/runbooks/node22-legacy-bringup.md",
            16,
        ),
    ]
    for finding in topology_findings:
        _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_entropy_audit_topology_guardrails_do_not_let_disguised_marker_hide_current_production_ops_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: current node-22/node-27 production authority
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # Current Production Operations Runbook

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
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
            "docs/runbooks/current-production-ops.md",
            14,
        ),
        (
            "production-topology-node22-local-postgres",
            "docs/runbooks/current-production-ops.md",
            15,
        ),
    ]
    for finding in topology_findings:
        _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_entropy_audit_topology_guardrails_dynamic_authority_cannot_self_exempt_with_complete_marker(
    tmp_path: Path,
) -> None:
    authority = "scripts/diagnostic/qhh/README.md"
    _write(
        tmp_path / "docs/runbooks/qhh-22-business-bringup.md",
        f"""
        ---
        status: historical baseline
        current_authority:
          - path: {authority}
            section: Diagnostic classification authority
            reason: current QHH diagnostic script classification
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # QHH node-22 business bring-up

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )
    # The target's own complete marker points at the canonical current runbook,
    # NOT at itself, so only the source marker's declaration can protect it.
    _write(
        tmp_path / authority,
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: current node-22/node-27 production authority
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # QHH Diagnostic Manifest

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    # The declared authority is not one of the five literal protected paths,
    # so it must not be able to use its own complete non-current marker to hide
    # current production topology drift.
    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert [(finding["evidence_path"], finding["line"]) for finding in topology_findings] == [
        (authority, 14),
        (authority, 15),
    ]
    for finding in topology_findings:
        _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_entropy_audit_topology_guardrails_incomplete_source_marker_cannot_declare_authority(
    tmp_path: Path,
) -> None:
    authority = "scripts/diagnostic/qhh/README.md"
    _write(
        tmp_path / "docs/runbooks/incomplete-source.md",
        f"""
        ---
        status: historical baseline
        current_authority:
          - path: {authority}
            section: Diagnostic classification authority
            reason: current QHH diagnostic script classification
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # Incomplete source marker
        """,
    )
    # The target's own complete marker points at the canonical current runbook,
    # so it is only non-current via its own marker and gains no protection from
    # the incomplete source.
    _write(
        tmp_path / authority,
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: current node-22/node-27 production authority
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical bring-up and incident evidence
        ---

        # QHH Diagnostic Manifest

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    # The source marker is missing status_since, so it is incomplete and must
    # not grant the target any authority: the target stays non-current (its own
    # complete marker is untouched) and its drift produces no finding. The
    # target's own marker naming the canonical current runbook does not make
    # the target itself a declared authority.
    assert audit_repo_entropy._topology_declared_current_authorities(tmp_path) == frozenset(
        {"docs/runbooks/current-production-ops.md"}
    )
    assert authority not in audit_repo_entropy._topology_declared_current_authorities(tmp_path)
    assert audit_repo_entropy._topology_document_is_non_current(
        authority,
        (tmp_path / authority).read_text(encoding="utf-8").splitlines(),
    ) is True

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]
    assert topology_findings == []


def test_entropy_audit_topology_authority_path_parser_rejects_malformed_and_escaping_paths() -> None:
    # Only top-level `- path:` list items under current_authority grant
    # authority; indented reason/section text never does, even when its prose
    # contains the literal `- path:` marker.
    assert audit_repo_entropy._topology_authority_paths(
        [
            "status: historical baseline",
            "current_authority:",
            "  - path: docs/runbooks/current-production-ops.md",
            "    section: Current production operations",
            "    reason: see - path: docs/evil.md for context",
            "  - path: scripts/diagnostic/qhh/README.md",
            "    reason: notes - path: /etc/passwd",
            "status_since: 2026-08-24",
        ]
    ) == (
        "docs/runbooks/current-production-ops.md",
        "scripts/diagnostic/qhh/README.md",
    )
    # A scalar current_authority value (no list shape) grants nothing.
    assert audit_repo_entropy._topology_authority_paths(
        [
            "current_authority: docs/governance/DOC_STATUS.md#conflict-resolution-order",
            "status: historical baseline",
        ]
    ) == ()
    # The block ends at the next top-level key.
    assert audit_repo_entropy._topology_authority_paths(
        [
            "current_authority:",
            "  - path: docs/runbooks/current-production-ops.md",
            "    reason: first",
            "status_since: 2026-08-24",
            "  - path: docs/runbooks/evil.md",
            "    reason: not under current_authority",
        ]
    ) == ("docs/runbooks/current-production-ops.md",)
    # Reasonable single/double quotes are honored.
    assert audit_repo_entropy._topology_authority_paths(
        ["current_authority:", '  - path: "docs/a.md"', "    reason: q"]
    ) == ("docs/a.md",)
    assert audit_repo_entropy._topology_authority_paths(
        ["current_authority:", "  - path: 'docs/b.md'", "    reason: q"]
    ) == ("docs/b.md",)

    for malformed in (
        "",
        "/etc/passwd",
        "\\etc\\passwd",
        "../escape.md",
        "docs/../../escape.md",
        "..\\escape.md",
        "~/home.md",
        "~user/home.md",
        "C:\\windows\\x.md",
        "C:/windows/x.md",
        ".",
        "..",
    ):
        assert (
            audit_repo_entropy._topology_authority_path_normalize(malformed) is None
        ), malformed

    for valid in (
        "docs/runbooks/current-production-ops.md",
        "scripts/diagnostic/qhh/README.md",
        "AGENTS.md",
    ):
        assert (
            audit_repo_entropy._topology_authority_path_normalize(valid) == valid
        ), valid

    # Normalization collapses `.` segments and duplicate slashes so the value
    # matches `_rel` spelling exactly.
    assert audit_repo_entropy._topology_authority_path_normalize(
        "docs/./runbooks//current-production-ops.md"
    ) == "docs/runbooks/current-production-ops.md"


def test_entropy_audit_topology_guardrails_malformed_declared_authority_is_not_adopted(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/source.md",
        """
        ---
        status: historical baseline
        current_authority:
          - path: /etc/passwd
            section: Not a repo path
            reason: malformed escape attempt
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical evidence
        ---

        # Source marker
        """,
    )
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        ---
        status: historical baseline
        current_authority:
          - path: docs/runbooks/current-production-ops.md
            section: Current production operations
            reason: self
        status_since: 2026-08-24
        archive_scope: whole-document
        retained_for: historical evidence
        ---

        Current NHMS production says node-22 is the active database writer.
        Operators should connect to node-22 local PostgreSQL on :55433.
        """,
    )

    # The malformed absolute authority path is dropped; the literal protected
    # current path still declares itself and surfaces its drift, but the
    # malformed path itself can never become a protected surface.
    assert audit_repo_entropy._topology_declared_current_authorities(tmp_path) == frozenset(
        {"docs/runbooks/current-production-ops.md"}
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert [(finding["evidence_path"], finding["line"]) for finding in topology_findings] == [
        ("docs/runbooks/current-production-ops.md", 12),
        ("docs/runbooks/current-production-ops.md", 13),
    ]
    for finding in topology_findings:
        _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_entropy_audit_archive_marker_parser_does_not_require_superseded_by_for_historical_baseline(
    tmp_path: Path,
) -> None:
    def marker_lines(marker_text: str) -> list[str]:
        return textwrap.dedent(marker_text).lstrip().splitlines()

    complete = audit_repo_entropy._whole_document_archive_status_marker_range(
        marker_lines(_complete_historical_baseline_front_matter())
    )
    assert complete is not None
    assert complete.start_line == 1
    assert complete.end_line == len(marker_lines(_complete_historical_baseline_front_matter()))

    incomplete = audit_repo_entropy._whole_document_archive_status_marker_range(
        marker_lines(
            _complete_historical_baseline_front_matter().replace("status_since: 2026-08-24", "")
        )
    )
    assert incomplete is None


def test_entropy_audit_archive_marker_parser_keeps_superseded_by_required_for_superseded_and_archived(
    tmp_path: Path,
) -> None:
    def marker_lines(marker_text: str) -> list[str]:
        return textwrap.dedent(marker_text).lstrip().splitlines()

    for status in ("superseded", "archived"):
        marker = audit_repo_entropy._whole_document_archive_status_marker_range(
            marker_lines(
                _complete_archive_status_front_matter("").replace(
                    "status: archived", f"status: {status}"
                )
            )
        )
        assert marker is not None, f"complete {status} marker without superseded_by must be recognized"

    for status in ("superseded", "archived"):
        marker = audit_repo_entropy._whole_document_archive_status_marker_range(
            marker_lines(
                _complete_archive_status_front_matter("")
                .replace("status: archived", f"status: {status}")
                .replace("superseded_by: none", "")
            )
        )
        assert marker is None, f"{status} marker missing superseded_by must stay incomplete"
