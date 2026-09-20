"""The three ``production-topology-*`` checks and their document authority rules.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Holds
``_check_production_topology_drift`` plus the helpers it calls directly: the
scan-file set, the finding builder, the archive/generated and non-current
document tests, the declared-current-authority front-matter parser, and the
context-window extractors (``_topology_line_context`` and the three fixed-width
wrappers over it)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from scripts.governance.entropy_audit.archive_status import _whole_document_archive_status_marker_range
from scripts.governance.entropy_audit.constants import ARCHIVE_STATUS_FRONT_MATTER_MAX_LINES
from scripts.governance.entropy_audit.repo_files import _iter_text_files, _module_for_path, _read_repo_text, _rel
from scripts.governance.entropy_audit.schema import FindingSpec
from scripts.governance.entropy_audit.topology_context_rules import (
    _topology_line_has_node22_database_url_scan_drift,
    _topology_line_has_node22_db_writer_drift,
    _topology_local_postgres_context_is_allowed,
    _topology_node22_db_writer_context_is_allowed,
)
from scripts.governance.entropy_audit.topology_display_env import (
    _topology_display_env_associated_writer,
    _topology_display_env_association_context,
    _topology_display_env_context_is_allowed,
    _topology_display_env_facts,
    _topology_display_env_file_allows_file_level_association,
)
from scripts.governance.entropy_audit.topology_predicates import (
    _topology_line_has_node22_local_postgres_or_mirror_drift,
    _topology_line_may_have_node22_db_writer_drift,
    _topology_line_may_start_node22_db_writer_claim,
    _topology_normalized,
)


def _check_production_topology_drift(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    declared_authorities = _topology_declared_current_authorities(root)
    for path in _production_topology_scan_files(root):
        rel = _rel(root, path)
        if _topology_path_is_archive_or_generated(rel):
            continue
        text = _read_repo_text(root, path)
        if not text:
            continue
        lines = text.splitlines()
        if _topology_document_is_non_current(rel, lines, declared_authorities=declared_authorities):
            continue
        display_env_facts = _topology_display_env_facts(lines)
        emitted_writer_claims: set[str] = set()
        emitted_display_contexts: set[str] = set()
        for index, line in enumerate(lines, start=1):
            if _topology_line_may_start_node22_db_writer_claim(line):
                writer_context = _topology_node22_writer_claim_context(lines, index)
                if (
                    _topology_line_has_node22_db_writer_drift(line, writer_context)
                    and not _topology_node22_db_writer_context_is_allowed(line, writer_context)
                    and writer_context not in emitted_writer_claims
                ):
                    emitted_writer_claims.add(writer_context)
                    findings.append(
                        _topology_finding(
                            root,
                            path,
                            line=index,
                            check_id="production-topology-node22-db-writer",
                            title="Active topology text assigns DB writer responsibility to node-22",
                            description=(
                                "An active topology surface describes node-22 as the current NHMS active DB writer."
                            ),
                            recommendation=(
                                "State that node-22 is compute/Slurm/artifact producer only; route active DB writes "
                                "and ingest validation to node-27."
                            ),
                        )
                    )
            has_node22_pg_or_mirror_drift = _topology_line_has_node22_local_postgres_or_mirror_drift(line)
            has_node22_database_url_scan_drift = False
            contract_context = ""
            if has_node22_pg_or_mirror_drift or "database_url" in _topology_normalized(line):
                contract_context = _topology_contract_context(lines, index)
                has_node22_database_url_scan_drift = _topology_line_has_node22_database_url_scan_drift(
                    line,
                    contract_context,
                )
            if has_node22_pg_or_mirror_drift or has_node22_database_url_scan_drift:
                claim_context = _topology_forward_claim_context(lines, index, after=4)
                if _topology_local_postgres_context_is_allowed(
                    line,
                    contract_context,
                    claim_context=claim_context,
                ):
                    continue
                findings.append(
                    _topology_finding(
                        root,
                        path,
                        line=index,
                        check_id="production-topology-node22-local-postgres",
                        title="Node-22 local PostgreSQL or rollback mirror lacks non-current boundary",
                        description=(
                            "An active topology surface mentions node-22 local PostgreSQL, port :55433, or a "
                            "node-22 rollback mirror without the required archived/stopped compatibility wording."
                        ),
                        recommendation=(
                            "Mark node-22 local PostgreSQL as historical and do-not-connect, and keep any "
                            "rollback mirror explicit-DSN, archived-rollback-flagged, compatibility-only, "
                            "and sunset-bound."
                        ),
                    )
                )
        for display_env_source in display_env_facts.sources:
            display_context = _topology_display_env_context(lines, display_env_source.line_no)
            associated_writer = _topology_display_env_associated_writer(
                display_env_facts,
                display_env_source.line_no,
                allow_file_level=_topology_display_env_file_allows_file_level_association(rel),
            )
            if associated_writer is None:
                continue
            association_context = _topology_display_env_association_context(
                lines,
                display_env_source.line_no,
                associated_writer.line_no,
            )
            display_allow_context = _topology_normalized(f"{display_context}\n{association_context}")
            if association_context in emitted_display_contexts:
                continue
            if _topology_display_env_context_is_allowed(display_allow_context):
                continue
            emitted_display_contexts.add(association_context)
            findings.append(
                _topology_finding(
                    root,
                    path,
                    line=display_env_source.line_no,
                    check_id="production-topology-display-env-writer",
                    title="Display runtime env is reused for data-plane writer or mirror authority",
                    description=(
                        "An active script or runbook sources infra/env/display.env for a data-plane writer or "
                        "archived rollback mirror path."
                    ),
                    recommendation=(
                        "Use the node-27 ingest env for writer work and an explicit mirror DSN for "
                        "compatibility-only mirror work; keep display.env limited to display_readonly runtime."
                    ),
                )
            )
    return findings


def _production_topology_scan_files(root: Path) -> Iterable[Path]:
    roots = [
        root / "scripts",
        root / "infra" / "env",
        root / "instructions" / "agents",
        root / "docs" / "governance",
        root / "docs" / "runbooks",
        root / "openspec" / "changes",
        root / "openspec" / "specs",
    ]
    files = [
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "infra" / "README.two-node-docker.md",
        root / "openspec" / "project-profile.md",
    ]
    yield from _iter_text_files(root, [*roots, *files])


def _topology_finding(
    root: Path,
    path: Path,
    *,
    line: int,
    check_id: str,
    title: str,
    description: str,
    recommendation: str,
) -> FindingSpec:
    return FindingSpec(
        check_id=check_id,
        title=title,
        axis="context",
        governance_face="production topology",
        role="shared_contract",
        evidence_path=_rel(root, path),
        line=line,
        severity="high",
        priority="P1",
        owner_area="production topology",
        module=_module_for_path(root, path),
        description=description,
        recommendation=recommendation,
    )


def _topology_path_is_archive_or_generated(relative_path: str) -> bool:
    parts = tuple(part.lower() for part in Path(relative_path).parts)
    if any(part in {"archived", "archive", "receipts", "receipt"} for part in parts):
        return True
    return relative_path.startswith("scripts/governance/")


def _topology_document_is_non_current(
    relative_path: str,
    lines: list[str],
    *,
    declared_authorities: frozenset[str] = frozenset(),
) -> bool:
    if relative_path in {
        "AGENTS.md",
        "CLAUDE.md",
        "docs/governance/ROLE_BOUNDARY.md",
        "docs/runbooks/current-production-ops.md",
        "openspec/project-profile.md",
    }:
        return False
    # A file that a complete whole-document marker names as the current
    # authority is itself a current production surface, so it cannot use its
    # own complete non-current marker to skip drift scanning.
    if relative_path in declared_authorities:
        return False
    # A complete whole-document archive-status marker is the authoritative
    # non-current declaration; it may legitimately coexist with a
    # current-production title elsewhere in the top region (for example a
    # preserved runbook that points at the current entrypoint).
    if _whole_document_archive_status_marker_range(lines) is not None:
        return True
    top_context = _topology_normalized("\n".join(lines[:80]))
    if "current production operations" in top_context or "当前生产值守" in top_context:
        return False
    top_lines = tuple(_topology_normalized(line) for line in lines[:80] if line.strip())
    if _topology_top_context_declares_whole_document_non_current(top_context, top_lines):
        return True
    return False


def _topology_declared_current_authorities(root: Path) -> frozenset[str]:
    """Repo-relative paths named as current authority by complete whole-document markers.

    Only a complete whole-document marker may grant authority: an incomplete
    marker cannot make preserved text safe to ignore, so it must not be able to
    turn another file into a current surface either. Paths that do not parse as
    well-formed repo-relative paths (absolute, backslash, parent escapes,
    tilde, empty, or malformed) are dropped so arbitrary strings cannot become
    authority. Returned values are normalized to the same repo-relative POSIX
    spelling that ``_rel`` produces so the set can be compared exactly.
    """
    declared: set[str] = set()
    for path in _production_topology_scan_files(root):
        rel = _rel(root, path)
        if _topology_path_is_archive_or_generated(rel):
            continue
        lines = _read_repo_text(root, path).splitlines()
        if _whole_document_archive_status_marker_range(lines) is None:
            continue
        closing_line = _archive_status_front_matter_closing_line(lines)
        if closing_line is None:
            continue
        for authority in _topology_authority_paths(lines[1:closing_line]):
            normalized = _topology_authority_path_normalize(authority)
            if normalized is not None:
                declared.add(normalized)
    return frozenset(declared)


def _archive_status_front_matter_closing_line(lines: list[str]) -> int | None:
    if not lines or lines[0].strip() != "---":
        return None
    max_index = min(len(lines), ARCHIVE_STATUS_FRONT_MATTER_MAX_LINES + 1)
    for index in range(1, max_index):
        if lines[index].strip() == "---":
            return index
    return None


def _topology_authority_paths(front_matter_lines: list[str]) -> tuple[str, ...]:
    """Extract ``- path: <value>`` list items from the current_authority block.

    Parses raw front-matter lines structurally: only top-level list items
    under the ``current_authority:`` key grant authority. The block ends at
    the next top-level (column-0) key. A path value is the first token of the
    list-item line after ``- path:`` (with optional single/double quotes).
    Section and reason text never grant authority because they are indented
    continuation lines, not ``- path:`` list items — even when their prose
    contains the literal ``- path:`` marker. A scalar ``current_authority:``
    value with no list shape grants nothing.
    """
    paths: list[str] = []
    in_authority_block = False
    for line in front_matter_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = line[:1] in {" ", "\t"}
        if indented or stripped.startswith("-"):
            if not in_authority_block:
                continue
            if stripped.startswith("- path:"):
                path_value = _topology_authority_list_item_path(stripped[len("- path:"):])
                if path_value:
                    paths.append(path_value)
            continue
        key, separator, _value = stripped.partition(":")
        if not separator:
            in_authority_block = False
            continue
        if key.strip().lower().replace("-", "_") == "current_authority":
            in_authority_block = True
            continue
        in_authority_block = False
    return tuple(paths)


def _topology_authority_list_item_path(value: str) -> str:
    value = value.strip()
    if value[:1] in {"'", '"'}:
        closing = value.find(value[0], 1)
        if closing == -1:
            return ""
        return value[1:closing].strip()
    token, _, _rest = value.partition(" ")
    return token.strip()


def _topology_authority_path_normalize(authority_path: str) -> str | None:
    """Normalize to repo-relative POSIX spelling, or None when not a valid path.

    Rejects empty, absolute, backslash, drive-letter, parent-escape, and
    tilde paths; collapses ``.`` segments and duplicate slashes so the value
    compares exactly with ``_rel`` output.
    """
    if not authority_path:
        return None
    if authority_path.startswith(("/", "\\", "~")):
        return None
    if re.match(r"^[A-Za-z]:[\\/]", authority_path):
        return None
    if "\\" in authority_path:
        return None
    parts: list[str] = []
    for part in authority_path.split("/"):
        if not part or part == ".":
            continue
        if part == ".." or part.startswith("~"):
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _topology_top_context_declares_whole_document_non_current(
    top_context: str,
    top_lines: tuple[str, ...],
) -> bool:
    whole_document_terms = (
        "this document",
        "this file",
        "this runbook",
        "entire document",
        "document preserves",
        "本文",
        "本文件",
        "本 runbook",
        "本runbook",
    )
    non_current_terms = (
        "not current",
        "non-current",
        "non current",
        "not current topology",
        "not current production topology",
        "not current production runbook",
        "不反映当前",
        "不作为当前",
        "不用于当前",
        "不是当前",
        "不是 current",
        "非当前",
        "当前部署事实不同",
        "当前物理部署不同",
    )
    design_or_superseded_terms = (
        "design intent",
        "设计意图",
        "historical",
        "history",
        "superseded",
        "deprecated",
        "archived",
        "历史",
        "已弃用",
    )
    has_whole_document_subject = any(term in top_context for term in whole_document_terms)
    has_non_current_marker = any(term in top_context for term in non_current_terms)
    has_design_or_superseded_marker = any(term in top_context for term in design_or_superseded_terms)
    if has_whole_document_subject and has_non_current_marker and has_design_or_superseded_marker:
        return True
    if "首跑" in top_context and "已迁移" in top_context and ("本文保留" in top_context or "保留" in top_context):
        return True
    return any(_topology_line_declares_document_superseded(line) for line in top_lines[:12])


def _topology_line_declares_document_superseded(line: str) -> bool:
    if not any(token in line for token in ("superseded", "deprecated", "archived", "historical", "历史")):
        return False
    return any(
        token in line
        for token in (
            "not current topology",
            "not current production topology",
            "not current production runbook",
            "retained for audit evidence",
            "audit evidence",
            "保留",
            "审计证据",
            "不用于当前",
            "不是当前",
        )
    )


def _topology_line_context(lines: list[str], line_no: int, *, before: int = 7, after: int = 7) -> str:
    start = max(0, line_no - before - 1)
    end = min(len(lines), line_no + after)
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_forward_claim_context(lines: list[str], line_no: int, *, after: int) -> str:
    start = line_no - 1
    end = min(len(lines), line_no + after)
    for index in range(start + 1, end):
        if not lines[index].strip():
            end = index
            break
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_node22_writer_claim_context(lines: list[str], line_no: int) -> str:
    line = lines[line_no - 1]
    stripped = line.rstrip()
    line_ends_claim = stripped.endswith((".", "。", "!", "！", "?", "？")) or bool(
        re.search(r'[.。!！?？]["\']\s*,?\s*$', stripped)
    )
    after = 0 if _topology_line_may_have_node22_db_writer_drift(line) and line_ends_claim else 2
    return _topology_line_context(lines, line_no, before=0, after=after)


def _topology_contract_context(lines: list[str], line_no: int) -> str:
    return _topology_local_block_context(lines, line_no, before=6, after=10)


def _topology_display_env_context(lines: list[str], line_no: int) -> str:
    return _topology_local_block_context(lines, line_no, before=6, after=6)


def _topology_local_block_context(lines: list[str], line_no: int, *, before: int, after: int) -> str:
    index = line_no - 1
    start = index
    before_remaining = before
    while start > 0 and before_remaining and lines[start - 1].strip():
        start -= 1
        before_remaining -= 1
    end = index + 1
    after_remaining = after
    while end < len(lines) and after_remaining and lines[end].strip():
        end += 1
        after_remaining -= 1
    return _topology_normalized("\n".join(lines[start:end]))
