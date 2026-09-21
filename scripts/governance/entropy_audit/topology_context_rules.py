"""Context-window allowance rules of the node-22 topology family.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Each rule
reads the surrounding context of a candidate drift line and decides whether it
is current drift or an allowed boundary: non-current documents, retirement and
rejection boundaries, compatibility-mirror contracts, guardrail/test meta,
pre-cutover historical evidence and archived/stopped state claims."""

from __future__ import annotations

import re

from scripts.governance.entropy_audit.topology_predicates import (
    _topology_context_has_negative_node22_writer_boundary,
    _topology_line_mentions_mirror,
    _topology_node22_db_writer_drift_clauses,
    _topology_normalized,
)


def _topology_line_has_node22_db_writer_drift(line: str, context: str) -> bool:
    candidate = _topology_normalized(context or line)
    return any(
        not _topology_node22_db_writer_clause_is_allowed(clause)
        for clause in _topology_node22_db_writer_drift_clauses(candidate)
    )


def _topology_node22_db_writer_context_is_allowed(line: str, context: str) -> bool:
    candidate = _topology_normalized(context or line)
    drift_clauses = _topology_node22_db_writer_drift_clauses(candidate)
    if not drift_clauses:
        return False
    return _topology_context_is_guardrail_or_test_meta(candidate) or all(
        _topology_node22_db_writer_clause_is_allowed(clause)
        for clause in drift_clauses
    )


def _topology_node22_db_writer_clause_is_allowed(clause: str) -> bool:
    normalized = _topology_normalized(clause)
    return (
        _topology_context_is_non_current(normalized)
        or _topology_context_has_negative_node22_writer_boundary(normalized)
    )


def _topology_line_has_node22_database_url_scan_drift(line: str, context: str) -> bool:
    line_lower = _topology_normalized(line)
    if "database_url" not in line_lower:
        return False
    context_lower = _topology_normalized(f"{line}\n{context}")
    if _topology_context_is_guardrail_or_test_meta(context_lower):
        return False
    has_node22_runtime = any(
        token in context_lower
        for token in (
            "/scratch/frd_muziyao/nwm",
            "compute.host.env",
            "node-22 checkout",
            "node22 checkout",
            "node-22 compute",
            "计算控制面",
        )
    )
    has_database_scan = any(
        token in context_lower
        for token in (
            "writer-or-readable-production-dsn",
            "production dsn",
            "db scan",
            "database scan",
            "hydro.",
            "met.",
        )
    )
    return has_node22_runtime and has_database_scan


def _topology_local_postgres_context_is_allowed(
    line: str,
    context: str,
    *,
    claim_context: str | None = None,
) -> bool:
    line_only_context = _topology_normalized(line)
    local_claim_context = _topology_normalized(claim_context or line)
    line_context = _topology_normalized(f"{line}\n{context}")
    if _topology_context_has_stale_pending_retirement_state(line_context):
        return False
    if _topology_context_is_pre_cutover_historical_evidence(line_context):
        return True
    if _topology_line_mentions_mirror(line_only_context):
        if _topology_context_is_guardrail_or_test_meta(line_context):
            return True
        if _topology_context_is_explicit_mirror_implementation(line_context):
            return True
        if _topology_context_is_compatibility_mirror_contract(line_context):
            return True
        if _topology_context_has_negative_mirror_boundary(line_context):
            return True
        return False
    if _topology_context_is_retirement_or_rejection_boundary(line_only_context):
        return True
    if _topology_context_has_current_node22_local_postgres_use(line_only_context) or (
        _topology_context_has_node22_local_postgres_use_action(line_only_context)
        and _topology_context_has_current_node22_local_postgres_use(local_claim_context)
    ):
        return False
    if _topology_context_has_complete_node22_local_pg_boundary(local_claim_context):
        return True
    if _topology_context_has_complete_node22_local_pg_boundary(line_context):
        return True
    if _topology_context_is_guardrail_or_test_meta(local_claim_context):
        return True
    if _topology_context_is_guardrail_or_test_meta(line_context):
        return True
    if _topology_context_is_retirement_or_rejection_boundary(local_claim_context):
        return True
    if _topology_context_is_retirement_or_rejection_boundary(line_context):
        return True
    if _topology_context_is_historical_receipt_evidence(line_context):
        return True
    if _topology_context_is_explicit_mirror_implementation(line_context):
        return True
    compatibility_context = _topology_normalized(f"{local_claim_context}\n{line_context}")
    if _topology_line_has_non_current_or_compatibility_marker(line) and (
        _topology_context_is_compatibility_mirror_contract(compatibility_context)
    ):
        return True
    return _topology_context_is_structured_node22_local_pg_boundary(line_context)


