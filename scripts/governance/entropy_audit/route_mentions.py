"""Per-mention analysis of a legacy display-route token occurrence.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Owns the
clause segmentation (``_stale_route_clause_analysis``), the redirect-span and
semantic-key derivations, the per-mention context window and the allowlist
reason the stale-display-route check attaches to each finding."""

from __future__ import annotations

from bisect import bisect_right

from scripts.governance.entropy_audit.archive_status import _line_has_complete_archive_status_marker
from scripts.governance.entropy_audit.constants import (
    COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON,
    HYDROMET_PAGE_IDENTIFIER_PATTERN,
    LEGACY_DISPLAY_ROUTE_BOUNDARY_PATTERN,
    LEGACY_DISPLAY_ROUTE_PATTERN,
    ROUTE_ACTIVE_CLAUSE_CONNECTOR_PATTERN,
    ROUTE_REDIRECT_WORD_PATTERN,
    ROUTE_VALUED_LEGACY_DISPLAY_ROUTE_PATTERN,
)
from scripts.governance.entropy_audit.findings import _normalized_reason_text
from scripts.governance.entropy_audit.route_governing_text import (
    _frontend_e2e_legacy_route_context_allowlist,
    _historical_route_authority_banner_line_numbers,
    _hydromet_page_historical_context,
    _line_has_active_route_instruction_context,
    _line_has_active_route_valued_context,
    _markdown_section_headings,
    _path_uses_markdown_route_context,
    _period_is_sentence_boundary,
    _route_arrow_points_from_token,
    _route_arrow_points_to_token,
    _stale_route_context_class,
    _stale_route_governing_mention_text,
    _stale_route_structural_context,
)
from scripts.governance.entropy_audit.schema import (
    _ArchiveStatusMarkerRange,
    _StaleRouteClauseAnalysis,
    _StaleRouteDuplicateKey,
    _StaleRouteLineContext,
    _StaleRouteMentionContext,
    _StaleRouteMentionFacts,
    _StaleRouteStructuralContext,
)


def _stale_route_allowlist_reason(
    relative_path: str,
    lines: list[str],
    line_no: int,
    token: str,
    mention_context: _StaleRouteMentionContext,
    *,
    archive_status_markers: tuple[_ArchiveStatusMarkerRange, ...] = (),
) -> str | None:
    if _line_has_complete_archive_status_marker(line_no, archive_status_markers):
        return COMPLETE_ARCHIVE_STATUS_ALLOWLIST_REASON
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
