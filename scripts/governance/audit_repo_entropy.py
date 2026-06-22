#!/usr/bin/env python3
"""Report-only repository entropy audit for Governance-4A.

The script intentionally emits signals instead of gates. It reads repository
files, classifies known governance drift families, and never writes
``.entropy-baseline/latest.json`` in the default report modes.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Literal

AXES = ("structure", "semantics", "behavior", "context", "protocol", "control")
CHECK_FAMILIES = (
    "role-env-boundary",
    "production-topology-node22-db-writer",
    "production-topology-node22-local-postgres",
    "production-topology-display-env-writer",
    "qhh-diagnostic-token",
    "paused-workflow-condition",
    "broad-e2e-api-mock",
    "stale-display-route-token",
    "placeholder-path-token",
    "placeholder-path-exists",
    "makefile-toolchain-discipline",
    "openapi-frontend-types-delegated",
    "openapi-frontend-types-presence",
    "openapi-frontend-types-signal",
    "slurm-gateway-route-leakage",
    "agent-artifact-ownership-policy",
    "agent-artifact-ignore-policy",
    "tracked-generated-artifact",
    "apps-api-layer-inversion",
)
RETIRED_ACTIVE_TREE_PREFIXES = (
    "apps/web",
    "workers/forcing-producer",
    "workers/shud-runtime",
    "workers/output-parser",
    "workers/flood-frequency",
    "workers/sbatch_templates",
    "services/tile-publisher",
)
HARD_GATE_CHECK_IDS = (
    "agent-artifact-ignore-policy",
    "agent-artifact-ownership-policy",
    "broad-e2e-api-mock",
    "makefile-toolchain-discipline",
    "openapi-frontend-types-presence",
    "paused-workflow-condition",
    "production-topology-display-env-writer",
    "production-topology-node22-db-writer",
    "production-topology-node22-local-postgres",
    "qhh-diagnostic-token",
    "role-env-boundary",
    "slurm-gateway-route-leakage",
    "tracked-generated-artifact",
)
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
PRIORITY_RANK = {"P3": 1, "P2": 2, "P1": 3, "P0": 4}
SCORE_RANK = {"low": 1, "medium": 2, "high": 3}
RANK_SCORE = {1: "low", 2: "medium", 3: "high"}
SCAN_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".cache",
    }
)
SCAN_SKIP_ROOT_DIRS = frozenset({"artifacts", "data"})
SCAN_SKIP_PREFIXES = (".nhms-",)
TEXT_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".css",
        ".env",
        ".example",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".lock",
        ".md",
        ".mjs",
        ".py",
        ".rst",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
MAX_SCANNED_TEXT_FILE_BYTES = 1_048_576
MAX_ARTIFACT_FINGERPRINT_BYTES = 1_048_576
HASH_CHUNK_BYTES = 1_048_576
LEGACY_DISPLAY_ROUTE_TOKENS = ("/hydro-met", "HydroMetPage")
LEGACY_DISPLAY_ROUTE_TOKEN_PATTERN = (
    r"(?P<token>"
    r"/(?:overview|hydro-met|forecast|meteorology|flood-alerts)"
    r"|/(?:basins|segments)/(?::[A-Za-z][A-Za-z0-9_-]*|[A-Za-z0-9][A-Za-z0-9_-]*)"
    r")"
    r"(?=$|[^A-Za-z0-9_/-])"
)
ROUTE_BASE_PLACEHOLDER_PATTERN = (
    r"(?:"
    r"\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|<[A-Za-z][A-Za-z0-9_-]*>"
    r")"
)
LEGACY_DISPLAY_ROUTE_PATTERN = re.compile(LEGACY_DISPLAY_ROUTE_TOKEN_PATTERN)
LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN = re.compile(
    r"(?:^|"
    + ROUTE_BASE_PLACEHOLDER_PATTERN
    + r"|[\s`'\"([{<|=?:&]|https?://[^/\s`'\"()<>{}]+)"
    + LEGACY_DISPLAY_ROUTE_TOKEN_PATTERN
)
ROUTE_VALUED_LEGACY_DISPLAY_ROUTE_PATTERN = re.compile(
    r"(?:"
    + ROUTE_BASE_PLACEHOLDER_PATTERN
    + r"|--[A-Za-z][A-Za-z0-9_-]*="
    r"|[?&][A-Za-z][A-Za-z0-9_-]*="
    r"|[A-Za-z_][A-Za-z0-9_]*="
    r")"
    + LEGACY_DISPLAY_ROUTE_TOKEN_PATTERN
)
HYDROMET_PAGE_IDENTIFIER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])HydroMetPage(?![A-Za-z0-9_])")
ROUTE_REDIRECT_TARGET_PATTERN = re.compile(r"(?:^|[\s`'\"([{<|])/(?:$|[\s`'\"()\]}>.,;:?|]|redirect|alias)")
ROUTE_REDIRECT_WORD_PATTERN = re.compile(r"\b(?:redirects?|redirected)\b|legacyredirect|重定向", re.IGNORECASE)
ROUTE_ACTIVE_CLAUSE_CONNECTOR_PATTERN = re.compile(
    r"\b(?:and|then)\s+(?:open|visit|browse|navigate|use)\b|且打开",
    re.IGNORECASE,
)
BROAD_E2E_API_MOCK_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_$])page\.route\s*\(\s*(?P<glob>(?P<quote>['\"])\*\*/api/v1/\*\*(?P=quote))",
    re.MULTILINE,
)
MARKDOWN_TABLE_SEPARATOR_PATTERN = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
MARKDOWN_LIST_ITEM_PATTERN = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+")
MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+")
MARKDOWN_BLOCKQUOTE_PREFIX_PATTERN = re.compile(r"^\s{0,3}>\s?")
TOPOLOGY_DISPLAY_ENV_PATH_PATTERN = re.compile(
    r"(?:\./|\$\{?[a-z_][a-z0-9_]*\}?/)*infra/env/display\.env"
)
TOPOLOGY_DISPLAY_ENV_SOURCE_TO_WRITER_MAX_LINES = 120

FindingAxis = Literal["structure", "semantics", "behavior", "context", "protocol", "control"]
AuditMode = Literal["report", "hard-gate"]
AllowlistState = Literal["allowlisted", "unallowlisted"]


@dataclass(frozen=True)
class FindingSpec:
    check_id: str
    title: str
    axis: FindingAxis
    governance_face: str
    role: str
    evidence_path: str
    severity: Literal["low", "medium", "high"]
    priority: Literal["P0", "P1", "P2", "P3"]
    owner_area: str
    description: str
    recommendation: str
    module: str
    allowlist_reason: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class _StaleRouteClauseAnalysis:
    route_valued_match_starts: tuple[int, ...]
    route_valued_token_spans: frozenset[tuple[int, int]]
    redirect_word_spans: tuple[tuple[int, int], ...]
    active_instruction_context: bool
    active_route_valued_context: bool


@dataclass(frozen=True)
class _StaleRouteMentionFacts:
    clause_index: int
    left: int
    right: int
    arrow_shape: str
    route_valued: bool
    redirect_local: bool
    semantic_key: str


@dataclass(frozen=True)
class _StaleRouteLineContext:
    line: str
    clause_ranges: tuple[tuple[int, int], ...]
    clause_starts: tuple[int, ...]
    clause_texts: tuple[str, ...]
    clause_has_per_mention_redirect_syntax: tuple[bool, ...]
    clause_analyses: tuple[_StaleRouteClauseAnalysis, ...]
    mention_facts: dict[tuple[int, int], _StaleRouteMentionFacts]
    redirect_governing_texts: tuple[str, ...]
    mention_governing_texts: tuple[str, ...]
    governing_text: str
    has_historical_route_authority_banner: bool
    document_has_historical_route_authority_banner: bool


@dataclass(frozen=True)
class _StaleRouteMentionContext:
    clause: str
    explicit_redirect_text: str
    redirect_text: str
    governing_text: str
    has_historical_route_authority_banner: bool
    document_has_historical_route_authority_banner: bool


@dataclass(frozen=True)
class _StaleRouteStructuralContext:
    governing_text: str
    redirect_governing_text: str


@dataclass(frozen=True)
class _TopologyDisplayEnvSource:
    line_no: int
    line: str


@dataclass(frozen=True)
class _TopologyDisplayEnvWriterUse:
    line_no: int
    line: str


@dataclass(frozen=True)
class _TopologyDisplayEnvFacts:
    sources: tuple[_TopologyDisplayEnvSource, ...]
    writer_uses: tuple[_TopologyDisplayEnvWriterUse, ...]


@dataclass(frozen=True)
class _StaleRouteLineMatch:
    line_no: int
    line: str
    token: str
    token_start: int
    token_end: int
    context: _StaleRouteMentionContext


_StaleRouteDuplicateKey = tuple[str, object, str, str, bool, bool]


class _StaleRouteContextFactory:
    def __init__(self, relative_path: str, lines: list[str]) -> None:
        self._relative_path = relative_path
        self._lines = lines
        self._historical_banner_lines: frozenset[int] | None = None
        self._section_headings: tuple[str | None, ...] | None = None
        self._line_contexts: dict[int, _StaleRouteLineContext] = {}
        self._structural_contexts: dict[int, _StaleRouteStructuralContext] = {}
        self._list_item_bounds: dict[int, tuple[int, int]] = {}
        self._line_list_starts: dict[int, int | None] = {}
        self._list_item_contexts: dict[int, tuple[_StaleRouteStructuralContext, ...]] = {}
        self._table_ranges: dict[int, tuple[int, int]] = {}
        self._paragraph_ranges: dict[int, tuple[int, int, int]] = {}
        self._paragraph_contexts: dict[tuple[int, int, int], _StaleRouteStructuralContext] = {}

    def line_context(self, line_index: int) -> _StaleRouteLineContext:
        line_context = self._line_contexts.get(line_index)
        if line_context is None:
            historical_banner_lines = self._get_historical_banner_lines()
            line_context = _stale_route_line_context(
                self._relative_path,
                self._lines,
                line_index,
                self._get_section_headings(),
                historical_banner_lines,
                bool(historical_banner_lines),
                structural_context=self._structural_context(line_index),
            )
            self._line_contexts[line_index] = line_context
        return line_context

    def _structural_context(self, line_index: int) -> _StaleRouteStructuralContext:
        context = self._structural_contexts.get(line_index)
        if context is None:
            context = self._build_structural_context(line_index)
            self._structural_contexts[line_index] = context
        return context

    def _build_structural_context(self, line_index: int) -> _StaleRouteStructuralContext:
        if not _path_uses_markdown_route_context(self._relative_path):
            return _StaleRouteStructuralContext(governing_text="", redirect_governing_text="")
        lines = self._lines
        section_headings = self._get_section_headings()
        line = lines[line_index]
        if _line_is_markdown_table_row(line):
            start, end = self._table_range(line_index)
            governing_text = _stale_route_table_governing_text_for_range(
                lines,
                line_index,
                section_headings,
                start,
                end,
            )
            context = " ".join(_preceding_context_lines(lines, start, section_headings))
            return _StaleRouteStructuralContext(
                governing_text=governing_text,
                redirect_governing_text=context,
            )
        list_start = self._line_list_start(line_index)
        if list_start is not None:
            return self._list_item_context(list_start, line_index)
        start, end, blockquote_depth = self._paragraph_range(line_index)
        cache_key = (start, end, blockquote_depth)
        cached = self._paragraph_contexts.get(cache_key)
        if cached is not None:
            return cached
        governing_text = _stale_route_paragraph_governing_text_for_range(
            lines,
            line_index,
            section_headings,
            start,
            end,
        )
        context = _StaleRouteStructuralContext(
            governing_text=governing_text,
            redirect_governing_text=governing_text,
        )
        self._paragraph_contexts[cache_key] = context
        return context

    def _table_range(self, line_index: int) -> tuple[int, int]:
        cached = self._table_ranges.get(line_index)
        if cached is not None:
            return cached
        blockquote_depth = _markdown_blockquote_depth(self._lines[line_index])
        start = line_index
        while (
            start > 0
            and _markdown_blockquote_depth(self._lines[start - 1]) == blockquote_depth
            and _line_is_markdown_table_row(self._lines[start - 1])
        ):
            start -= 1
        end = line_index + 1
        while (
            end < len(self._lines)
            and _markdown_blockquote_depth(self._lines[end]) == blockquote_depth
            and _line_is_markdown_table_row(self._lines[end])
        ):
            end += 1
        bounds = (start, end)
        for index in range(start, end):
            self._table_ranges[index] = bounds
        return bounds

    def _list_item_context(
        self,
        list_start: int,
        line_index: int,
    ) -> _StaleRouteStructuralContext:
        cached = self._list_item_contexts.get(list_start)
        if cached is None:
            cached = self._build_list_item_contexts(list_start)
            self._list_item_contexts[list_start] = cached
        return cached[line_index - list_start]

    def _build_list_item_contexts(self, list_start: int) -> tuple[_StaleRouteStructuralContext, ...]:
        lines = self._lines
        section_headings = self._get_section_headings()
        list_indent = _list_item_indent_width(lines[list_start])
        item_end = self._list_item_bounds_for_start(list_start, list_indent)[1]
        parent_indexes = _parent_list_item_indexes(lines, list_start, list_indent)
        heading = _section_heading_at(section_headings, list_start)
        base_parts = [heading] if heading else []
        base_parts.extend(lines[index].strip() for index in parent_indexes)
        item_lines = [line.strip() for line in lines[list_start:item_end]]
        governing_text = " ".join(part for part in (*base_parts, *item_lines) if part).strip()

        following_redirect_texts = [""] * (item_end - list_start)
        following_redirect_text = ""
        for index in range(item_end - 1, list_start - 1, -1):
            line = lines[index]
            offset = index - list_start
            following_redirect_texts[offset] = following_redirect_text
            if _line_starts_markdown_list_item(line):
                following_redirect_text = ""
                continue
            tokens = set(_normalized_reason_text(line).split())
            if _line_has_redirect_alias_context(line, tokens):
                stripped = line.strip()
                following_redirect_text = (
                    f"{stripped} {following_redirect_text}".strip()
                    if following_redirect_text
                    else stripped
                )

        contexts: list[_StaleRouteStructuralContext] = []
        for offset, index in enumerate(range(list_start, item_end)):
            redirect_parts = list(base_parts)
            if index != list_start:
                redirect_parts.append(lines[list_start].strip())
            if following_redirect_texts[offset]:
                redirect_parts.append(following_redirect_texts[offset])
            contexts.append(
                _StaleRouteStructuralContext(
                    governing_text=governing_text,
                    redirect_governing_text=" ".join(part for part in redirect_parts if part).strip(),
                )
            )
        return tuple(contexts)

    def _line_list_start(self, line_index: int) -> int | None:
        cached = self._line_list_starts.get(line_index)
        if cached is not None or line_index in self._line_list_starts:
            return cached
        if not _line_is_list_or_list_continuation(self._lines, line_index):
            self._line_list_starts[line_index] = None
            return None
        list_start = _list_item_start_index(self._lines, line_index)
        list_indent = _list_item_indent_width(self._lines[list_start])
        item_start, item_end = self._list_item_bounds_for_start(list_start, list_indent)
        for index in range(item_start, item_end):
            self._line_list_starts[index] = list_start
        return list_start

    def _list_item_bounds_for_start(self, list_start: int, list_indent: int) -> tuple[int, int]:
        cached = self._list_item_bounds.get(list_start)
        if cached is not None:
            return cached
        bounds = (list_start, _list_item_end_index(self._lines, list_start, list_indent))
        self._list_item_bounds[list_start] = bounds
        return bounds

    def _paragraph_range(self, line_index: int) -> tuple[int, int, int]:
        cached = self._paragraph_ranges.get(line_index)
        if cached is not None:
            return cached
        blockquote_depth = _markdown_blockquote_depth(self._lines[line_index])
        start = line_index
        while (
            start > 0
            and _markdown_blockquote_depth(self._lines[start - 1]) == blockquote_depth
            and _line_continues_route_paragraph(self._lines[start - 1])
        ):
            if not _line_wraps_to_next(self._lines[start - 1]) and not _line_continues_previous(
                self._lines[start]
            ):
                break
            start -= 1
        end = line_index + 1
        while (
            end < len(self._lines)
            and _markdown_blockquote_depth(self._lines[end]) == blockquote_depth
            and _line_continues_route_paragraph(self._lines[end])
        ):
            if not _line_wraps_to_next(self._lines[end - 1]) and not _line_continues_previous(self._lines[end]):
                break
            end += 1
        bounds = (start, end, blockquote_depth)
        for index in range(start, end):
            self._paragraph_ranges[index] = bounds
        return bounds

    def _get_historical_banner_lines(self) -> frozenset[int]:
        if self._historical_banner_lines is None:
            self._historical_banner_lines = (
                _historical_route_authority_banner_line_numbers(self._lines)
                if self._relative_path.startswith("docs/runbooks/")
                else frozenset()
            )
        return self._historical_banner_lines

    def _get_section_headings(self) -> tuple[str | None, ...]:
        if self._section_headings is None:
            self._section_headings = (
                _markdown_section_headings(self._lines)
                if _path_uses_markdown_route_context(self._relative_path)
                else ()
            )
        return self._section_headings


def repo_root_from(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / ".git").exists():
            return candidate
    return start


def build_report(repo_root: Path | None = None, *, mode: AuditMode = "report") -> dict[str, object]:
    root = repo_root_from(repo_root)
    findings = sorted(
        _dedupe_findings(_collect_findings(root)),
        key=lambda item: (
            -PRIORITY_RANK[item.priority],
            -SEVERITY_RANK[item.severity],
            item.check_id,
            item.evidence_path,
            item.line or 0,
        ),
    )
    finding_records = [_finding_record(index, finding) for index, finding in enumerate(findings, start=1)]
    return {
        "metadata": _metadata(root, finding_records, mode=mode),
        "module_heatmap": _module_heatmap(finding_records),
        "findings": finding_records,
        "high_spread_patterns": _high_spread_patterns(finding_records),
    }


def render_markdown(report: dict[str, object]) -> str:
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    heatmap = report["module_heatmap"]
    findings = report["findings"]
    patterns = report["high_spread_patterns"]
    assert isinstance(heatmap, list)
    assert isinstance(findings, list)
    assert isinstance(patterns, list)

    lines = [
        "# Repository Entropy Audit",
        "",
        f"- Mode: `{metadata['mode']}`",
    ]
    if metadata["mode"] == "hard-gate":
        lines.extend(
            [
                f"- Hard gate status: `{metadata['hard_gate_status']}`",
                f"- Hard gate failing findings: `{metadata['hard_gate_failing_count']}`",
                "- Hard gate gated check IDs: `"
                + "`, `".join(str(check_id) for check_id in metadata["hard_gate_gated_check_ids"])
                + "`",
            ]
        )
    lines.extend(
        [
            f"- Generated: `{metadata['generated_at']}`",
            f"- Baseline path: `{metadata['baseline_path']}`",
            f"- Baseline written: `{str(metadata['baseline_written']).lower()}`",
            f"- Findings: `{metadata['finding_count']}`",
            f"- Budget-counted findings: `{metadata['budget_counted_count']}`",
            f"- Gate-eligible findings: `{metadata['gate_eligible_count']}`",
            "",
            "## Entropy Heatmap",
            "",
            "| Module | Structure | Semantics | Behavior | Context | Protocol | Control | Priority | Findings |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|",
        ]
    )
    for row in heatmap:
        assert isinstance(row, dict)
        lines.append(
            "| {module} | {structure} | {semantics} | {behavior} | {context} | {protocol} | "
            "{control} | {priority} | {finding_count} |".format(**row)
        )

    lines.extend(["", "## High-Spread Patterns", ""])
    if patterns:
        for pattern in patterns:
            assert isinstance(pattern, dict)
            lines.append(
                "- **{pattern}**: {occurrence_count} findings across {module_count} modules; "
                "top priority `{top_priority}`; roles `{roles}`.".format(
                    pattern=pattern["pattern"],
                    occurrence_count=pattern["occurrence_count"],
                    module_count=pattern["module_count"],
                    top_priority=pattern["top_priority"],
                    roles=", ".join(pattern["roles"]) if pattern["roles"] else "none",
                )
            )
    else:
        lines.append("- No replicated patterns detected.")

    lines.extend(["", "## Prioritized Cleanup Targets", ""])
    if findings:
        for finding in findings[:20]:
            assert isinstance(finding, dict)
            location = finding["evidence_path"]
            if finding.get("line"):
                location = f"{location}:{finding['line']}"
            state = str(finding["allowlist_state"])
            gate = "gate-eligible" if finding["gate_eligible"] else "not gate-eligible"
            lines.append(
                "- `{priority}` `{severity}` **{title}** ({axis}, {role}) at `{location}`: "
                "{recommendation} [{state}; {gate}]".format(
                    priority=finding["priority"],
                    severity=finding["severity"],
                    title=finding["title"],
                    axis=finding["axis"],
                    role=finding["role"],
                    location=location,
                    recommendation=finding["recommendation"],
                    state=state,
                    gate=gate,
                )
            )
    else:
        lines.append("- No cleanup targets detected.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a repository entropy audit.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--mode",
        choices=("report", "hard-gate"),
        default="report",
        help="Run in report-only mode by default, or explicitly evaluate prepared hard-gate findings.",
    )
    args = parser.parse_args(argv)

    report = build_report(mode=args.mode)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return _exit_code_for_report(report)


def _metadata(root: Path, findings: list[dict[str, object]], *, mode: AuditMode) -> dict[str, object]:
    baseline_path = ".entropy-baseline/latest.json"
    summary_counts = _summary_counts(findings)
    metadata: dict[str, object] = {
        "schema_version": "governance-4a.entropy-report.v1",
        "mode": "hard-gate" if mode == "hard-gate" else "report-only",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo_root": root.as_posix(),
        "baseline_path": baseline_path,
        "baseline_exists": (root / baseline_path).exists(),
        "baseline_written": False,
        "finding_count": len(findings),
        "check_family_count": len({str(finding["check_id"]) for finding in findings}),
        "budget_counted_count": int(summary_counts["by_budget_count"]["budget_counted"]),
        "gate_eligible_count": int(summary_counts["by_gate_eligibility"]["gate_eligible"]),
        "summary_counts": summary_counts,
        "max_scanned_text_file_bytes": MAX_SCANNED_TEXT_FILE_BYTES,
        "max_artifact_fingerprint_bytes": MAX_ARTIFACT_FINGERPRINT_BYTES,
        "executed_check_families": list(CHECK_FAMILIES),
        "skipped_path_families": sorted(
            [
                ".git",
                ".venv",
                "node_modules",
                "dist",
                "artifacts",
                "data",
                ".nhms-*",
                "caches",
            ]
        ),
    }
    if mode == "hard-gate":
        failing_count = _hard_gate_failing_count(findings)
        metadata.update(
            {
                "hard_gate_status": "fail" if failing_count else "pass",
                "hard_gate_gated_check_ids": list(HARD_GATE_CHECK_IDS),
                "hard_gate_failing_count": failing_count,
            }
        )
    return metadata


def _hard_gate_failing_count(findings: Iterable[dict[str, object]]) -> int:
    return sum(1 for finding in findings if bool(finding["gate_eligible"]))


def _exit_code_for_report(report: dict[str, object]) -> int:
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    if metadata.get("mode") != "hard-gate":
        return 0
    return 1 if int(metadata["hard_gate_failing_count"]) > 0 else 0


def _collect_findings(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    findings.extend(_check_display_env_boundaries(root))
    findings.extend(_check_production_topology_drift(root))
    findings.extend(_check_qhh_diagnostic_tokens(root))
    findings.extend(_check_paused_workflows(root))
    findings.extend(_check_broad_e2e_mocks(root))
    findings.extend(_check_stale_route_tokens(root))
    findings.extend(_check_placeholder_paths(root))
    findings.extend(_check_makefile_toolchain(root))
    findings.extend(_check_openapi_frontend_type_drift(root))
    findings.extend(_check_slurm_gateway_route_leakage(root))
    findings.extend(_check_agent_artifact_ownership(root))
    findings.extend(_check_apps_api_layer_inversion(root))
    return findings


def _check_display_env_boundaries(root: Path) -> list[FindingSpec]:
    compute_only_tokens = (
        "WORKSPACE_ROOT",
        "SHUD_EXECUTABLE",
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_PARTITION",
        "SLURM_ACCOUNT",
    )
    findings: list[FindingSpec] = []
    for path in _infra_role_env_scan_files(root):
        text = _read_repo_text(root, path)
        lines = text.splitlines()
        for line_no, line in _matching_lines(text, compute_only_tokens):
            if line.lstrip().startswith("#"):
                continue
            if not _is_display_boundary_context(root, path, lines, line_no):
                continue
            token = next(token for token in compute_only_tokens if token in line)
            findings.append(
                FindingSpec(
                    check_id="role-env-boundary",
                    title="Display configuration references compute-only environment",
                    axis="protocol",
                    governance_face="role boundary",
                    role="display_readonly",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="high",
                    priority="P1",
                    owner_area="infra/runtime",
                    module=_module_for_path(root, path),
                    description=(
                        f"Display-facing env or compose file references `{token}`, which is part of the "
                        "compute/control-plane boundary inventory."
                    ),
                    recommendation=(
                        "Keep display config limited to read-only runtime identity and public display inputs; "
                        "move compute-only values to compute env/compose files."
                    ),
                )
            )
    return findings


def _infra_role_env_scan_files(root: Path) -> Iterable[Path]:
    roots = [root / "infra", root / "apps" / "frontend"]
    for path in _iter_text_files(root, roots):
        if (
            path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix in {".env", ".example", ".yaml", ".yml"}
        ):
            yield path


def _is_display_boundary_context(root: Path, path: Path, lines: list[str], line_no: int) -> bool:
    rel = _rel(root, path).lower()
    env_like = path.name == ".env" or path.name.startswith(".env.") or path.suffix in {".env", ".example"}
    if env_like:
        if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
            return True
        return _line_has_display_env_section(lines, line_no)
    if path.suffix in {".yaml", ".yml"}:
        if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
            return True
        return any(start <= line_no <= end for start, end in _display_yaml_line_ranges(lines))
    if _path_has_display_hint(rel) and not _path_has_compute_hint(rel):
        return True
    return False


def _path_has_display_hint(relative: str) -> bool:
    parts = re.split(r"[/_.-]+", relative)
    return any(part in {"display", "frontend", "webui"} for part in parts)


def _path_has_compute_hint(relative: str) -> bool:
    parts = re.split(r"[/_.-]+", relative)
    return any(part in {"api", "backend", "compute", "gateway", "slurm", "worker"} for part in parts)


def _display_yaml_line_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    services_indent: int | None = None
    service_indent: int | None = None
    current_name: str | None = None
    current_start: int | None = None
    current_lines: list[str] = []

    def close_current(end_line: int) -> None:
        nonlocal current_name, current_start, current_lines
        if current_name is not None and current_start is not None:
            if _yaml_service_looks_display_facing(current_name, current_lines):
                ranges.append((current_start, end_line))
        current_name = None
        current_start = None
        current_lines = []

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            if current_name is not None:
                current_lines.append(line)
            continue
        if re.match(r"services\s*:\s*(?:#.*)?$", stripped):
            close_current(index - 1)
            services_indent = indent
            service_indent = None
            continue
        if services_indent is None:
            continue
        if indent <= services_indent:
            close_current(index - 1)
            services_indent = None
            service_indent = None
            continue

        key_match = re.match(r"['\"]?([A-Za-z0-9_.-]+)['\"]?\s*:\s*(?:#.*)?$", stripped)
        if key_match:
            if service_indent is None:
                service_indent = indent
            if indent == service_indent:
                close_current(index - 1)
                current_name = key_match.group(1)
                current_start = index
                current_lines = [line]
                continue
        if current_name is not None:
            current_lines.append(line)

    close_current(len(lines))
    return ranges


def _yaml_service_looks_display_facing(service_name: str, service_lines: list[str]) -> bool:
    name = service_name.lower()
    service_text = "\n".join(service_lines).lower()
    if any(hint in name for hint in ("display", "frontend", "webui")):
        return True
    if any(hint in name for hint in ("compute", "slurm", "gateway", "api", "worker", "db", "postgres", "redis")):
        return False
    return any(
        hint in service_text
        for hint in (
            "apps/frontend",
            "frontend",
            "display",
            "nginx",
            "caddy",
            "vite",
            "web-ui",
            "webui",
        )
    )


def _line_has_display_env_section(lines: list[str], line_no: int) -> bool:
    for index in range(line_no - 2, max(-1, line_no - 12), -1):
        stripped = lines[index].strip().lower()
        if not stripped:
            break
        if "compute" in stripped or "slurm" in stripped or "backend" in stripped:
            return False
        if "display" in stripped or "frontend" in stripped or "web-ui" in stripped or "webui" in stripped:
            return True
    return False


def _check_production_topology_drift(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for path in _production_topology_scan_files(root):
        rel = _rel(root, path)
        if _topology_path_is_archive_or_generated(rel):
            continue
        text = _read_repo_text(root, path)
        if not text:
            continue
        lines = text.splitlines()
        if _topology_document_is_non_current(rel, lines):
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
            if _topology_line_has_node22_local_postgres_or_mirror_drift(line):
                contract_context = _topology_contract_context(lines, index)
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
                        title="Node-22 local PostgreSQL or transitional mirror lacks non-current boundary",
                        description=(
                            "An active topology surface mentions node-22 local PostgreSQL, port :55433, or a "
                            "transitional node-22 mirror without the required non-current compatibility wording."
                        ),
                        recommendation=(
                            "Mark node-22 local PostgreSQL as historical and do-not-connect, and keep any "
                            "transitional mirror explicit-DSN, compatibility-only, and sunset-bound."
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
                        "transitional mirror path."
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
        root / "docs" / "governance",
        root / "docs" / "runbooks",
        root / "openspec" / "changes",
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


def _topology_document_is_non_current(relative_path: str, lines: list[str]) -> bool:
    if relative_path in {
        "AGENTS.md",
        "CLAUDE.md",
        "docs/governance/ROLE_BOUNDARY.md",
        "docs/runbooks/current-production-ops.md",
        "openspec/project-profile.md",
    }:
        return False
    top_context = _topology_normalized("\n".join(lines[:80]))
    if "current production operations" in top_context or "当前生产值守" in top_context:
        return False
    if ("design intent" in top_context or "设计意图" in top_context) and (
        "not current" in top_context
        or "不反映当前" in top_context
        or "当前部署事实" in top_context
    ):
        return True
    if ("historical" in top_context or "历史" in top_context or "superseded" in top_context) and (
        "not current" in top_context
        or "non current" in top_context
        or "已迁移" in top_context
        or "保留" in top_context
        or "not current topology" in top_context
    ):
        return True
    if "首跑" in top_context and "已迁移" in top_context and "保留" in top_context:
        return True
    return False


def _topology_line_context(lines: list[str], line_no: int, *, before: int = 7, after: int = 7) -> str:
    start = max(0, line_no - before - 1)
    end = min(len(lines), line_no + after)
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_forward_claim_context(lines: list[str], line_no: int, *, after: int) -> str:
    start = line_no - 1
    end = min(len(lines), line_no + after)
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_node22_writer_claim_context(lines: list[str], line_no: int) -> str:
    line = lines[line_no - 1]
    line_ends_claim = line.rstrip().endswith((".", "。", "!", "！", "?", "？"))
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


def _topology_normalized(text: str) -> str:
    normalized = text.lower().replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def _topology_line_may_start_node22_db_writer_claim(line: str) -> bool:
    return any(
        _topology_mentions_node22(clause)
        and (
            _topology_mentions_writer(clause)
            or "active primary" in clause
            or "current primary" in clause
            or re.search(r"\b(?:is|as|host|hosts|hosted|own|owns|owned)\s+(?:the\s+)?active\b", clause)
            or "主库" in clause
        )
        for clause in _topology_relation_clauses(line)
    )


def _topology_line_may_have_node22_db_writer_drift(line: str) -> bool:
    return any(
        _topology_mentions_node22(clause)
        and _topology_mentions_database(clause)
        and (
            _topology_mentions_writer(clause)
            or _topology_mentions_active_primary_database_authority(clause)
        )
        for clause in _topology_relation_clauses(line)
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


def _topology_node22_db_writer_drift_clauses(text: str) -> tuple[str, ...]:
    return tuple(
        clause
        for clause in _topology_relation_clauses(text)
        if _topology_mentions_node22(clause)
        and _topology_mentions_database(clause)
        and (
            _topology_mentions_writer(clause)
            or _topology_mentions_active_primary_database_authority(clause)
        )
    )


def _topology_node22_db_writer_clause_is_allowed(clause: str) -> bool:
    normalized = _topology_normalized(clause)
    return (
        _topology_context_is_non_current(normalized)
        or _topology_context_has_negative_node22_writer_boundary(normalized)
    )


def _topology_relation_clauses(text: str) -> tuple[str, ...]:
    normalized = _topology_normalized(text)
    return tuple(
        clause.strip()
        for clause in re.split(
            r"\s*(?:[,;，；。]|\.(?:\s+|$)|\s+-\s+|\s+while\s+|\s+whereas\s+|\s+but\s+|\s+and\s+display\s+|\s+and\s+node-27\s+)\s*",
            normalized,
        )
        if clause.strip()
    )


def _topology_mentions_node22(text: str) -> bool:
    lowered = _topology_normalized(text)
    return bool(
        re.search(
            r"\bnode[-_ ]?22\b|(?<!\d)22(?!\d)\s*"
            r"(?:is|as|own|owns|owned|host|hosts|node|节点|写入|写|拥有|postgres|postgresql|pg|db|数据库)",
            lowered,
        )
    )


def _topology_mentions_database(text: str) -> bool:
    lowered = _topology_normalized(text)
    return any(
        token in lowered
        for token in (
            "database",
            "postgres",
            "postgresql",
            "db ",
            " db",
            "数据库",
            "55433",
        )
    )


def _topology_mentions_writer(text: str) -> bool:
    lowered = _topology_normalized(text)
    return any(
        token in lowered
        for token in (
            "active primary",
            "host active",
            "hosts active",
            "hosted active",
            "host primary",
            "hosts primary",
            "writer",
            "writes",
            "writing",
            "writable",
            "hosted writer",
            "mutation",
            "mutate",
            "owns database",
            "owns db",
            "db mutation",
            "写入",
            "写 db",
            "db 写",
            "读写",
            "拥有",
        )
    )


def _topology_mentions_active_primary_database_authority(text: str) -> bool:
    lowered = _topology_normalized(text)
    return _topology_mentions_database(lowered) and (
        "active primary" in lowered
        or "current primary" in lowered
        or "primary postgresql" in lowered
        or "primary postgres" in lowered
        or "主库" in lowered
    )


def _topology_context_has_negative_node22_writer_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negative_node22_db_access(normalized):
        return True
    return any(
        token in normalized
        for token in (
            "does not connect",
            "does not use",
            "do not connect",
            "do not use",
            "not as current",
            "not current",
            "not through",
            "must not be treated",
            "must not read",
            "shall not instruct",
            "shall not present",
            "not instruct",
            "not present",
            "no active",
            "no implicit",
            "not expose",
            "without relying",
            "without invoking",
            "out of current",
            "outside current",
            "不连",
            "不作为",
            "不要连",
            "不应连接",
            "不使用",
            "禁止",
        )
    )


def _topology_context_has_negative_node22_db_access(context: str) -> bool:
    node22_db = r"node[-_ ]?22\s+(?:active\s+primary\s+)?(?:db|database|postgres|postgresql)"
    access_verb = (
        r"(?:(?:querying|reading|using|accessing)\s+(?:an?\s+)?(?:active\s+)?"
        r"|connecting\s+to\s+(?:an?\s+)?(?:active\s+)?"
        r"|relying\s+on\s+(?:an?\s+)?(?:active\s+)?"
        r")?"
    )
    return bool(
        re.search(
            rf"\b(?:without|no)\s+{access_verb}(?:an?\s+)?(?:active\s+)?{node22_db}"
            r"(?:\s+(?:access|query|queries|connection|read|reads|writer))?\b",
            context,
        )
    )


def _topology_line_has_node22_local_postgres_or_mirror_drift(line: str) -> bool:
    lowered = _topology_normalized(line)
    if ":55433" in lowered or " 55433" in lowered:
        return True
    if _topology_mentions_node22(lowered) and any(
        token in lowered
        for token in (
            "local postgres",
            "local postgresql",
            "local pg",
            "本地 pg",
            "本机 pg",
            "本地 postgresql",
            "本机 postgresql",
        )
    ):
        return True
    if ("n22_dsn" in lowered or "node22-url" in lowered) and _topology_line_mentions_mirror(lowered):
        return True
    return (
        _topology_mentions_node22(lowered)
        and _topology_line_mentions_mirror(lowered)
        and ("transitional" in lowered or "fallback" in lowered or "compatibility" in lowered)
    )


def _topology_line_mentions_mirror(text: str) -> bool:
    lowered = _topology_normalized(text)
    return "mirror" in lowered or "镜像" in lowered


def _topology_local_postgres_context_is_allowed(
    line: str,
    context: str,
    *,
    claim_context: str | None = None,
) -> bool:
    local_claim_context = _topology_normalized(claim_context or line)
    line_context = _topology_normalized(f"{line}\n{context}")
    if _topology_context_is_non_current(local_claim_context):
        return True
    if _topology_context_has_negative_node22_writer_boundary(local_claim_context):
        return True
    if _topology_context_is_guardrail_or_test_meta(local_claim_context):
        return True
    if _topology_context_is_guardrail_or_test_meta(line_context):
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
            "retired",
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
            "not current topology",
            "out of current",
            "outside current",
            "不要连",
            "不应连接",
            "不作为当前",
            "不用于当前",
        )
    )
    has_sunset = any(
        token in context
        for token in (
            "pending removal",
            "pending_removal",
            "sunset",
            "removal",
            "remove",
            "delete",
            "待删",
            "待删除",
            "删除",
            "迁移",
        )
    )
    return (has_historical and (has_do_not_connect or has_sunset)) or (
        has_do_not_connect and has_sunset
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
            "--node22-url",
            "n22_dsn",
            "显式",
        )
    )
    has_sunset = any(
        token in combined
        for token in (
            "sunset",
            "removal",
            "remove this mirror",
            "remove the mirror",
            "pending removal",
            "after object-store",
            "declared handoff",
            "handoff manifest",
            "handoff packages",
            "pre-contract",
            "移除",
            "待删除",
        )
    )
    return has_mirror and has_compatibility and has_explicit_dsn and has_sunset


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
            "pending removal",
            "sunset",
            "removal",
            "explicit",
            "n22_dsn",
            "--node22-url",
            "兼容",
            "历史",
            "已弃用",
            "不要连",
            "待删除",
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
        )
        and ":55433" in normalized
        and (
            _topology_context_is_non_current(normalized)
            or "historical_node22_pg_status" in normalized
        )
    )


def _topology_context_is_guardrail_or_test_meta(context: str) -> bool:
    normalized = _topology_normalized(context)
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
            "focused tests cover",
            "regression rows",
            "drift in current operational surfaces",
            "while allowing clearly historical evidence",
            "positive fixtures",
            "negative fixtures",
        )
    )


def _topology_context_is_explicit_mirror_implementation(context: str) -> bool:
    normalized = _topology_normalized(context)
    has_explicit_mirror = (
        _topology_line_mentions_mirror(normalized)
        and any(token in normalized for token in ("explicit", "--node22-url", "n22_dsn"))
    )
    return has_explicit_mirror and any(
        token in normalized
        for token in (
            "node22mirrorsource",
            "_resolve_node22_source",
            "mirror.extend",
            "parser.add_argument",
            "node22mirrordsnmissing",
            "node22_dsn_missing_reason",
            "source=\"cli:--node22-url\"",
            "source=\"env:n22_dsn\"",
            "help=\"explicit node-22",
        )
    )


def _topology_display_env_facts(lines: list[str]) -> _TopologyDisplayEnvFacts:
    aliases: set[str] = set()
    sources: list[_TopologyDisplayEnvSource] = []
    writer_uses: list[_TopologyDisplayEnvWriterUse] = []
    for line_no, line in enumerate(lines, start=1):
        normalized = _topology_normalized(line)
        alias = _topology_line_display_env_alias_name(normalized)
        if alias is not None:
            aliases.add(alias)
        if _topology_line_sources_display_env(normalized, aliases):
            sources.append(_TopologyDisplayEnvSource(line_no=line_no, line=line))
        if _topology_line_has_unnegated_data_plane_writer_or_mirror_use(normalized):
            writer_uses.append(_TopologyDisplayEnvWriterUse(line_no=line_no, line=line))
    return _TopologyDisplayEnvFacts(sources=tuple(sources), writer_uses=tuple(writer_uses))


def _topology_display_env_associated_writer(
    facts: _TopologyDisplayEnvFacts,
    source_line: int,
    *,
    allow_file_level: bool,
) -> _TopologyDisplayEnvWriterUse | None:
    for writer_use in facts.writer_uses:
        if writer_use.line_no < source_line:
            continue
        if writer_use.line_no - source_line <= 6:
            return writer_use
    if not allow_file_level:
        return None
    for writer_use in facts.writer_uses:
        if writer_use.line_no < source_line:
            continue
        if writer_use.line_no - source_line <= TOPOLOGY_DISPLAY_ENV_SOURCE_TO_WRITER_MAX_LINES:
            return writer_use
    return None


def _topology_display_env_file_allows_file_level_association(relative_path: str) -> bool:
    path = Path(relative_path)
    return relative_path.startswith("scripts/") or path.suffix in {".sh", ".py"}


def _topology_display_env_association_context(
    lines: list[str],
    source_line: int,
    writer_line: int,
) -> str:
    start = max(0, min(source_line, writer_line) - 1)
    end = min(len(lines), max(source_line, writer_line))
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_line_may_source_display_env(line: str) -> bool:
    lowered = _topology_normalized(line)
    return _topology_line_sources_display_env(lowered, set())


def _topology_context_sources_display_env(context: str) -> bool:
    lowered = _topology_normalized(context)
    if not _topology_context_references_display_env_path(lowered):
        return False
    if _topology_context_has_direct_display_env_source(lowered):
        return True
    if _topology_context_has_indirect_display_env_source(lowered):
        return True
    return _topology_context_has_display_env_authority_prose(lowered)


def _topology_context_has_direct_display_env_source(context: str) -> bool:
    return bool(
        re.search(
            rf"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
            context,
        )
        or "--env-file" in context
        and _topology_context_references_display_env_path(context)
        or re.search(
            rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}",
            context,
        )
        or "加载 infra/env/display.env" in context
    )


def _topology_context_has_indirect_display_env_source(context: str) -> bool:
    assignment = re.search(
        rf"\b(?P<name>[a-z_][a-z0-9_]*)\s*=\s*['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
        context,
    )
    if assignment is None:
        return False
    variable = re.escape(assignment.group("name"))
    return bool(
        re.search(
            rf"(?:^|[;&|]\s*|\s+|\bthen\s+)(?:source|\.)\s+"
            rf"['\"]?\$({variable}|\{{{variable}\}})['\"]?",
            context,
        )
    )


def _topology_context_has_display_env_authority_prose(context: str) -> bool:
    if not _topology_context_references_display_env_path(context):
        return False
    if not any(token in context for token in ("database_url", "db url", "dsn", "writer", "ingest")):
        return False
    return any(
        token in context
        for token in (
            " from ",
            " in ",
            "来自 ",
            "authority",
            "权威",
        )
    )


def _topology_line_has_shell_source_command(line: str) -> bool:
    lowered = _topology_normalized(line)
    return bool(
        re.search(
            r"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+"
            rf"(?:['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?|['\"]?\$\{{?[a-z_][a-z0-9_]*\}}?['\"]?)",
            lowered,
        )
    )


def _topology_line_sources_display_env(line: str, aliases: set[str]) -> bool:
    normalized = _topology_normalized(line)
    if _topology_line_has_unnegated_direct_display_env_source(normalized):
        return True
    if _topology_line_sources_display_env_alias(normalized, aliases):
        return True
    return _topology_context_has_display_env_authority_prose(normalized)


def _topology_line_display_env_alias_name(line: str) -> str | None:
    match = re.search(
        rf"\b(?:export\s+)?(?P<name>[a-z_][a-z0-9_]*)\s*=\s*['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
        line,
    )
    return match.group("name") if match else None


def _topology_line_sources_display_env_alias(line: str, aliases: set[str]) -> bool:
    for alias in aliases:
        variable = re.escape(alias)
        if re.search(
            rf"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+['\"]?\$({variable}|\{{{variable}\}})['\"]?",
            line,
        ):
            return True
    return False


def _topology_line_has_unnegated_direct_display_env_source(line: str) -> bool:
    if not _topology_context_references_display_env_path(line):
        return False
    if "--env-file" in line:
        return True
    source_pattern = re.compile(
        rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?"
    )
    for match in source_pattern.finditer(line):
        prefix = line[max(0, match.start() - 28) : match.start()]
        if not _topology_prefix_has_display_env_source_negation(prefix):
            return True
    return "加载 infra/env/display.env" in line and "不要加载" not in line


def _topology_context_references_display_env_path(context: str) -> bool:
    return bool(TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.search(_topology_normalized(context)))


def _topology_prefix_has_display_env_source_negation(prefix: str) -> bool:
    return any(
        token in prefix
        for token in (
            "do not ",
            "never ",
            "not ",
            "no ",
            "不要",
            "不得",
        )
    )


def _topology_line_has_unnegated_data_plane_writer_or_mirror_use(line: str) -> bool:
    normalized = _topology_normalized(line)
    if _topology_context_has_negative_writer_or_mirror_terms(normalized):
        return False
    command_terms = (
        "autopipe",
        "autopipeline",
        "node27_autopipeline",
        "node27_autopipe",
        "node27_mirror_forcing",
        "n22_dsn",
    )
    if any(token in normalized for token in command_terms):
        return True
    weak_writer_terms = (
        "data-plane",
        "database_url",
        "db url",
        "dsn",
        "ingest",
        "mirror",
        "writer",
        "write",
        "writes",
        "writing",
        "写入",
    )
    return any(
        any(token in clause for token in weak_writer_terms)
        and (
            _topology_context_has_display_env_authority_prose(clause)
            or "authority" in clause
            or "权威" in clause
        )
        for clause in _topology_relation_clauses(normalized)
    )


def _topology_context_has_negative_writer_or_mirror_terms(context: str) -> bool:
    normalized = _topology_normalized(context)
    return any(
        token in normalized
        for token in (
            "no writer credentials",
            "without writer",
            "without a writer",
            "not writer",
            "not a writer",
            "no data-plane writer",
            "not for ingest",
            "do not source",
            "instead of deriving",
            "not derive",
            "without deriving",
            "must not fall back",
            "never reads",
            "never read",
            "不要 source",
            "不要加载",
            "不得 source",
        )
    )


def _topology_context_has_data_plane_writer_or_mirror_terms(context: str) -> bool:
    return _topology_line_has_unnegated_data_plane_writer_or_mirror_use(context)


def _topology_display_env_context_is_allowed(context: str) -> bool:
    if _topology_context_is_guardrail_or_test_meta(context):
        return True
    if _topology_context_has_display_env_prohibition(context) and not (
        _topology_context_has_unnegated_display_env_source(context)
    ):
        return True
    readonly_terms = (
        "display api",
        "display_readonly",
        "readonly",
        "read-only",
        "start-display-api",
        "compose.display",
        "display runtime",
        "只读",
        "展示",
    )
    return any(token in context for token in readonly_terms) and not (
        _topology_context_has_data_plane_writer_or_mirror_terms(context)
    )


def _topology_context_has_display_env_prohibition(context: str) -> bool:
    return any(
        token in context
        for token in (
            "do not source",
            "never reads",
            "never read",
            "not for ingest",
            "forbidden_sources",
            "不要 source",
            "不要加载",
            "不得 source",
        )
    )


def _topology_context_has_unnegated_display_env_source(context: str) -> bool:
    normalized = _topology_normalized(context)
    if "--env-file" in normalized and _topology_context_references_display_env_path(normalized):
        return True
    if _topology_context_has_indirect_display_env_source(normalized):
        return True
    source_pattern = re.compile(
        rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?"
    )
    for match in source_pattern.finditer(normalized):
        prefix = normalized[max(0, match.start() - 24) : match.start()]
        if not _topology_prefix_has_display_env_source_negation(prefix):
            return True
    return False


def _check_qhh_diagnostic_tokens(root: Path) -> list[FindingSpec]:
    tokens = (
        "DIAGNOSTIC-ONLY",
        "run_qhh_cycle",
        "run_qhh_continuous",
        "run_qhh_backend_smoke",
        "create_qhh_shud_manifest",
        "scripts/run_qhh_cycle.sh",
        "scripts/run_qhh_continuous.py",
        "scripts/run_qhh_backend_smoke.sh",
        "scripts/create_qhh_shud_manifest.py",
    )
    production_roots = [
        root / "services" / "orchestrator",
        root / "services" / "production_closure",
        root / "workers",
    ]
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, production_roots):
        text = _read_repo_text(root, path)
        for line_no, line in _matching_lines(text, tokens):
            token = next(token for token in tokens if token in line)
            findings.append(
                FindingSpec(
                    check_id="qhh-diagnostic-token",
                    title="Production path references QHH diagnostic token",
                    axis="behavior",
                    governance_face="legacy/dead-code",
                    role="compute_control",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="high" if _rel(root, path).startswith("services/orchestrator/") else "medium",
                    priority="P1",
                    owner_area="production scheduler",
                    module=_module_for_path(root, path),
                    description=f"Production-adjacent source references diagnostic token `{token}`.",
                    recommendation=(
                        "Keep QHH diagnostic runners in scripts/diagnostic evidence lanes; production scheduling "
                        "should use the generic orchestrator and standalone Slurm gateway path."
                    ),
                )
            )
    return findings


def _check_paused_workflows(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / ".github" / "workflows"]):
        text = _read_repo_text(root, path)
        for line_no, line in _matching_lines(text, ("&& false",)):
            findings.append(
                FindingSpec(
                    check_id="paused-workflow-condition",
                    title="Workflow condition is paused with hidden false branch",
                    axis="control",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path=_rel(root, path),
                    line=line_no,
                    severity="medium",
                    priority="P2",
                    owner_area="ci",
                    module=_module_for_path(root, path),
                    description="A workflow line contains `&& false`, which can hide disabled validation.",
                    recommendation=(
                        "Replace hidden false conditions with explicit workflow_dispatch, path filters, or a "
                        "documented non-blocking job state."
                    ),
                )
            )
    return findings


def _check_broad_e2e_mocks(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / "apps" / "frontend"]):
        rel = _rel(root, path)
        if _path_is_frontend_generated_artifact(rel):
            continue
        if not ("/e2e/" in rel or rel.endswith(".spec.ts") or _path_has_label(rel, {"live"})):
            continue
        text = _read_repo_text(root, path)
        for line_no in _broad_e2e_mock_line_numbers(text):
            classification = _classify_broad_e2e_mock_path(rel)
            findings.append(
                FindingSpec(
                    check_id="broad-e2e-api-mock",
                    title=classification["title"],
                    axis="behavior",
                    governance_face="docs alignment",
                    role="display_readonly",
                    evidence_path=rel,
                    line=line_no,
                    severity=classification["severity"],
                    priority=classification["priority"],
                    owner_area="frontend e2e",
                    module=_module_for_path(root, path),
                    allowlist_reason=classification["allowlist_reason"],
                    description="Broad `page.route('**/api/v1/**')` mocks can be mistaken for live display evidence.",
                    recommendation=(
                        "Keep broad API mocks in deterministic mocked regressions and label live evidence specs so "
                        "they use real API calls or narrowly scoped mocks."
                    ),
                )
            )
    return findings


def _broad_e2e_mock_line_numbers(text: str) -> list[int]:
    return [text.count("\n", 0, match.start("glob")) + 1 for match in BROAD_E2E_API_MOCK_PATTERN.finditer(text)]


def _path_is_frontend_generated_artifact(relative: str) -> bool:
    return relative.startswith("apps/frontend/artifacts/")


def _classify_broad_e2e_mock_path(
    relative: str,
) -> dict[str, Literal["medium", "high", "P1", "P2"] | str | None]:
    if _path_has_label(relative, {"live"}):
        return {
            "title": "Live-labeled frontend E2E path uses broad API mock",
            "severity": "high",
            "priority": "P1",
            "allowlist_reason": None,
        }
    if _path_has_label(relative, {"deterministic", "fixture", "fixtures", "mock", "mocked", "preview", "visual"}):
        return {
            "title": "Deterministic frontend E2E path uses broad API mock",
            "severity": "medium",
            "priority": "P2",
            "allowlist_reason": "deterministic mocked/preview/visual e2e broad mock",
        }
    return {
        "title": "Frontend E2E path uses broad API mock",
        "severity": "medium",
        "priority": "P2",
        "allowlist_reason": None,
    }


def _check_stale_route_tokens(root: Path) -> list[FindingSpec]:
    roots = [
        root / "apps",
        root / "docs",
        root / "openspec",
        root / "README.md",
        root / "progress.md",
        root / "CLAUDE.md",
    ]
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, roots):
        rel = _rel(root, path)
        if rel.startswith(("docs/archived/",)):
            continue
        text = _read_repo_text(root, path)
        lines = text.splitlines()
        for match in _stale_route_line_matches(
            rel,
            lines,
            include_legacy_tokens=_path_is_legacy_route_token_scope(rel),
            include_expanded_aliases=_path_is_route_authority_expanded_scope(rel),
        ):
            allowed_reason = _stale_route_allowlist_reason(rel, lines, match.line_no, match.token, match.context)
            findings.append(
                FindingSpec(
                    check_id="stale-display-route-token",
                    title="Stale display route or HydroMetPage token remains",
                    axis="context",
                    governance_face="docs alignment",
                    role="display_readonly",
                    evidence_path=rel,
                    line=match.line_no,
                    severity="low" if allowed_reason else "medium",
                    priority="P3" if allowed_reason else "P2",
                    owner_area="frontend/docs",
                    module=_module_for_path(root, path),
                    allowlist_reason=allowed_reason,
                    description=(
                        f"Reference to legacy display route token `{match.token}` remains after M26 "
                        "single-map routing consolidation."
                    ),
                    recommendation=(
                        "Confirm whether the reference is historical/redirect evidence or should point to the "
                        "current single-map `/` display entrypoint."
                    ),
                )
            )
    return findings


def _path_is_legacy_route_token_scope(relative_path: str) -> bool:
    return (
        relative_path.startswith(("apps/", "docs/", "openspec/"))
        or relative_path == "progress.md"
    )


def _path_is_route_authority_expanded_scope(relative_path: str) -> bool:
    return (
        relative_path.startswith("docs/runbooks/")
        or relative_path in {"README.md", "progress.md", "CLAUDE.md", "docs/governance/DOC_STATUS.md"}
    )


def _stale_route_line_matches(
    relative_path: str,
    lines: list[str],
    *,
    include_legacy_tokens: bool,
    include_expanded_aliases: bool,
) -> Iterable[_StaleRouteLineMatch]:
    context_factory = _StaleRouteContextFactory(relative_path, lines)
    for line_no, line in enumerate(lines, start=1):
        emitted_spans: set[tuple[str, int, int]] = set()
        emitted_contexts: set[_StaleRouteDuplicateKey] = set()
        if include_legacy_tokens:
            for match in HYDROMET_PAGE_IDENTIFIER_PATTERN.finditer(line):
                token = match.group(0)
                start = match.start()
                end = match.end()
                line_context = context_factory.line_context(line_no - 1)
                duplicate_key = _stale_route_duplicate_key(line_context, token, start)
                if duplicate_key in emitted_contexts:
                    emitted_spans.add((token, start, end))
                    continue
                emitted_spans.add((token, start, end))
                emitted_contexts.add(duplicate_key)
                yield _StaleRouteLineMatch(
                    line_no=line_no,
                    line=line,
                    token=token,
                    token_start=start,
                    token_end=end,
                    context=_stale_route_mention_context(line_context, start, end),
                )
            for match in LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN.finditer(line):
                token = match.group("token")
                start = match.start("token")
                end = match.end("token")
                if token != "/hydro-met" or (token, start, end) in emitted_spans:
                    continue
                line_context = context_factory.line_context(line_no - 1)
                duplicate_key = _stale_route_duplicate_key(line_context, token, start)
                if duplicate_key in emitted_contexts:
                    emitted_spans.add((token, start, end))
                    continue
                emitted_spans.add((token, start, end))
                emitted_contexts.add(duplicate_key)
                yield _StaleRouteLineMatch(
                    line_no=line_no,
                    line=line,
                    token=token,
                    token_start=start,
                    token_end=end,
                    context=_stale_route_mention_context(line_context, start, end),
                )
        if not include_expanded_aliases:
            continue
        for match in LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN.finditer(line):
            token = match.group("token")
            start = match.start("token")
            end = match.end("token")
            if (token, start, end) in emitted_spans:
                continue
            line_context = context_factory.line_context(line_no - 1)
            duplicate_key = _stale_route_duplicate_key(line_context, token, start)
            if duplicate_key in emitted_contexts:
                emitted_spans.add((token, start, end))
                continue
            emitted_spans.add((token, start, end))
            emitted_contexts.add(duplicate_key)
            yield _StaleRouteLineMatch(
                line_no=line_no,
                line=line,
                token=token,
                token_start=start,
                token_end=end,
                context=_stale_route_mention_context(line_context, start, end),
            )


def _check_placeholder_paths(root: Path) -> list[FindingSpec]:
    placeholder_patterns = RETIRED_ACTIVE_TREE_PREFIXES
    findings: list[FindingSpec] = []
    for path in _iter_text_files(root, [root / "docs", root / "openspec", root / "services", root / "infra"]):
        rel = _rel(root, path)
        text = _read_repo_text(root, path)
        for line_no, line in _matching_lines(text, placeholder_patterns):
            token = next(token for token in placeholder_patterns if token in line)
            allowlist = _placeholder_path_allowlist_reason(rel)
            findings.append(
                FindingSpec(
                    check_id="placeholder-path-token",
                    title="Placeholder or retired path token remains",
                    axis="semantics",
                    governance_face="legacy/dead-code",
                    role="shared_contract",
                    evidence_path=rel,
                    line=line_no,
                    severity="low" if allowlist else "medium",
                    priority="P3" if allowlist else "P2",
                    owner_area="docs/modules",
                    module=_module_for_path(root, path),
                    allowlist_reason=allowlist,
                    description=f"Reference to retired placeholder path `{token}` remains in active scan scope.",
                    recommendation=(
                        "Use canonical underscore package paths or mark the reference as historical inventory with "
                        "a narrow reason."
                    ),
                )
            )
    findings.extend(_check_tracked_retired_paths(root))
    return findings


def _check_tracked_retired_paths(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    for tracked_path in _git_tracked_paths(root, RETIRED_ACTIVE_TREE_PREFIXES):
        retired_prefix = _retired_active_tree_prefix_for(tracked_path)
        if retired_prefix is None:
            continue
        findings.append(
            FindingSpec(
                check_id="placeholder-path-exists",
                title="Tracked retired path returned to active tree",
                axis="structure",
                governance_face="legacy/dead-code",
                role="shared_contract",
                evidence_path=tracked_path,
                severity="medium",
                priority="P2",
                owner_area="repo structure",
                module=_module_for_relative(tracked_path),
                description=(
                    f"Tracked file `{tracked_path}` returned under retired active-tree prefix "
                    f"`{retired_prefix}`."
                ),
                recommendation=(
                    "Remove the tracked retired path or move the implementation to the canonical active "
                    "underscore/package path."
                ),
            )
        )
    return findings


def _retired_active_tree_prefix_for(relative_path: str) -> str | None:
    for prefix in RETIRED_ACTIVE_TREE_PREFIXES:
        if relative_path == prefix or relative_path.startswith(f"{prefix}/"):
            return prefix
    return None


def _check_makefile_toolchain(root: Path) -> list[FindingSpec]:
    makefile = root / "Makefile"
    if not makefile.exists():
        return []
    findings: list[FindingSpec] = []
    for line_no, line in enumerate(_read_repo_text(root, makefile).splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _makefile_line_has_unmanaged_python_tool(stripped):
            findings.append(
                FindingSpec(
                    check_id="makefile-toolchain-discipline",
                    title="Makefile command bypasses repository-managed Python environment",
                    axis="protocol",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path="Makefile",
                    line=line_no,
                    severity="medium",
                    priority="P2",
                    owner_area="developer tooling",
                    module="Makefile",
                    description="Makefile line invokes system Python tooling instead of `uv run`.",
                    recommendation="Route Python, pytest, and ruff commands through `uv run` from the repository root.",
                )
            )
    return findings


def _check_openapi_frontend_type_drift(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    openapi = root / "openapi" / "nhms.v1.yaml"
    frontend_types = root / "apps" / "frontend" / "src" / "api" / "types.ts"
    drift_test = root / "tests" / "test_openapi_drift.py"
    if not openapi.exists() or not frontend_types.exists():
        return [
            FindingSpec(
                check_id="openapi-frontend-types-presence",
                title="OpenAPI or generated frontend types are missing",
                axis="protocol",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path="openapi/nhms.v1.yaml",
                severity="high",
                priority="P1",
                owner_area="api contract",
                module="openapi",
                description="Could not find both static OpenAPI spec and generated frontend type file.",
                recommendation=(
                    "Restore the OpenAPI spec and generated frontend type artifact before enabling CI drift checks."
                ),
            )
        ]
    if drift_test.exists():
        findings.append(
            FindingSpec(
                check_id="openapi-frontend-types-delegated",
                title="OpenAPI/frontend type drift delegated to existing contract checks",
                axis="protocol",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path="tests/test_openapi_drift.py",
                severity="low",
                priority="P3",
                owner_area="api contract",
                module="openapi",
                allowlist_reason="existing OpenAPI drift tests are the enforced contract oracle",
                description=(
                    "Static OpenAPI and generated frontend types are present; this report records delegation to the "
                    "existing contract-drift test lane."
                ),
                recommendation="Keep running `tests/test_openapi_drift.py` and frontend API type generation checks.",
            )
        )
    fingerprint_reason, fingerprint_available = _artifact_fingerprint_pair_reason(root, openapi, frontend_types)
    findings.append(
        FindingSpec(
            check_id="openapi-frontend-types-signal",
            title="OpenAPI/frontend type artifacts have comparable fingerprints",
            axis="control",
            governance_face="entropy automation/control",
            role="shared_contract",
            evidence_path="apps/frontend/src/api/types.ts",
            severity="low",
            priority="P3",
            owner_area="api contract",
            module="openapi",
            allowlist_reason=fingerprint_reason,
            description=(
                "Report-only drift signal records both artifacts without asserting byte-level generation parity."
                if fingerprint_available
                else (
                    "Report-only drift signal records artifact presence but skipped unsafe or oversized "
                    "fingerprinting."
                )
            ),
            recommendation=(
                "Use the existing OpenAPI drift test and frontend `check:api-types` command as hard oracles."
            ),
        )
    )
    return findings


def _makefile_line_has_unmanaged_python_tool(line: str) -> bool:
    for segment in _shell_command_segments(line):
        command_index, command_name = _shell_command_name(segment)
        if command_name in {"python", "pytest", "ruff"}:
            return True
        if command_name == "pip" and command_index + 1 < len(segment) and segment[command_index + 1] == "install":
            return True
    return False


def _shell_command_segments(line: str) -> list[list[str]]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return [[line]]

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in ";&|" for char in token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _shell_command_name(tokens: list[str]) -> tuple[int, str]:
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip("@-+") if index == 0 else tokens[index]
        if not token or _is_shell_assignment(token):
            index += 1
            continue
        if token == "env":
            index += 1
            while index < len(tokens) and (tokens[index].startswith("-") or _is_shell_assignment(tokens[index])):
                index += 1
            continue
        return index, token
    return len(tokens), ""


def _is_shell_assignment(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _check_slurm_gateway_route_leakage(root: Path) -> list[FindingSpec]:
    path = root / "services" / "slurm_gateway" / "app.py"
    if not path.exists():
        return []
    findings: list[FindingSpec] = []
    text = _read_repo_text(root, path)
    for line_no, reason in _slurm_gateway_leakage_lines(text):
        findings.append(
            FindingSpec(
                check_id="slurm-gateway-route-leakage",
                title="Standalone Slurm gateway references business route surface",
                axis="structure",
                governance_face="role boundary",
                role="slurm_gateway",
                evidence_path=_rel(root, path),
                line=line_no,
                severity="high",
                priority="P1",
                owner_area="slurm gateway",
                module=_module_for_path(root, path),
                description=(
                    "Standalone Slurm gateway source contains a token associated with "
                    f"business/static routes: {reason}."
                ),
                recommendation=(
                    "Keep the standalone gateway limited to `/health` and `/api/v1/slurm/*`; expose business "
                    "routes only from `apps.api.main`."
                ),
            )
        )
    return findings


def _slurm_gateway_leakage_lines(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    findings: dict[int, str] = {}
    docstring_lines = _docstring_line_numbers(tree)
    for node in ast.walk(tree):
        line_no = getattr(node, "lineno", None)
        if not isinstance(line_no, int) or line_no in docstring_lines:
            continue
        if isinstance(node, ast.Call):
            reason = _slurm_gateway_forbidden_call_reason(node)
            if reason:
                findings.setdefault(line_no, reason)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            reason = _slurm_gateway_forbidden_path_reason(node.value)
            if reason:
                findings.setdefault(line_no, reason)
    return sorted(findings.items())


def _docstring_line_numbers(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        start = getattr(first, "lineno", None)
        end = getattr(first, "end_lineno", start)
        if isinstance(start, int) and isinstance(end, int):
            lines.update(range(start, end + 1))
    return lines


def _slurm_gateway_forbidden_call_reason(node: ast.Call) -> str | None:
    func_name = _call_name(node.func)
    if func_name.endswith(".include_router"):
        for arg in node.args:
            arg_name = _name_for_ast(arg)
            if _slurm_gateway_router_name_is_forbidden(arg_name):
                return f"business router registration `{arg_name}`"
    if func_name.endswith(".mount"):
        return "static/frontend mount call"
    if func_name == "StaticFiles" or func_name.endswith(".StaticFiles"):
        return "StaticFiles registration"
    if _call_is_route_decorator(node):
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                reason = _slurm_gateway_forbidden_path_reason(arg.value)
                if reason:
                    return f"direct route decorator for {reason}"
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _name_for_ast(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name_for_ast(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _slurm_gateway_router_name_is_forbidden(name: str) -> bool:
    normalized = name.lower()
    return any(token in normalized for token in ("forecast", "model", "pipeline", "static", "frontend"))


def _call_is_route_decorator(node: ast.Call) -> bool:
    func_name = _call_name(node.func)
    return any(
        func_name.endswith(f".{method}")
        for method in ("get", "post", "put", "delete", "patch", "options", "head", "api_route")
    )


def _slurm_gateway_forbidden_path_reason(value: str) -> str | None:
    if not value.startswith("/"):
        return None
    normalized = value.rstrip("/") or "/"
    segments = [segment for segment in re.split(r"[/?#]+", normalized.strip("/")) if segment]
    forbidden_segment_tokens = ("forecast", "model", "pipeline", "static", "assets", "frontend")
    for segment in segments:
        if segment == "slurm":
            continue
        if any(token in segment.lower() for token in forbidden_segment_tokens):
            return f"path literal `{value}`"
    forbidden_prefixes = (
        "/api/v1/forecast",
        "/api/v1/models",
        "/api/v1/model",
        "/api/v1/pipeline",
        "/forecast",
        "/models",
        "/model",
        "/pipeline",
        "/static",
        "/assets",
        "/frontend",
    )
    for prefix in forbidden_prefixes:
        if normalized == prefix or normalized.startswith(f"{prefix}/") or normalized.startswith(f"{prefix}{{"):
            return f"path literal `{value}`"
    return None


def _check_agent_artifact_ownership(root: Path) -> list[FindingSpec]:
    doc_status = root / "docs" / "governance" / "DOC_STATUS.md"
    findings: list[FindingSpec] = []
    required_terms = (
        ".agents/skills/**",
        ".codex/tmp/",
        ".codex/cache/",
        ".codex/evidence/",
        "apps/frontend/artifacts/**",
        "Root `artifacts/`",
        ".dockerignore",
    )
    text = _read_repo_text(root, doc_status) if doc_status.exists() else ""
    for term in required_terms:
        if term not in text:
            findings.append(
                FindingSpec(
                    check_id="agent-artifact-ownership-policy",
                    title="DOC_STATUS ownership policy misses governed artifact term",
                    axis="context",
                    governance_face="docs alignment",
                    role="shared_contract",
                    evidence_path=_rel(root, doc_status),
                    severity="medium",
                    priority="P2",
                    owner_area="governance docs",
                    module="docs/governance",
                    description=f"`DOC_STATUS.md` does not mention expected ownership term `{term}`.",
                    recommendation=(
                        "Update the ownership policy before relying on generated agent/artifact path handling."
                    ),
                )
            )
    ignore_text = _read_repo_text(root, root / ".gitignore")
    dockerignore_text = _read_repo_text(root, root / ".dockerignore")
    ignore_expectations = {
        ".codex/": ignore_text,
        "artifacts/": ignore_text,
        "apps/frontend/artifacts/": ignore_text,
        ".agents": dockerignore_text,
        ".codex": dockerignore_text,
        "apps/frontend/artifacts": dockerignore_text,
    }
    for token, haystack in ignore_expectations.items():
        if token not in haystack:
            findings.append(
                FindingSpec(
                    check_id="agent-artifact-ignore-policy",
                    title="Generated agent/artifact path is not covered by ignore policy",
                    axis="control",
                    governance_face="entropy automation/control",
                    role="shared_contract",
                    evidence_path=(
                        ".gitignore" if "artifacts" in token or token.startswith(".codex") else ".dockerignore"
                    ),
                    severity="medium",
                    priority="P2",
                    owner_area="repo hygiene",
                    module="repo policy",
                    description=f"Expected ignore token `{token}` was not found in the relevant ignore file.",
                    recommendation="Align ignore files with `docs/governance/DOC_STATUS.md` ownership policy.",
                )
            )
    tracked = _git_tracked_paths(root)
    unexpected = [
        path
        for path in tracked
        if path.startswith((".codex/tmp/", ".codex/cache/", ".codex/evidence/", "artifacts/"))
        or (
            path.startswith("apps/frontend/artifacts/")
            and not fnmatch.fnmatch(path, "apps/frontend/artifacts/m11-*.png")
        )
    ]
    for path in unexpected:
        findings.append(
            FindingSpec(
                check_id="tracked-generated-artifact",
                title="Generated artifact path appears tracked",
                axis="control",
                governance_face="entropy automation/control",
                role="shared_contract",
                evidence_path=path,
                severity="medium",
                priority="P2",
                owner_area="repo hygiene",
                module=_module_for_relative(path),
                description="A generated agent/artifact path conflicts with the documented ownership policy.",
                recommendation=(
                    "Remove the generated artifact from tracking or promote it with explicit issue-scoped review."
                ),
            )
        )
    return findings


def _check_apps_api_layer_inversion(root: Path) -> list[FindingSpec]:
    scan_roots = [root / "packages", root / "services", root / "workers"]
    scan_files = [
        root / "services" / "slurm_gateway" / "models.py",
        root / "services" / "production_closure" / "ops_validation.py",
    ]
    findings: list[FindingSpec] = []
    for path in sorted({*list(_iter_python_files(root, scan_roots)), *[file for file in scan_files if file.exists()]}):
        rel = _rel(root, path)
        if rel.startswith("apps/api/"):
            continue
        try:
            tree = ast.parse(_read_repo_text(root, path), filename=rel)
        except SyntaxError as exc:
            findings.append(
                FindingSpec(
                    check_id="apps-api-layer-parse-error",
                    title="Could not parse Python file for apps.api layer inversion scan",
                    axis="structure",
                    governance_face="role boundary",
                    role="shared_contract",
                    evidence_path=rel,
                    line=exc.lineno,
                    severity="low",
                    priority="P3",
                    owner_area="layering",
                    module=_module_for_path(root, path),
                    description="The AST import scan skipped a file because it could not be parsed.",
                    recommendation="Fix parse errors or exclude generated files from the source tree.",
                )
            )
            continue
        for module in sorted(_normalized_apps_api_import_modules(tree)):
            findings.append(
                FindingSpec(
                    check_id="apps-api-layer-inversion",
                    title="Non-API layer imports apps.api",
                    axis="structure",
                    governance_face="role boundary",
                    role="shared_contract",
                    evidence_path=rel,
                    severity="high",
                    priority="P1",
                    owner_area="layering",
                    module=_module_for_path(root, path),
                    description=f"Shared/service/worker source imports `{module}` from the API layer.",
                    recommendation=(
                        "Move shared contracts to packages/common or inject API-only behavior from apps/api."
                    ),
                )
            )
    return findings


def _dedupe_findings(findings: Iterable[FindingSpec]) -> list[FindingSpec]:
    seen: set[tuple[object, ...]] = set()
    result: list[FindingSpec] = []
    for finding in findings:
        key = (
            finding.check_id,
            finding.evidence_path,
            finding.line,
            finding.title,
            finding.allowlist_reason,
            finding.description,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def _finding_record(index: int, finding: FindingSpec) -> dict[str, object]:
    axis_scores = {axis: "low" for axis in AXES}
    axis_scores[finding.axis] = finding.severity
    allowlist_key = _allowlist_key(finding)
    allowlist_state = _allowlist_state(allowlist_key)
    budget_counted = allowlist_state == "unallowlisted"
    gate_eligible = budget_counted and finding.check_id in HARD_GATE_CHECK_IDS
    return {
        "id": f"ENT-{index:04d}",
        "check_id": finding.check_id,
        "title": finding.title,
        "axis": finding.axis,
        "axis_scores": axis_scores,
        "governance_face": finding.governance_face,
        "role": finding.role,
        "evidence_path": finding.evidence_path,
        "line": finding.line,
        "severity": finding.severity,
        "priority": finding.priority,
        "owner_area": finding.owner_area,
        "module": finding.module,
        "allowlist_reason": finding.allowlist_reason,
        "allowlist_key": allowlist_key,
        "allowlist_state": allowlist_state,
        "budget_counted": budget_counted,
        "gate_eligible": gate_eligible,
        "description": finding.description,
        "recommendation": finding.recommendation,
    }


def _allowlist_state(allowlist_key: str | None) -> AllowlistState:
    return "allowlisted" if allowlist_key else "unallowlisted"


def _allowlist_key(finding: FindingSpec) -> str | None:
    reason = finding.allowlist_reason
    if reason is None or not reason.strip():
        return None
    reason_key = _allowlist_reason_key(finding.check_id, reason)
    return f"{finding.check_id}:{reason_key}"


def _allowlist_reason_key(check_id: str, reason: str) -> str:
    normalized = _normalized_reason_text(reason)
    tokens = set(normalized.split())
    if check_id == "broad-e2e-api-mock" and tokens & {"deterministic", "mock", "mocked", "preview", "visual"}:
        return "deterministic-mocked-preview-visual"
    if check_id == "stale-display-route-token":
        if "milestone" in tokens or "progress" in tokens:
            return "historical-milestone-summary"
        if "provenance" in tokens or "extraction" in tokens:
            return "library-extraction-provenance"
        if "compatibility" in tokens or "compatible" in tokens:
            return "legacy-route-compatibility-context"
        if "historical" in tokens or "pre" in tokens or "plans" in tokens:
            return "historical-plan-or-pre-m26-evidence"
        if "m26" in tokens or "redirect" in tokens:
            return "m26-route-consolidation-or-redirect"
    if check_id == "placeholder-path-token" and {"governance", "inventory"} <= tokens:
        return "governance-retired-placeholder-inventory"
    if check_id == "placeholder-path-token" and {"governed", "archived", "evidence"} <= tokens:
        return "governed-archived-retired-placeholder-evidence"
    if check_id == "placeholder-path-token" and {"governed", "completed", "openspec", "evidence"} <= tokens:
        return "governed-completed-openspec-retired-placeholder-evidence"
    if check_id == "openapi-frontend-types-delegated":
        return "existing-contract-oracle-delegation"
    if check_id == "openapi-frontend-types-signal":
        if "skipped" in tokens:
            return "report-only-fingerprint-skipped"
        if "fingerprint" in tokens:
            return "report-only-fingerprint-record"
    return _slug(normalized)


def _normalized_reason_text(reason: str) -> str:
    text = reason.lower()
    text = text.replace("report only", "report-only")
    text = text.replace("allow listed", "allowlisted")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _slug(text: str) -> str:
    return "-".join(text.split()) or "unspecified"


def _summary_counts(findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "by_check_id": _count_by(findings, "check_id"),
        "by_priority": _count_by(findings, "priority"),
        "by_role": _count_by(findings, "role"),
        "by_allowlist_state": _count_by_with_defaults(
            findings,
            "allowlist_state",
            ("allowlisted", "unallowlisted"),
        ),
        "by_gate_eligibility": _boolean_count_by(
            findings,
            "gate_eligible",
            true_key="gate_eligible",
            false_key="not_gate_eligible",
        ),
        "by_budget_count": _boolean_count_by(
            findings,
            "budget_counted",
            true_key="budget_counted",
            false_key="not_budget_counted",
        ),
    }


def _count_by(findings: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = str(finding[field])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _count_by_with_defaults(
    findings: list[dict[str, object]],
    field: str,
    defaults: tuple[str, ...],
) -> dict[str, int]:
    counts = {key: 0 for key in defaults}
    counts.update(_count_by(findings, field))
    return counts


def _boolean_count_by(
    findings: list[dict[str, object]],
    field: str,
    *,
    true_key: str,
    false_key: str,
) -> dict[str, int]:
    counts = {true_key: 0, false_key: 0}
    for finding in findings:
        counts[true_key if bool(finding[field]) else false_key] += 1
    return counts


def _module_heatmap(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    modules: dict[str, dict[str, object]] = {}
    for finding in findings:
        module = str(finding["module"])
        row = modules.setdefault(
            module,
            {
                "module": module,
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
        )
        row["finding_count"] = int(row["finding_count"]) + 1
        axis = str(finding["axis"])
        severity = str(finding["severity"])
        if SCORE_RANK[severity] > SCORE_RANK[str(row[axis])]:
            row[axis] = severity
        priority = str(finding["priority"])
        if PRIORITY_RANK[priority] > PRIORITY_RANK[str(row["priority"])]:
            row["priority"] = priority
    return sorted(
        modules.values(),
        key=lambda row: (-PRIORITY_RANK[str(row["priority"])], -int(row["finding_count"]), str(row["module"])),
    )


def _high_spread_patterns(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for finding in findings:
        grouped[str(finding["check_id"])].append(finding)
    patterns: list[dict[str, object]] = []
    for check_id, group in grouped.items():
        modules = sorted({str(finding["module"]) for finding in group})
        if len(group) < 2 and len(modules) < 2:
            continue
        priorities = [str(finding["priority"]) for finding in group]
        severities = [str(finding["severity"]) for finding in group]
        patterns.append(
            {
                "pattern": check_id,
                "occurrence_count": len(group),
                "module_count": len(modules),
                "modules": modules,
                "roles": sorted({str(finding["role"]) for finding in group}),
                "governance_faces": sorted({str(finding["governance_face"]) for finding in group}),
                "top_priority": max(priorities, key=lambda item: PRIORITY_RANK[item]),
                "top_severity": max(severities, key=lambda item: SEVERITY_RANK[item]),
            }
        )
    return sorted(
        patterns,
        key=lambda item: (
            -PRIORITY_RANK[str(item["top_priority"])],
            -int(item["occurrence_count"]),
            str(item["pattern"]),
        ),
    )


def _existing_files(paths: Iterable[Path]) -> list[Path]:
    return sorted({path for path in paths if path.is_file()})


def _iter_text_files(root: Path, roots: Iterable[Path]) -> Iterable[Path]:
    root = root.resolve(strict=False)
    for scan_root in roots:
        if scan_root.is_file():
            if _repo_text_rejection_reason(root, scan_root) is None:
                yield scan_root
            continue
        if not scan_root.exists():
            continue
        if not _is_scannable_dir(root, scan_root):
            continue
        for current, dirnames, filenames in os.walk(scan_root):
            current_path = Path(current)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if _is_scannable_dir(root, current_path / dirname)
            ]
            for filename in filenames:
                path = current_path / filename
                if _repo_text_rejection_reason(root, path) is None:
                    yield path


def _iter_python_files(root: Path, roots: Iterable[Path]) -> Iterable[Path]:
    for path in _iter_text_files(root, roots):
        if path.suffix == ".py":
            yield path


def _is_scannable_dir(root: Path, path: Path) -> bool:
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            return False
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    except OSError:
        return False
    if any(part in SCAN_SKIP_DIRS for part in relative.parts):
        return False
    if any(part.startswith(SCAN_SKIP_PREFIXES) for part in relative.parts):
        return False
    return not (relative.parts and relative.parts[0] in SCAN_SKIP_ROOT_DIRS)


def _read_repo_text(root: Path, path: Path) -> str:
    if _repo_text_rejection_reason(root, path) is not None:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_SCANNED_TEXT_FILE_BYTES)
    except OSError:
        return ""


def _repo_text_rejection_reason(root: Path, path: Path) -> str | None:
    root_resolved = root.resolve(strict=False)
    try:
        file_stat = path.lstat()
    except OSError:
        return "stat-error"
    if stat.S_ISLNK(file_stat.st_mode):
        return "symlink"
    if not stat.S_ISREG(file_stat.st_mode):
        return "not-regular-file"
    try:
        relative = path.resolve(strict=False).relative_to(root_resolved)
    except (OSError, ValueError):
        return "outside-repo"
    if _repo_relative_path_is_skipped(relative):
        return "skipped-path"
    if not _has_scannable_text_name(path):
        return "unsupported-extension"
    if file_stat.st_size > MAX_SCANNED_TEXT_FILE_BYTES:
        return f"exceeds-{MAX_SCANNED_TEXT_FILE_BYTES}-bytes"
    return None


def _repo_relative_path_is_skipped(relative: Path) -> bool:
    if any(part in SCAN_SKIP_DIRS for part in relative.parts):
        return True
    if any(part.startswith(SCAN_SKIP_PREFIXES) for part in relative.parts):
        return True
    return bool(relative.parts and relative.parts[0] in SCAN_SKIP_ROOT_DIRS)


def _has_scannable_text_name(path: Path) -> bool:
    return (
        path.suffix in TEXT_EXTENSIONS
        or path.name in {"Makefile", ".gitignore", ".dockerignore", ".env"}
        or path.name.startswith(".env.")
    )


def _matching_lines(text: str, tokens: tuple[str, ...]) -> Iterable[tuple[int, str]]:
    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(token in line for token in tokens):
            yield line_no, line


def _normalized_apps_api_import_modules(tree: ast.AST) -> frozenset[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "apps.api" or alias.name.startswith("apps.api."):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                continue
            if module == "apps":
                for alias in node.names:
                    if alias.name == "api":
                        modules.add("apps.api")
                    elif alias.name == "*":
                        modules.add("apps.api.*")
            elif module == "apps.api":
                for alias in node.names:
                    modules.add("apps.api.*" if alias.name == "*" else f"apps.api.{alias.name}")
            elif module.startswith("apps.api."):
                modules.add(f"{module}.*" if any(alias.name == "*" for alias in node.names) else module)
    return frozenset(modules)


def _stale_route_allowlist_reason(
    relative_path: str,
    lines: list[str],
    line_no: int,
    token: str,
    mention_context: _StaleRouteMentionContext,
) -> str | None:
    if relative_path.startswith("openspec/changes/m26-"):
        return "M26 route-consolidation evidence or redirect contract"
    if token == "HydroMetPage":
        if relative_path in {"progress.md"}:
            return "current entrypoint summarizes historical milestone context"
        if relative_path.startswith("apps/frontend/src/lib/hydroMet/"):
            return "library extraction provenance comment"
        if _hydromet_page_historical_context(mention_context.governing_text):
            return "historical pre-M26 display evidence"
    context_class = _stale_route_context_class(relative_path, mention_context)
    if context_class == "redirect":
        return "M26 route-consolidation redirect alias"
    if context_class == "historical":
        return "historical plan or pre-M26 display evidence"
    if context_class == "active":
        return None
    if context_class == "compatibility":
        return "legacy route compatibility context"
    if relative_path.startswith("docs/plans/") or relative_path.startswith("openspec/changes/m22-"):
        return "historical plan or pre-M26 display evidence"
    if relative_path in {"progress.md"}:
        return "current entrypoint summarizes historical milestone context"
    if "__tests__" in relative_path and token == "/hydro-met":
        return "frontend redirect regression test"
    if _frontend_e2e_legacy_route_context_allowlist(relative_path, lines, line_no, token):
        return "frontend redirect regression test"
    return None


def _stale_route_line_contexts(relative_path: str, lines: list[str]) -> list[_StaleRouteLineContext]:
    historical_banner_lines = (
        _historical_route_authority_banner_line_numbers(lines)
        if relative_path.startswith("docs/runbooks/")
        else frozenset()
    )
    has_historical_route_authority_banner = bool(historical_banner_lines)
    section_headings = _markdown_section_headings(lines) if _path_uses_markdown_route_context(relative_path) else ()
    return [
        _stale_route_line_context(
            relative_path,
            lines,
            line_index,
            section_headings,
            historical_banner_lines,
            has_historical_route_authority_banner,
        )
        for line_index in range(len(lines))
    ]


def _stale_route_line_context(
    relative_path: str,
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    historical_banner_lines: frozenset[int],
    has_historical_route_authority_banner: bool,
    *,
    structural_context: _StaleRouteStructuralContext | None = None,
) -> _StaleRouteLineContext:
    line = lines[line_index]
    clause_ranges = tuple(_stale_route_clause_ranges(line))
    clause_texts = tuple(line[start:end].strip() for start, end in clause_ranges)
    clause_has_per_mention_redirect_syntax = tuple(
        _clause_has_per_mention_redirect_syntax(line[start:end])
        for start, end in clause_ranges
    )
    clause_analyses = tuple(_stale_route_clause_analysis(line, start, end) for start, end in clause_ranges)
    mention_facts = _stale_route_mention_facts_by_span(
        line,
        clause_ranges,
        tuple(start for start, _end in clause_ranges),
        clause_analyses,
    )
    structural_context = structural_context or _stale_route_structural_context(
        relative_path,
        lines,
        line_index,
        section_headings,
    )
    governing_text = structural_context.governing_text
    redirect_governing_texts = tuple(structural_context.redirect_governing_text for _clause_text in clause_texts)
    mention_governing_texts = tuple(
        _stale_route_governing_mention_text(clause_text, governing_text)
        for clause_text in clause_texts
    )
    return _StaleRouteLineContext(
        line=line,
        clause_ranges=clause_ranges,
        clause_starts=tuple(start for start, _end in clause_ranges),
        clause_texts=clause_texts,
        clause_has_per_mention_redirect_syntax=clause_has_per_mention_redirect_syntax,
        clause_analyses=clause_analyses,
        mention_facts=mention_facts,
        redirect_governing_texts=redirect_governing_texts,
        mention_governing_texts=mention_governing_texts,
        governing_text=governing_text,
        has_historical_route_authority_banner=(line_index + 1) in historical_banner_lines,
        document_has_historical_route_authority_banner=has_historical_route_authority_banner,
    )


def _stale_route_clause_ranges(line: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(line):
        if char in "|,，;。；" or (char == "." and _period_is_sentence_boundary(line, index)):
            ranges.append((start, index))
            start = index + 1
    if ranges:
        ranges.append((start, len(line)))
    else:
        ranges = [(0, len(line))]
    return _split_stale_route_active_clause_connectors(line, ranges)


def _split_stale_route_active_clause_connectors(
    line: str,
    ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    split_ranges: list[tuple[int, int]] = []
    for start, end in ranges:
        cursor = start
        for match in ROUTE_ACTIVE_CLAUSE_CONNECTOR_PATTERN.finditer(line, start, end):
            if not _stale_route_connector_splits_clause(line, start, end, match.start()):
                continue
            split_ranges.append((cursor, match.start()))
            connector_text = line[match.start() : match.end()]
            active_start = match.start()
            if connector_text.lower().startswith(("and ", "then ")):
                active_start = line.find(" ", match.start(), match.end()) + 1
            cursor = active_start
        split_ranges.append((cursor, end))
    return split_ranges or [(0, len(line))]


def _stale_route_connector_splits_clause(
    line: str,
    start: int,
    end: int,
    connector_start: int,
) -> bool:
    before = line[start:connector_start]
    after = line[connector_start:end]
    return bool(
        LEGACY_DISPLAY_ROUTE_PATTERN.search(before)
        and ROUTE_REDIRECT_WORD_PATTERN.search(before)
        and LEGACY_DISPLAY_ROUTE_PATTERN.search(after)
    )


def _clause_has_per_mention_redirect_syntax(clause: str) -> bool:
    return bool("->" in clause or "→" in clause)


def _stale_route_clause_analysis(line: str, start: int, end: int) -> _StaleRouteClauseAnalysis:
    route_valued_matches = tuple(ROUTE_VALUED_LEGACY_DISPLAY_ROUTE_PATTERN.finditer(line, start, end))
    clause_text = line[start:end].strip()
    clause_tokens = frozenset(_normalized_reason_text(clause_text).split())
    return _StaleRouteClauseAnalysis(
        route_valued_match_starts=tuple(match.start() for match in route_valued_matches),
        route_valued_token_spans=frozenset(
            (match.start("token"), match.end("token")) for match in route_valued_matches
        ),
        redirect_word_spans=tuple(
            (match.start(), match.end()) for match in ROUTE_REDIRECT_WORD_PATTERN.finditer(line, start, end)
        ),
        active_instruction_context=_line_has_active_route_instruction_context(clause_text),
        active_route_valued_context=_line_has_active_route_valued_context(
            clause_text,
            set(clause_tokens),
            redirect_text="",
        ),
    )


def _stale_route_mention_facts_by_span(
    line: str,
    clause_ranges: tuple[tuple[int, int], ...],
    clause_starts: tuple[int, ...],
    clause_analyses: tuple[_StaleRouteClauseAnalysis, ...],
) -> dict[tuple[int, int], _StaleRouteMentionFacts]:
    facts: dict[tuple[int, int], _StaleRouteMentionFacts] = {}
    for match in HYDROMET_PAGE_IDENTIFIER_PATTERN.finditer(line):
        _record_stale_route_mention_facts(
            facts,
            line,
            clause_ranges,
            clause_starts,
            clause_analyses,
            match.start(),
            match.end(),
        )
    for match in LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN.finditer(line):
        _record_stale_route_mention_facts(
            facts,
            line,
            clause_ranges,
            clause_starts,
            clause_analyses,
            match.start("token"),
            match.end("token"),
        )
    return facts


def _record_stale_route_mention_facts(
    facts: dict[tuple[int, int], _StaleRouteMentionFacts],
    line: str,
    clause_ranges: tuple[tuple[int, int], ...],
    clause_starts: tuple[int, ...],
    clause_analyses: tuple[_StaleRouteClauseAnalysis, ...],
    token_start: int,
    token_end: int,
) -> None:
    key = (token_start, token_end)
    if key in facts:
        return
    clause_index = max(0, bisect_right(clause_starts, token_start) - 1)
    left, right = clause_ranges[clause_index]
    arrow_shape = _route_arrow_shape_for_bounds(line, left, right, token_start, token_end)
    if arrow_shape == "arrow-from-token":
        left = max(left, token_start)
    elif arrow_shape == "arrow-to-token":
        right = min(right, token_end)
    analysis = clause_analyses[clause_index]
    route_valued = (token_start, token_end) in analysis.route_valued_token_spans
    redirect_local = _stale_route_mention_has_redirect_syntax_from_analysis(
        line,
        left,
        right,
        token_start,
        token_end,
        analysis,
        arrow_shape,
        route_valued,
    )
    facts[key] = _StaleRouteMentionFacts(
        clause_index=clause_index,
        left=left,
        right=right,
        arrow_shape=arrow_shape,
        route_valued=route_valued,
        redirect_local=redirect_local,
        semantic_key=_stale_route_precomputed_semantic_key(analysis, route_valued, redirect_local),
    )


def _stale_route_precomputed_semantic_key(
    analysis: _StaleRouteClauseAnalysis,
    route_valued: bool,
    redirect_local: bool,
) -> str:
    if route_valued and analysis.active_route_valued_context:
        return "active-local"
    if redirect_local:
        return "redirect-local"
    if analysis.active_instruction_context or analysis.active_route_valued_context:
        return "active-local"
    return "context-local"


def _stale_route_mention_facts(
    line_context: _StaleRouteLineContext,
    token_start: int,
    token_end: int,
) -> _StaleRouteMentionFacts:
    facts = line_context.mention_facts.get((token_start, token_end))
    if facts is not None:
        return facts
    clause_index = max(0, bisect_right(line_context.clause_starts, token_start) - 1)
    left, right = line_context.clause_ranges[clause_index]
    arrow_shape = _route_arrow_shape_for_bounds(line_context.line, left, right, token_start, token_end)
    if arrow_shape == "arrow-from-token":
        left = max(left, token_start)
    elif arrow_shape == "arrow-to-token":
        right = min(right, token_end)
    analysis = line_context.clause_analyses[clause_index]
    route_valued = (token_start, token_end) in analysis.route_valued_token_spans
    redirect_local = _stale_route_mention_has_redirect_syntax_from_analysis(
        line_context.line,
        left,
        right,
        token_start,
        token_end,
        analysis,
        arrow_shape,
        route_valued,
    )
    return _StaleRouteMentionFacts(
        clause_index=clause_index,
        left=left,
        right=right,
        arrow_shape=arrow_shape,
        route_valued=route_valued,
        redirect_local=redirect_local,
        semantic_key=_stale_route_precomputed_semantic_key(analysis, route_valued, redirect_local),
    )


def _stale_route_mention_context(
    line_context: _StaleRouteLineContext,
    token_start: int,
    token_end: int,
) -> _StaleRouteMentionContext:
    facts = _stale_route_mention_facts(line_context, token_start, token_end)
    clause_index = facts.clause_index
    line = line_context.line
    left, right = facts.left, facts.right

    clause = line[left:right].strip()
    explicit_redirect_text = _stale_route_mention_redirect_span_from_facts(
        line_context,
        facts,
        token_start,
        token_end,
    )
    redirect_text = _stale_route_join_redirect_text(
        _stale_route_redirect_governing_text(line_context, clause_index),
        explicit_redirect_text,
    )
    governing_text = line_context.mention_governing_texts[clause_index]
    return _StaleRouteMentionContext(
        clause=clause,
        explicit_redirect_text=explicit_redirect_text,
        redirect_text=redirect_text,
        governing_text=governing_text,
        has_historical_route_authority_banner=line_context.has_historical_route_authority_banner,
        document_has_historical_route_authority_banner=line_context.document_has_historical_route_authority_banner,
    )


def _stale_route_duplicate_key(
    line_context: _StaleRouteLineContext,
    token: str,
    token_start: int,
) -> _StaleRouteDuplicateKey:
    token_end = token_start + len(token)
    facts = _stale_route_mention_facts(line_context, token_start, token_end)
    clause_index = facts.clause_index
    if line_context.clause_has_per_mention_redirect_syntax[clause_index]:
        clause_key = (
            line_context.clause_texts[clause_index],
            facts.arrow_shape,
        )
    else:
        clause_key = (
            line_context.mention_governing_texts[clause_index],
            facts.semantic_key,
        )
    return (
        token,
        clause_key,
        line_context.mention_governing_texts[clause_index],
        _stale_route_redirect_governing_text(line_context, clause_index),
        line_context.has_historical_route_authority_banner,
        line_context.document_has_historical_route_authority_banner,
    )


def _stale_route_mention_semantic_key(
    line_context: _StaleRouteLineContext,
    clause_index: int,
    token_start: int,
    token_end: int,
) -> str:
    facts = _stale_route_mention_facts(line_context, token_start, token_end)
    if facts.clause_index != clause_index:
        return "context-local"
    return facts.semantic_key


def _stale_route_mention_has_redirect_syntax(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
) -> bool:
    arrow_shape = _route_arrow_shape_for_bounds(line, left, right, token_start, token_end)
    analysis = _stale_route_clause_analysis(line, left, right)
    token_is_route_valued = (token_start, token_end) in analysis.route_valued_token_spans
    return _stale_route_mention_has_redirect_syntax_from_analysis(
        line,
        left,
        right,
        token_start,
        token_end,
        analysis,
        arrow_shape,
        token_is_route_valued,
    )


def _stale_route_mention_has_redirect_syntax_from_analysis(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
    analysis: _StaleRouteClauseAnalysis,
    arrow_shape: str,
    token_is_route_valued: bool,
) -> bool:
    if arrow_shape in {"arrow-from-token", "arrow-to-token"}:
        return True
    clause = line[left:right]
    if "->" in clause or "→" in clause:
        return False
    word_start_limit = right
    following_route_valued_starts = [
        match_start for match_start in analysis.route_valued_match_starts if match_start >= token_end
    ]
    if following_route_valued_starts:
        word_start_limit = min(following_route_valued_starts)
    for word_start, word_end in analysis.redirect_word_spans:
        if word_start >= word_start_limit:
            continue
        if token_end <= word_start:
            segment = line[token_end:word_start]
        elif token_start >= word_end:
            if token_is_route_valued:
                continue
            segment = line[word_end:token_start]
        else:
            return True
        if _route_redirect_connector_segment(segment):
            return True
    return False


def _route_token_is_route_valued(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
) -> bool:
    return (token_start, token_end) in _stale_route_clause_analysis(line, left, right).route_valued_token_spans


def _route_arrow_shape_near_token(
    line_context: _StaleRouteLineContext,
    clause_index: int,
    token_start: int,
    token_end: int,
) -> str:
    left, right = line_context.clause_ranges[clause_index]
    return _route_arrow_shape_for_bounds(line_context.line, left, right, token_start, token_end)


def _route_arrow_shape_for_bounds(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
) -> str:
    after = line[token_end : min(right, token_end + 16)].lstrip()
    if after.startswith(("->", "→")):
        return "arrow-from-token"
    before = line[max(left, token_start - 16) : token_start].rstrip()
    if before.endswith(("->", "→")):
        return "arrow-to-token"
    return "no-local-arrow"


def _stale_route_mention_context_key(
    line_context: _StaleRouteLineContext,
    token_start: int,
    token_end: int,
) -> tuple[object, ...]:
    clause_index = max(0, bisect_right(line_context.clause_starts, token_start) - 1)
    line = line_context.line
    left, right = _stale_route_mention_clause_bounds(line_context, clause_index, token_start, token_end)
    arrow_from = _route_arrow_points_from_token(line, token_start, token_end)
    arrow_to = _route_arrow_points_to_token(line, token_start)
    clause_key: object = line_context.mention_governing_texts[clause_index]
    if arrow_from or arrow_to:
        clause_key = (left, right)
    return (
        clause_key,
        line_context.mention_governing_texts[clause_index],
        _stale_route_mention_redirect_text(line_context, clause_index, left, right, token_start, token_end),
        line_context.has_historical_route_authority_banner,
        line_context.document_has_historical_route_authority_banner,
    )


def _stale_route_mention_clause_bounds(
    line_context: _StaleRouteLineContext,
    clause_index: int,
    token_start: int,
    token_end: int,
) -> tuple[int, int]:
    facts = _stale_route_mention_facts(line_context, token_start, token_end)
    if facts.clause_index != clause_index:
        return line_context.clause_ranges[clause_index]
    return facts.left, facts.right


def _stale_route_mention_redirect_text(
    line_context: _StaleRouteLineContext,
    clause_index: int,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
) -> str:
    return _stale_route_join_redirect_text(
        _stale_route_redirect_governing_text(line_context, clause_index),
        _stale_route_mention_redirect_span_from_facts(
            line_context,
            _stale_route_mention_facts(line_context, token_start, token_end),
            token_start,
            token_end,
        ),
    )


def _stale_route_join_redirect_text(*parts: str) -> str:
    return " ".join(part for part in parts if part).strip()


def _stale_route_redirect_governing_text(
    line_context: _StaleRouteLineContext,
    clause_index: int,
) -> str:
    return line_context.redirect_governing_texts[clause_index]


def _stale_route_mention_redirect_span(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
) -> str:
    analysis = _stale_route_clause_analysis(line, left, right)
    arrow_shape = _route_arrow_shape_for_bounds(line, left, right, token_start, token_end)
    token_is_route_valued = (token_start, token_end) in analysis.route_valued_token_spans
    return _stale_route_mention_redirect_span_from_analysis(
        line,
        left,
        right,
        token_start,
        token_end,
        analysis,
        arrow_shape,
        token_is_route_valued,
    )


def _stale_route_mention_redirect_span_from_facts(
    line_context: _StaleRouteLineContext,
    facts: _StaleRouteMentionFacts,
    token_start: int,
    token_end: int,
) -> str:
    analysis = line_context.clause_analyses[facts.clause_index]
    return _stale_route_mention_redirect_span_from_analysis(
        line_context.line,
        facts.left,
        facts.right,
        token_start,
        token_end,
        analysis,
        facts.arrow_shape,
        facts.route_valued,
    )


def _stale_route_mention_redirect_span_from_analysis(
    line: str,
    left: int,
    right: int,
    token_start: int,
    token_end: int,
    analysis: _StaleRouteClauseAnalysis,
    arrow_shape: str,
    token_is_route_valued: bool,
) -> str:
    if arrow_shape == "arrow-from-token":
        return line[token_start:right].strip()
    if arrow_shape == "arrow-to-token":
        return line[left:token_end].strip()
    clause = line[left:right]
    if "->" in clause or "→" in clause:
        return ""
    span_right = word_start_limit = right
    following_route_valued_starts = [
        match_start for match_start in analysis.route_valued_match_starts if match_start >= token_end
    ]
    if following_route_valued_starts:
        span_right = min(following_route_valued_starts)
        word_start_limit = span_right
    for word_start, word_end in analysis.redirect_word_spans:
        if word_start >= word_start_limit:
            continue
        if token_end <= word_start:
            segment = line[token_end:word_start]
        elif token_start >= word_end:
            if token_is_route_valued:
                continue
            segment = line[word_end:token_start]
        else:
            return line[left:span_right].strip()
        if _route_redirect_connector_segment(segment):
            return line[left:span_right].strip()
    return ""


def _route_redirect_connector_segment(segment: str) -> bool:
    text = LEGACY_DISPLAY_ROUTE_PATTERN.sub(" ", segment)
    tokens = set(_normalized_reason_text(text).split())
    if not tokens:
        return True
    return tokens <= {
        "alias",
        "aliases",
        "and",
        "as",
        "backward",
        "backwards",
        "belong",
        "belongs",
        "compatibility",
        "compatible",
        "deep",
        "deeplink",
        "deeplinks",
        "element",
        "from",
        "keep",
        "keeps",
        "kept",
        "legacy",
        "legacyredirect",
        "link",
        "links",
        "old",
        "only",
        "or",
        "path",
        "route",
        "routes",
        "to",
    }


def _stale_route_structural_context(
    relative_path: str,
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> _StaleRouteStructuralContext:
    governing_text = _stale_route_governing_text(relative_path, lines, line_index, section_headings)
    redirect_governing_text = _stale_route_redirect_governing_text_for_line(
        lines,
        line_index,
        governing_text,
        section_headings,
    )
    return _StaleRouteStructuralContext(
        governing_text=governing_text,
        redirect_governing_text=redirect_governing_text,
    )


def _stale_route_redirect_governing_texts(
    lines: list[str],
    line_index: int,
    clause_texts: tuple[str, ...],
    governing_text: str,
    section_headings: tuple[str | None, ...],
) -> tuple[str, ...]:
    redirect_governing_text = _stale_route_redirect_governing_text_for_line(
        lines,
        line_index,
        governing_text,
        section_headings,
    )
    return tuple(redirect_governing_text for _clause_text in clause_texts)


def _stale_route_redirect_governing_text_for_line(
    lines: list[str],
    line_index: int,
    governing_text: str,
    section_headings: tuple[str | None, ...],
) -> str:
    line = lines[line_index]
    if _line_is_markdown_table_row(line):
        blockquote_depth = _markdown_blockquote_depth(line)
        table_start = line_index
        while (
            table_start > 0
            and _markdown_blockquote_depth(lines[table_start - 1]) == blockquote_depth
            and _line_is_markdown_table_row(lines[table_start - 1])
        ):
            table_start -= 1
        return " ".join(_preceding_context_lines(lines, table_start, section_headings))
    if _line_is_list_or_list_continuation(lines, line_index):
        return _stale_route_list_redirect_governing_text(lines, line_index, section_headings)
    return governing_text


def _stale_route_list_redirect_governing_texts(
    lines: list[str],
    line_index: int,
    clause_texts: tuple[str, ...],
    section_headings: tuple[str | None, ...],
) -> tuple[str, ...]:
    context = _stale_route_list_redirect_governing_text(lines, line_index, section_headings)
    return tuple(context for _clause_text in clause_texts)


def _stale_route_list_redirect_governing_text(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> str:
    list_start = _list_item_start_index(lines, line_index)
    list_indent = _list_item_indent_width(lines[list_start])
    item_end = _list_item_end_index(lines, list_start, list_indent)
    return _stale_route_list_redirect_governing_text_for_range(
        lines,
        line_index,
        section_headings,
        list_start,
        list_indent,
        item_end,
    )


def _stale_route_list_redirect_governing_text_for_range(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    list_start: int,
    list_indent: int,
    item_end: int,
) -> str:
    parts = []
    heading = _section_heading_at(section_headings, list_start)
    if heading:
        parts.append(heading)
    parts.extend(lines[index].strip() for index in _parent_list_item_indexes(lines, list_start, list_indent))
    if line_index != list_start:
        parts.append(lines[list_start].strip())
    parts.extend(
        line.strip()
        for line in _same_list_item_following_lines(lines, line_index, list_start, item_end)
        if _line_has_redirect_alias_context(line, set(_normalized_reason_text(line).split()))
    )
    context = " ".join(part for part in parts if part).strip()
    return context


def _same_list_item_following_lines(
    lines: list[str],
    line_index: int,
    list_start: int,
    item_end: int,
) -> list[str]:
    following_lines = []
    for index in range(line_index + 1, item_end):
        if _line_starts_markdown_list_item(lines[index]):
            break
        following_lines.append(lines[index])
    return following_lines


def _stale_route_governing_mention_text(
    clause_text: str,
    governing_text: str,
) -> str:
    parts = [clause_text]
    if governing_text:
        parts.append(governing_text)
    return " ".join(part for part in parts if part).strip()


def _stale_route_governing_text(
    relative_path: str,
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> str:
    if not _path_uses_markdown_route_context(relative_path):
        return ""
    line = lines[line_index]
    if _line_is_markdown_table_row(line):
        return _stale_route_table_governing_text(lines, line_index, section_headings)
    if _line_is_list_or_list_continuation(lines, line_index):
        return _stale_route_list_governing_text(lines, line_index, section_headings)
    return _stale_route_paragraph_governing_text(lines, line_index, section_headings)


def _path_uses_markdown_route_context(relative_path: str) -> bool:
    return relative_path.endswith((".md", ".rst", ".txt"))


def _markdown_section_headings(lines: list[str]) -> tuple[str | None, ...]:
    headings: list[str | None] = []
    current_headings_by_blockquote_depth: dict[int, str] = {}
    previous_blockquote_depth = 0
    for line in lines:
        blockquote_depth = _markdown_blockquote_depth(line)
        normalized = _markdown_context_line(line)
        if blockquote_depth < previous_blockquote_depth:
            current_headings_by_blockquote_depth = {
                depth: heading
                for depth, heading in current_headings_by_blockquote_depth.items()
                if depth <= blockquote_depth
            }
        if MARKDOWN_HEADING_PATTERN.match(normalized):
            current_headings_by_blockquote_depth = {
                depth: heading
                for depth, heading in current_headings_by_blockquote_depth.items()
                if depth <= blockquote_depth
            }
            current_headings_by_blockquote_depth[blockquote_depth] = normalized.strip()
        headings.append(current_headings_by_blockquote_depth.get(blockquote_depth))
        previous_blockquote_depth = blockquote_depth
    return tuple(headings)


def _markdown_context_line(line: str) -> str:
    previous = line
    while True:
        normalized = MARKDOWN_BLOCKQUOTE_PREFIX_PATTERN.sub("", previous)
        if normalized == previous:
            return normalized
        previous = normalized


def _markdown_blockquote_depth(line: str) -> int:
    depth = 0
    previous = line
    while True:
        normalized = MARKDOWN_BLOCKQUOTE_PREFIX_PATTERN.sub("", previous)
        if normalized == previous:
            return depth
        depth += 1
        previous = normalized


def _markdown_context_stripped(line: str) -> str:
    return _markdown_context_line(line).strip()


def _line_starts_markdown_list_item(line: str) -> bool:
    return bool(MARKDOWN_LIST_ITEM_PATTERN.match(_markdown_context_line(line)))


def _line_starts_markdown_heading(line: str) -> bool:
    return bool(MARKDOWN_HEADING_PATTERN.match(_markdown_context_line(line)))


def _line_is_markdown_table_row(line: str) -> bool:
    stripped = _markdown_context_stripped(line)
    return stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]


def _stale_route_table_governing_text(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> str:
    blockquote_depth = _markdown_blockquote_depth(lines[line_index])
    start = line_index
    while (
        start > 0
        and _markdown_blockquote_depth(lines[start - 1]) == blockquote_depth
        and _line_is_markdown_table_row(lines[start - 1])
    ):
        start -= 1
    end = line_index + 1
    while (
        end < len(lines)
        and _markdown_blockquote_depth(lines[end]) == blockquote_depth
        and _line_is_markdown_table_row(lines[end])
    ):
        end += 1

    return _stale_route_table_governing_text_for_range(lines, line_index, section_headings, start, end)


def _stale_route_table_governing_text_for_range(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    start: int,
    end: int,
) -> str:
    parts = _preceding_context_lines(lines, start, section_headings)
    row_indexes = [start]
    if start + 1 < end and MARKDOWN_TABLE_SEPARATOR_PATTERN.match(_markdown_context_stripped(lines[start + 1])):
        row_indexes.append(start + 1)
    row_indexes.append(line_index)
    for row_index in row_indexes:
        if not MARKDOWN_TABLE_SEPARATOR_PATTERN.match(_markdown_context_stripped(lines[row_index])):
            parts.append(lines[row_index].strip())
    return " ".join(part for part in parts if part).strip()


def _line_is_list_or_list_continuation(lines: list[str], line_index: int) -> bool:
    line = lines[line_index]
    normalized = _markdown_context_line(line)
    if MARKDOWN_LIST_ITEM_PATTERN.match(normalized):
        return True
    if not normalized.startswith((" ", "\t")):
        return False
    blockquote_depth = _markdown_blockquote_depth(line)
    cursor = line_index - 1
    while cursor >= 0:
        previous = lines[cursor]
        if _markdown_blockquote_depth(previous) != blockquote_depth:
            return False
        previous_normalized = _markdown_context_line(previous)
        if not previous_normalized.strip():
            return False
        if MARKDOWN_LIST_ITEM_PATTERN.match(previous_normalized):
            return True
        if not previous_normalized.startswith((" ", "\t")):
            return False
        cursor -= 1
    return False


def _stale_route_list_governing_text(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> str:
    list_start = _list_item_start_index(lines, line_index)
    list_indent = _list_item_indent_width(lines[list_start])
    item_end = _list_item_end_index(lines, list_start, list_indent)
    return _stale_route_list_governing_text_for_range(
        lines,
        line_index,
        section_headings,
        list_start,
        list_indent,
        item_end,
    )


def _stale_route_list_governing_text_for_range(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    list_start: int,
    list_indent: int,
    item_end: int,
) -> str:
    parent_indexes = _parent_list_item_indexes(lines, list_start, list_indent)
    heading = _section_heading_at(section_headings, list_start)
    parts = [heading] if heading else []
    parts.extend(lines[index].strip() for index in parent_indexes)
    parts.extend(line.strip() for line in lines[list_start:item_end])
    return " ".join(part for part in parts if part).strip()


def _list_item_start_index(lines: list[str], line_index: int) -> int:
    if _line_starts_markdown_list_item(lines[line_index]):
        return line_index
    blockquote_depth = _markdown_blockquote_depth(lines[line_index])
    cursor = line_index - 1
    while cursor >= 0:
        line = lines[cursor]
        if _markdown_blockquote_depth(line) != blockquote_depth:
            break
        normalized = _markdown_context_line(line)
        if not normalized.strip() or MARKDOWN_HEADING_PATTERN.match(normalized):
            break
        if MARKDOWN_LIST_ITEM_PATTERN.match(normalized):
            return cursor
        if not normalized.startswith((" ", "\t")):
            break
        cursor -= 1
    return line_index


def _list_item_end_index(lines: list[str], start_index: int, item_indent: int) -> int:
    blockquote_depth = _markdown_blockquote_depth(lines[start_index])
    end = start_index + 1
    while end < len(lines):
        current = lines[end]
        if _markdown_blockquote_depth(current) != blockquote_depth:
            break
        normalized = _markdown_context_line(current)
        if not normalized.strip() or MARKDOWN_HEADING_PATTERN.match(normalized):
            break
        if MARKDOWN_LIST_ITEM_PATTERN.match(normalized):
            indent = _list_item_indent_width(current)
            if indent <= item_indent:
                break
            end += 1
            continue
        if normalized.startswith((" ", "\t")):
            end += 1
            continue
        break
    return end


def _parent_list_item_indexes(lines: list[str], start_index: int, item_indent: int) -> list[int]:
    parent_indexes: list[int] = []
    cursor = start_index - 1
    current_indent = item_indent
    blockquote_depth = _markdown_blockquote_depth(lines[start_index])
    while cursor >= 0:
        line = lines[cursor]
        if _markdown_blockquote_depth(line) != blockquote_depth:
            break
        normalized = _markdown_context_line(line)
        if not normalized.strip() or MARKDOWN_HEADING_PATTERN.match(normalized):
            break
        if MARKDOWN_LIST_ITEM_PATTERN.match(normalized):
            indent = _list_item_indent_width(line)
            if indent < current_indent:
                parent_indexes.append(cursor)
                current_indent = indent
            cursor -= 1
            continue
        if not normalized.startswith((" ", "\t")):
            break
        cursor -= 1
    return list(reversed(parent_indexes))


def _list_item_indent_width(line: str) -> int:
    normalized = _markdown_context_line(line)
    match = MARKDOWN_LIST_ITEM_PATTERN.match(normalized)
    if not match:
        return len(normalized) - len(normalized.lstrip(" \t"))
    return _markdown_indent_width(match.group("indent"))


def _markdown_indent_width(indent: str) -> int:
    width = 0
    for char in indent:
        width += 4 if char == "\t" else 1
    return width


def _stale_route_paragraph_governing_text(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
) -> str:
    blockquote_depth = _markdown_blockquote_depth(lines[line_index])
    start = line_index
    while (
        start > 0
        and _markdown_blockquote_depth(lines[start - 1]) == blockquote_depth
        and _line_continues_route_paragraph(lines[start - 1])
    ):
        if not _line_wraps_to_next(lines[start - 1]) and not _line_continues_previous(lines[start]):
            break
        start -= 1
    end = line_index + 1
    while (
        end < len(lines)
        and _markdown_blockquote_depth(lines[end]) == blockquote_depth
        and _line_continues_route_paragraph(lines[end])
    ):
        if not _line_wraps_to_next(lines[end - 1]) and not _line_continues_previous(lines[end]):
            break
        end += 1

    return _stale_route_paragraph_governing_text_for_range(lines, line_index, section_headings, start, end)


def _stale_route_paragraph_governing_text_for_range(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    start: int,
    end: int,
) -> str:
    heading = _section_heading_at(section_headings, start)
    if start == line_index and end == line_index + 1:
        return heading or ""
    parts = [heading] if heading else []
    parts.extend(line.strip() for line in lines[start:end] if line.strip())
    return " ".join(part for part in parts if part).strip()


def _line_continues_route_paragraph(line: str) -> bool:
    stripped = _markdown_context_stripped(line)
    return bool(
        stripped
        and not _line_starts_markdown_heading(line)
        and not _line_is_markdown_table_row(line)
        and not MARKDOWN_TABLE_SEPARATOR_PATTERN.match(stripped)
        and not _line_starts_markdown_list_item(line)
    )


def _line_wraps_to_next(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped:
        return False
    return bool(
        stripped.endswith(("/", "、", ",", "，", "(", "（", "->", "→"))
        or stripped.endswith((" and", " or", " and/or"))
        or (
            stripped[-1].islower()
            and not stripped.endswith((".", ":", ";", "。", "：", "；"))
            and len(stripped) >= 88
        )
    )


def _line_continues_previous(line: str) -> bool:
    stripped = line.lstrip()
    return bool(stripped.startswith(("/", "、", "，", ",", "and ", "or ", "compatibility ")))


def _preceding_context_lines(
    lines: list[str],
    start_index: int,
    section_headings: tuple[str | None, ...],
) -> list[str]:
    parts: list[str] = []
    blockquote_depth = _markdown_blockquote_depth(lines[start_index]) if 0 <= start_index < len(lines) else 0
    cursor = start_index - 1
    while cursor >= 0 and len(parts) < 3:
        line = lines[cursor]
        if _markdown_blockquote_depth(line) != blockquote_depth:
            break
        stripped = _markdown_context_stripped(line)
        if not stripped:
            if parts:
                break
            cursor -= 1
            continue
        if _line_is_markdown_table_row(line) or MARKDOWN_TABLE_SEPARATOR_PATTERN.match(stripped):
            break
        parts.append(stripped)
        if _line_starts_markdown_heading(line) or _line_starts_markdown_list_item(line):
            break
        cursor -= 1
    if not any(MARKDOWN_HEADING_PATTERN.match(part) for part in parts):
        heading = _section_heading_at(section_headings, start_index)
        if heading:
            parts.append(heading)
    return list(reversed(parts))


def _section_heading_at(section_headings: tuple[str | None, ...], line_index: int) -> str | None:
    if not section_headings or line_index < 0:
        return None
    return section_headings[min(line_index, len(section_headings) - 1)]


def _period_is_sentence_boundary(line: str, index: int) -> bool:
    next_index = index + 1
    return next_index >= len(line) or line[next_index].isspace()


def _route_arrow_points_from_token(line: str, token_start: int, token_end: int) -> bool:
    return line[token_end:].lstrip().startswith(("->", "→"))


def _route_arrow_points_to_token(line: str, token_start: int) -> bool:
    return line[:token_start].rstrip().endswith(("->", "→"))


def _historical_route_authority_banner_line_numbers(lines: list[str]) -> frozenset[int]:
    header_end = min(20, len(lines))
    header_text = "\n".join(lines[:header_end]).lower()
    if not _text_has_historical_marker(header_text) or not _text_has_route_authority_marker(header_text):
        return frozenset()

    banner_lines: set[int] = set()
    for line_index, line in enumerate(lines[:header_end]):
        if _text_has_historical_marker(line) or _text_has_route_authority_marker(line):
            banner_lines.add(line_index + 1)
            if line.startswith(">"):
                cursor = line_index + 1
                while cursor < header_end and lines[cursor].startswith(">"):
                    banner_lines.add(cursor + 1)
                    cursor += 1

    for line_index, line in enumerate(lines):
        if line_index >= header_end and _line_starts_new_current_section(line):
            break
        normalized = _normalized_reason_text(line)
        tokens = set(normalized.split())
        if _line_has_historical_context("", tokens):
            banner_lines.add(line_index + 1)
    return frozenset(banner_lines)


def _line_starts_new_current_section(line: str) -> bool:
    if not MARKDOWN_HEADING_PATTERN.match(line):
        return False
    normalized = _normalized_reason_text(line)
    tokens = set(normalized.split())
    return not bool(tokens & {"historical", "history", "superseded", "archive", "archived"})


def _text_has_historical_marker(text: str) -> bool:
    normalized = _normalized_reason_text(text)
    tokens = set(normalized.split())
    return bool(
        tokens & {"historical", "history", "superseded"}
        or "历史" in text
        or "已被" in text
    )


def _text_has_route_authority_marker(text: str) -> bool:
    text = text.lower()
    return bool(
        "current route authority" in text
        or "route authority" in text
        or "single-map" in text
        or "single map" in text
    )


def _frontend_e2e_legacy_route_context_allowlist(
    relative_path: str,
    lines: list[str],
    line_no: int,
    token: str,
) -> bool:
    if token != "/hydro-met" or not relative_path.startswith("apps/frontend/e2e/"):
        return False
    line_index = line_no - 1
    start = max(0, line_index - 2)
    end = min(len(lines), line_index + 3)
    context = _normalized_reason_text("\n".join(lines[start:end]))
    tokens = set(context.split())
    return "legacyredirect" in tokens or ("redirect" in tokens and bool(tokens & {"legacy", "m26"}))


def _stale_route_context_class(
    relative_path: str,
    line: _StaleRouteMentionContext,
) -> Literal[
    "active",
    "historical",
    "redirect",
    "compatibility",
    "drift",
]:
    normalized = _normalized_reason_text(line.governing_text)
    tokens = set(normalized.split())
    explicit_redirect_tokens = set(_normalized_reason_text(line.explicit_redirect_text).split())
    if _line_has_redirect_alias_context(line.explicit_redirect_text, explicit_redirect_tokens):
        return "redirect"
    has_active_route_context = _line_has_active_route_instruction_context(
        line.clause
    ) or _line_has_terse_active_route_context(
        line.clause
    ) or _line_has_active_route_valued_context(
        line.governing_text,
        tokens,
        redirect_text=line.explicit_redirect_text,
        allow_evidence_boundary=True,
    )
    if _line_has_evidence_boundary_context(tokens) and has_active_route_context:
        return "active"
    if _line_has_historical_context(relative_path, tokens):
        return "historical"
    if (
        line.document_has_historical_route_authority_banner
        and not _line_has_current_route_governing_context(line.governing_text)
    ):
        return "historical"
    if has_active_route_context:
        return "active"
    redirect_tokens = set(_normalized_reason_text(line.redirect_text).split())
    if _line_has_redirect_alias_context(line.redirect_text, redirect_tokens):
        return "redirect"
    if _line_has_compatibility_context(tokens):
        return "compatibility"
    return "drift"


def _line_has_redirect_alias_context(line: str, tokens: set[str]) -> bool:
    return (
        _line_has_route_redirect_arrow(line)
        or "redirect" in tokens
        or "redirects" in tokens
        or "redirected" in tokens
        or "legacyredirect" in tokens
        or "重定向" in line
    )


def _line_has_route_redirect_arrow(line: str) -> bool:
    for arrow in ("->", "→"):
        if arrow not in line:
            continue
        left, right = line.split(arrow, maxsplit=1)
        if LEGACY_DISPLAY_ROUTE_PATTERN.search(left) and ROUTE_REDIRECT_TARGET_PATTERN.search(right):
            return True
    return False


def _line_has_active_route_instruction_context(line: str) -> bool:
    normalized = _normalized_reason_text(line)
    tokens = set(normalized.split())
    if bool(tokens & {"open", "visit", "browse", "navigate"}) and bool(
        tokens & {"proof", "evidence", "browser", "display", "route", "page"}
    ):
        return True
    if "打开" in line and bool(tokens & {"proof", "evidence", "browser", "display", "route", "page"}):
        return True
    if "use" in tokens and bool(tokens & {"active", "current", "live"}) and bool(
        tokens & {"display", "entrypoint", "page", "route"}
    ):
        return True
    return _line_has_active_route_valued_context(line, tokens)


def _line_has_terse_active_route_context(line: str) -> bool:
    if not LEGACY_DISPLAY_ROUTE_PATTERN.search(line):
        return False
    tokens = set(_normalized_reason_text(line).split())
    if _line_has_redirect_alias_context(line, tokens):
        return False
    if bool(tokens & {"open", "visit", "browse", "navigate"}):
        return True
    return "current" in tokens and "route" in tokens and bool(
        tokens & {"display", "entrypoint", "page", "path"}
    )


def _line_has_current_route_governing_context(line: str) -> bool:
    tokens = _normalized_reason_text(line).split()
    current_markers = {"active", "current", "currently", "live"}
    negation_markers = {"former", "formerly", "no", "non", "not", "previously", "without"}
    evidence_boundary_markers = {
        "blocked",
        "blocker",
        "blockers",
        "deterministic",
        "diagnostic",
        "fail",
        "failed",
        "fixture",
        "fixtures",
        "mocked",
        "skipped",
    }
    governing_markers = {
        "browser",
        "browse",
        "current",
        "display",
        "entrypoint",
        "navigate",
        "open",
        "operator",
        "page",
        "procedure",
        "procedures",
        "proof",
        "route",
        "use",
        "visit",
    }
    token_set = set(tokens)
    if evidence_boundary_markers & token_set:
        return False
    if _line_has_historical_context("", token_set):
        return False
    for index, token in enumerate(tokens):
        if token not in current_markers:
            continue
        if set(tokens[max(0, index - 4) : index]) & negation_markers:
            continue
        return bool(governing_markers & token_set)
    return False


def _line_has_active_route_valued_context(
    line: str,
    tokens: set[str],
    *,
    redirect_text: str | None = None,
    allow_evidence_boundary: bool = False,
) -> bool:
    if not ROUTE_VALUED_LEGACY_DISPLAY_ROUTE_PATTERN.search(line):
        return False
    redirect_context = line if redirect_text is None else redirect_text
    redirect_tokens = tokens if redirect_text is None else set(_normalized_reason_text(redirect_text).split())
    if _line_has_redirect_alias_context(redirect_context, redirect_tokens) or _line_has_historical_context(
        "",
        tokens,
        allow_evidence_boundary=allow_evidence_boundary,
    ):
        return False
    if _line_has_compatibility_context(tokens) and not _line_has_current_route_governing_context(line):
        return False
    return True


def _hydromet_page_historical_context(text: str) -> bool:
    normalized = _normalized_reason_text(text)
    tokens = set(normalized.split())
    return bool(
        tokens & {"delete", "deleted", "remove", "removed", "retire", "retired", "toy"}
        or "已删" in text
        or "删除" in text
        or "玩具页" in text
    )


def _line_has_compatibility_context(tokens: set[str]) -> bool:
    return bool(
        tokens
        & {
            "compatibility",
            "compatible",
            "backward",
            "backwards",
            "deep",
            "deeplink",
            "deeplinks",
            "links",
            "bookmark",
            "bookmarks",
        }
    )


def _line_has_historical_context(
    relative_path: str,
    tokens: set[str],
    *,
    allow_evidence_boundary: bool = False,
) -> bool:
    if relative_path.startswith(("docs/plans/", "openspec/changes/m22-", "openspec/changes/m26-")):
        return True
    if tokens & {"superseded", "archive", "archived", "milestone"}:
        return True
    if _line_has_evidence_boundary_context(tokens) and not allow_evidence_boundary:
        return True
    if tokens & {"evidence", "pre"} and tokens & {"historical", "history", "m26"}:
        return True
    return False


def _line_has_evidence_boundary_context(tokens: set[str]) -> bool:
    return "evidence" in tokens and "boundary" in tokens


def _placeholder_path_allowlist_reason(relative_path: str) -> str | None:
    if relative_path == "docs/governance/LEGACY_DEAD_CODE_INVENTORY.md":
        return "governance inventory documents retired placeholder paths"
    if relative_path.startswith("docs/archived/"):
        return "governed archived evidence documents retired placeholder paths"
    if relative_path.startswith("openspec/changes/governance-2-legacy-dead-code-retirement/"):
        return "governed completed OpenSpec evidence documents retired placeholder paths"
    if relative_path.startswith("openspec/changes/governance-5-e1-entropy-baseline-burndown/"):
        return "governed Governance-5 E1 fixture evidence documents retired placeholder paths"
    return None


def _path_has_label(relative_path: str, labels: set[str]) -> bool:
    parts = re.split(r"[^a-z0-9]+", relative_path.lower())
    return any(part in labels for part in parts)


def _module_for_path(root: Path, path: Path) -> str:
    return _module_for_relative(_rel(root, path))


def _module_for_relative(relative: str) -> str:
    parts = Path(relative).parts
    if not parts:
        return "repo"
    if parts[0] in {"apps", "services", "workers", "packages"} and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    if parts[0] == "openspec" and len(parts) >= 3:
        return f"openspec/{parts[2]}"
    if parts[0] == "docs" and len(parts) >= 2:
        return f"docs/{parts[1]}"
    if parts[0] == ".github":
        return ".github/workflows"
    if parts[0] == "infra" and len(parts) >= 2:
        return f"infra/{parts[1]}"
    return parts[0]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.as_posix()


def _artifact_fingerprint_pair_reason(root: Path, left: Path, right: Path) -> tuple[str, bool]:
    left_fingerprint = _bounded_artifact_sha256(root, left, MAX_ARTIFACT_FINGERPRINT_BYTES)
    right_fingerprint = _bounded_artifact_sha256(root, right, MAX_ARTIFACT_FINGERPRINT_BYTES)
    failures = [
        fingerprint
        for fingerprint in (left_fingerprint, right_fingerprint)
        if fingerprint.startswith("skipped:")
    ]
    if failures:
        return f"report-only fingerprint skipped ({'; '.join(failures)})", False
    hash_pair = f"{left_fingerprint}:{right_fingerprint}"
    return f"report-only fingerprint {hash_pair[:24]}", True


def _bounded_artifact_sha256(root: Path, path: Path, max_bytes: int) -> str:
    try:
        root_resolved = root.resolve(strict=False)
        relative = path.absolute().relative_to(root_resolved)
    except (OSError, ValueError):
        return f"skipped:{path.as_posix()}:outside-repo"
    rel = relative.as_posix()
    try:
        file_stat = path.lstat()
    except OSError:
        return f"skipped:{rel}:stat-error"
    if stat.S_ISLNK(file_stat.st_mode):
        return f"skipped:{rel}:symlink"
    if not stat.S_ISREG(file_stat.st_mode):
        return f"skipped:{rel}:not-regular-file"
    if file_stat.st_size > max_bytes:
        return f"skipped:{rel}:exceeds-{max_bytes}-bytes"
    try:
        return _file_sha256(path, max_bytes)
    except OSError:
        return f"skipped:{rel}:read-error"


def _file_sha256(path: Path, max_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = max_bytes
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _git_tracked_paths(root: Path, pathspecs: Iterable[str] = ()) -> list[str]:
    command = ["git", "ls-files", "-z"]
    scoped_pathspecs = list(pathspecs)
    if scoped_pathspecs:
        command.extend(["--", *scoped_pathspecs])
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


if __name__ == "__main__":
    sys.exit(main())