def _topology_context_is_non_current(context: str) -> bool:
    if ("design intent" in context or "设计意图" in context) and (
        "wording" in context
        or "phrase" in context
        or "措辞" in context
    ):
        return True
    if ("design intent" in context or "design-time role contract" in context or "设计意图" in context) and (
        "physical host assignment may differ" in context
        or "不反映当前" in context
        or "current deployment" in context
        or "当前物理部署不同" in context
        or "当前与设计" in context
    ):
        return True
    has_historical = any(
        token in context
        for token in (
            "design intent",
            "design-time role contract",
            "historical",
            "history",
            "legacy",
            "old local",
            "rollback",
            "retire",
            "retirement",
            "retired",
            "rollback archive",
            "rollback-only",
            "rollback only",
            "superseded",
            "deprecated",
            "历史",
            "旧",
            "已弃用",
            "历史排障",
            "设计意图",
        )
    )
    has_do_not_connect = any(
        token in context
        for token in (
            "do-not-connect",
            "do_not_connect",
            "do not connect",
            "do not use",
            "not current",
            "non-current",
            "no active db",
            "no active database",
            "not current topology",
            "out of current",
            "outside current",
            "not scheduler runtime env",
            "not scheduler-owned",
            "不要连",
            "不连任何活 db",
            "不连任何活 database",
            "不应连接",
            "不作为当前",
            "不用于当前",
            "不是当前",
        )
    )
    has_sunset = any(
        token in context
        for token in (
            "sunset",
            "removal",
            "remove",
            "delete",
            "archive",
            "archived",
            "stop",
            "stopping",
            "删除",
            "迁移",
        )
    )
    has_dependency_only = any(
        token in context
        for token in (
            "online only because",
            "retained only because",
            "kept only because",
        )
    )
    return (has_historical and (has_do_not_connect or has_sunset or has_dependency_only)) or (
        has_do_not_connect and has_sunset
    )


def _topology_context_has_stale_pending_retirement_state(context: str) -> bool:
    normalized = _topology_normalized(context)
    return any(
        token in normalized
        for token in (
            "pending removal",
            "pending_removal",
            "pending deletion",
            "pending_deletion",
            "已弃用待删",
            "待删",
            "待删除",
        )
    )


