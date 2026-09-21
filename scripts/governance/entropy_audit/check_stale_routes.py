"""The ``stale-display-route-token`` check and its per-document context cache.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Holds the
check entrypoint, the two scope predicates that decide which token set a path is
scanned for, the match generator, and ``_StaleRouteContextFactory`` -- the
per-document memo that keeps the markdown context work linear in document size."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from scripts.governance.entropy_audit.archive_status import _complete_archive_status_marker_ranges
from scripts.governance.entropy_audit.constants import (
    HYDROMET_PAGE_IDENTIFIER_PATTERN,
    LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN,
)
from scripts.governance.entropy_audit.findings import _normalized_reason_text
from scripts.governance.entropy_audit.repo_files import _iter_text_files, _module_for_path, _read_repo_text, _rel
from scripts.governance.entropy_audit.route_governing_text import (
    _historical_route_authority_banner_line_numbers,
    _line_continues_previous,
    _line_continues_route_paragraph,
    _line_has_redirect_alias_context,
    _line_is_list_or_list_continuation,
    _line_is_markdown_table_row,
    _line_starts_markdown_list_item,
    _line_wraps_to_next,
    _list_item_end_index,
    _list_item_indent_width,
    _list_item_start_index,
    _markdown_blockquote_depth,
    _markdown_section_headings,
    _parent_list_item_indexes,
    _path_uses_markdown_route_context,
    _section_heading_at,
    _stale_route_paragraph_governing_text_for_range,
    _stale_route_table_governing_text_for_range,
    _stale_route_table_redirect_governing_text,
)
from scripts.governance.entropy_audit.route_mentions import (
    _stale_route_allowlist_reason,
    _stale_route_duplicate_key,
    _stale_route_line_context,
    _stale_route_mention_context,
)
from scripts.governance.entropy_audit.schema import (
    FindingSpec,
    _StaleRouteDuplicateKey,
    _StaleRouteLineContext,
    _StaleRouteLineMatch,
    _StaleRouteStructuralContext,
)


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
            context = _stale_route_table_redirect_governing_text(
                lines,
                line_index,
                section_headings,
                start,
            )
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
        text = _read_repo_text(root, path)
        lines = text.splitlines()
        archive_status_markers = _complete_archive_status_marker_ranges(lines)
        for match in _stale_route_line_matches(
            rel,
            lines,
            include_legacy_tokens=_path_is_legacy_route_token_scope(rel),
            include_expanded_aliases=_path_is_route_authority_expanded_scope(rel),
        ):
            allowed_reason = _stale_route_allowlist_reason(
                rel,
                lines,
                match.line_no,
                match.token,
                match.context,
                archive_status_markers=archive_status_markers,
            )
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
        relative_path.startswith(("docs/archived/", "docs/runbooks/", "openspec/changes/archive/"))
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