def _topology_context_has_current_node22_local_postgres_use(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negative_node22_writer_boundary(normalized):
        return False
    has_current = any(
        token in normalized
        for token in (
            "current",
            "active",
            "production state",
            "production db",
            "current checks",
            "当前",
            "生产",
        )
    )
    return has_current and _topology_context_has_node22_local_postgres_use_action(normalized)


def _topology_context_has_node22_local_postgres_use_action(context: str) -> bool:
    normalized = _topology_normalized(context)
    return any(
        token in normalized
        for token in (
            "use node-22 local postgresql",
            "connect to node-22 local postgresql",
            "query node-22 local postgresql",
            "read node-22 local postgresql",
            "use :55433",
            "connect to :55433",
            "query :55433",
            "read :55433",
            "使用 node-22",
            "连接 node-22",
            "查询 node-22",
        )
    )


def _topology_context_is_retirement_or_rejection_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_current_node22_local_postgres_use(normalized):
        return False
    if not (
        ":55433" in normalized
        or " 55433" in normalized
        or "node-22 historical postgresql" in normalized
        or "historical postgresql listener on node-22" in normalized
    ):
        return False
    if _topology_context_has_complete_node22_local_pg_boundary(normalized):
        return True
    return any(
        token in normalized
        for token in (
            "before stopping",
            "blocked",
            "blocker",
            "database_url absent",
            "db-free",
            "do not stop",
            "fails unless",
            "grep 55433",
            "guardrail",
            "shall not instruct",
            "shall not present",
            "no database_url",
            "no scheduler postgresql",
            "not scheduler runtime env",
            "not scheduler-owned",
            "reject",
            "rejection",
            "stop gate",
            "stopped only after",
            "without scheduler postgresql",
        )
    )


def _topology_context_is_compatibility_mirror_contract(context: str) -> bool:
    combined = _topology_normalized(context)
    has_mirror = "mirror" in combined or "镜像" in combined
    has_compatibility = any(token in combined for token in ("compatibility", "compatibility-only", "兼容"))
    has_explicit_dsn = any(
        token in combined
        for token in (
            "explicit dsn",
            "explicit-dsn",
            "explicit mirror dsn",
            "explicit node-22 dsn",
            "explicit transitional",
            "--node22-dsn-file",
            "owner-only file",
            "n22_dsn",
            "显式",
        )
    )
    has_sunset = any(
        token in combined
        for token in (
            "sunset",
            "sunset-bound",
            "removal",
            "removed",
            "fully removed",
            "remove this mirror",
            "remove the mirror",
            "delete this mirror",
            "delete the mirror",
            "移除",
        )
    )
    has_allow_flag = any(
        token in combined
        for token in (
            "archived-rollback allow flag",
            "archived rollback allow flag",
            "archived-rollback allow-flagged",
            "allow-flagged",
            "allow flagged",
            "nhms_allow_archived_node22_db_rollback_mirror",
            "--allow-archived-node22-db-rollback-mirror",
            "allow flag",
            "显式允许",
        )
    )
    has_archived = _topology_context_has_unnegated_archived_state(combined)
    has_stopped = _topology_context_has_unnegated_stopped_state(combined)
    return (
        has_mirror
        and has_compatibility
        and has_explicit_dsn
        and has_allow_flag
        and has_archived
        and has_stopped
        and has_sunset
    )


def _topology_context_has_negative_mirror_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    if not _topology_line_mentions_mirror(normalized):
        return False
    return any(
        token in normalized
        for token in (
            "must not use",
            "must not call",
            "must not invoke",
            "must not read",
            "never read",
            "never falls back",
            "no mirror fallback",
            "without invoking mirror",
            "does not rely on mirror",
            "mirror not used",
            "node22_mirror_used=false",
        )
    )


def _topology_line_has_non_current_or_compatibility_marker(line: str) -> bool:
    normalized = _topology_normalized(line)
    return any(
        token in normalized
        for token in (
            "compatibility",
            "compatibility-only",
            "historical",
            "do-not-connect",
            "do not connect",
            "not current",
            "non-current",
            "sunset",
            "removal",
            "explicit",
            "n22_dsn",
            "--node22-dsn-file",
            "兼容",
            "历史",
            "已弃用",
            "不要连",
            "显式",
        )
    )


def _topology_context_is_structured_node22_local_pg_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    return (
        (
            "node22_local_postgres" in normalized
            or "historical_node22_pg_status" in normalized
            or "node-22 local postgresql" in normalized
            or "postgresql listener on node-22" in normalized
        )
        and ":55433" in normalized
        and _topology_context_has_complete_node22_local_pg_boundary(normalized)
    )


def _topology_context_has_complete_node22_local_pg_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negated_archive_or_stop_state(normalized):
        return False
    has_node22_local_pg = (
        ":55433" in normalized
        or " 55433" in normalized
        or "node-22 local postgresql" in normalized
        or "node-22 local postgres" in normalized
        or "historical postgresql listener on node-22" in normalized
        or "postgresql listener on node-22" in normalized
    )
    has_historical = any(
        token in normalized
        for token in (
            "historical",
            "non-current",
            "not current",
            "outside current",
            "out of current",
            "retired",
            "rollback archive",
            "rollback-only",
            "rollback only",
            "历史",
        )
    )
    has_do_not_connect = any(
        token in normalized
        for token in (
            "do-not-connect",
            "do_not_connect",
            "do not connect",
            "do not use",
            "must not use",
            "must not be used",
            "not current",
            "non-current",
            "out of current",
            "outside current",
            "不要连",
            "不连任何活 db",
            "不连任何活 database",
            "不应连接",
            "不用于当前",
        )
    )
    has_archived = _topology_context_has_unnegated_archived_state(normalized)
    has_stopped = _topology_context_has_unnegated_stopped_state(normalized)
    return has_node22_local_pg and has_historical and has_do_not_connect and has_archived and has_stopped


def _topology_context_is_pre_cutover_historical_evidence(context: str) -> bool:
    normalized = _topology_normalized(context)
    has_pre_cutover_marker = any(
        token in normalized
        for token in (
            "before #836/#837",
            "before #836 and #837",
            "before #837",
            "pre-cutover",
            "pre cutover",
            "initial context",
            "historical pre-cutover",
            "historical pre-cutover evidence only",
        )
    )
    has_not_yet_retired_state = any(
        token in normalized
        for token in (
            "not yet archived/stopped",
            "not yet archived",
            "not archived/stopped yet",
            "still listening but unused",
            "stayed online during #836",
            "historical pre-cutover evidence only",
        )
    )
    has_current_action = (
        _topology_context_has_current_node22_local_postgres_use(normalized)
        or _topology_context_has_node22_local_postgres_use_action(normalized)
    )
    return has_pre_cutover_marker and has_not_yet_retired_state and not has_current_action


def _topology_context_has_unnegated_archived_state(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negated_archive_or_stop_state(normalized):
        return False
    return any(token in normalized for token in ("archived", "archive", "rollback-only", "rollback only", "归档"))


def _topology_context_has_unnegated_stopped_state(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negated_archive_or_stop_state(normalized):
        return False
    return any(
        token in normalized
        for token in (
            "stopped",
            "stop",
            "rollback-only",
            "rollback only",
            "exited",
            "not listening",
            "no listener",
            "停止",
        )
    )


def _topology_context_has_negated_archive_or_stop_state(context: str) -> bool:
    normalized = _topology_normalized(context)
    return bool(
        re.search(
            r"\b(?:not|never|no|without|lacks?|missing)\s+"
            r"(?:yet\s+)?(?:an?\s+)?(?:archive|archived|stop|stopped)"
            r"(?:\s+or\s+(?:archive|archived|stop|stopped))?(?:\s+yet)?\b",
            normalized,
        )
        or re.search(
            r"\bnot\s+(?:archive|archived)\s+or\s+(?:stop|stopped)(?:\s+yet)?\b",
            normalized,
        )
        or re.search(
            r"\b(?:archive|archived|stop|stopped)\s+is\s+not\b",
            normalized,
        )
    )


def _topology_context_is_guardrail_or_test_meta(context: str) -> bool:
    normalized = _topology_normalized(context)
    if (
        "node22-url" in normalized
        and any(token in normalized for token in ("parser.add_argument", "mirror.extend"))
        and not any(
            token in normalized
            for token in (
                "_write(",
                "assert ",
                "monkeypatch",
                "negative fixture",
                "guardrails flag",
                "guardrails_flag",
                "test_entropy",
            )
        )
    ):
        return False
    return any(
        token in normalized
        for token in (
            "static checks should flag",
            "static governance checks",
            "static guard positive fixture",
            "guard positive fixture",
            "guard reports",
            "reports a finding",
            "checks that flag active",
            "static guardrails flag",
            "assumptions are flagged",
            "marked do-not-connect",
            "must be marked historical",
            "guardrail tests",
            "guardrails flag",
            "guardrails so",
            "reports topology drift",
            "reports display-env writer drift",
            "production-topology-node22-db-writer",
            "production-topology-display-env-writer",
            "production-topology-node22-local-postgres",
            "database_url_endpoint_not_node27",
            "database_url_node22_historical_endpoint",
            "_node22_dsn_source_from_env",
            "_resolve_node22_source",
            "_missing_node22_dsn_report",
            "mirror_dsn_config",
            "mirror.append",
            "parser.add_argument",
            "node22mirrorsource",
            "node22mirrordsnmissing",
            "nhms_node22_dsn_source",
            "node22_historical_db_hosts",
            "node22_historical_db_port",
            "node22_dsn_file_invalid_reason",
            "node22_dsn_file_unsafe_reason",
            "node22_dsn_query_override_forbidden_reason",
            "node22_dsn_endpoint_not_archived_node22_reason",
            "node22_dsn_invalid_reason",
            "node22_dsn_username_missing_reason",
            "node22_dsn_password_missing_reason",
            "node22_mirror_failed_reason",
            "node22_rollback_mirror_not_allowed_reason",
            "database_url_username_missing_reason",
            "database_url_readonly_identity_reason",
            "database_url_password_missing_reason",
            "default_allowed_db_endpoints",
            "historical_node22_pg_status",
            "transitional_mirror_sunset",
            "_source_preflight",
            "_source_forbidden_report",
            "focused tests cover",
            "regression rows",
            "drift in current operational surfaces",
            "while allowing clearly historical evidence",
            "positive fixtures",
            "negative fixtures",
        )
    )


def _topology_context_is_historical_receipt_evidence(context: str) -> bool:
    normalized = _topology_normalized(context)
    if not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", normalized):
        return False
    if not any(token in normalized for token in ("receipt", "historical", "history", "当时", "历史")):
        return False
    return not any(
        token in normalized
        for token in (
            "current production says",
            "current production state",
            "current checks",
            "active database writer",
            "active db writer",
            "当前生产",
            "当前检查",
            "当前主库",
        )
    )


def _topology_context_is_explicit_mirror_implementation(context: str) -> bool:
    normalized = _topology_normalized(context)
    if not _topology_context_is_compatibility_mirror_contract(normalized):
        return False
    has_explicit_mirror = (
        _topology_line_mentions_mirror(normalized)
        and any(token in normalized for token in ("explicit", "--node22-dsn-file", "n22_dsn"))
    )
    has_explicit_mirror = has_explicit_mirror or (
        _topology_line_mentions_mirror(normalized)
        and "--allow-archived-node22-db-rollback-mirror" in normalized
    )
    return has_explicit_mirror and any(
        token in normalized
        for token in (
            "node22mirrorsource",
            "_resolve_node22_source",
            "mirror_dsn_config",
            "mirror.extend",
            "parser.add_argument",
            "node22mirrordsnmissing",
            "node22_rollback_mirror_not_allowed_reason",
            "node22_dsn_missing_reason",
            "source=\"env:n22_dsn\"",
            "source.startswith(\"file:\")",
            "help=\"explicit node-22",
        )
    )
