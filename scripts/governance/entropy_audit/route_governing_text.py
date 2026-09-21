"""Markdown context extraction for the stale-display-route family.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Given a line
and its document, these helpers recover the governing text of the construct the
mention sits in -- table row, list item (including parent items), paragraph or
section heading -- and the redirect/historical/compatibility classifiers built
on it."""

from __future__ import annotations

from typing import Literal

from scripts.governance.entropy_audit.constants import (
    LEGACY_DISPLAY_ROUTE_PATTERN,
    MARKDOWN_BLOCKQUOTE_PREFIX_PATTERN,
    MARKDOWN_HEADING_PATTERN,
    MARKDOWN_LIST_ITEM_PATTERN,
    MARKDOWN_TABLE_SEPARATOR_PATTERN,
    ROUTE_REDIRECT_TARGET_PATTERN,
    ROUTE_VALUED_LEGACY_DISPLAY_ROUTE_PATTERN,
)
from scripts.governance.entropy_audit.findings import _normalized_reason_text
from scripts.governance.entropy_audit.schema import _StaleRouteMentionContext, _StaleRouteStructuralContext


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
        return _stale_route_table_redirect_governing_text(
            lines,
            line_index,
            section_headings,
            table_start,
        )
    if _line_is_list_or_list_continuation(lines, line_index):
        return _stale_route_list_redirect_governing_text(lines, line_index, section_headings)
    return governing_text


def _stale_route_table_redirect_governing_text(
    lines: list[str],
    line_index: int,
    section_headings: tuple[str | None, ...],
    table_start: int,
) -> str:
    # A markdown table row names the route in one cell and its disposition in the next, so
    # the redirect wording is never inside the mention's own clause. Paragraph context
    # already includes its own line and list context includes its item's lines; the table
    # branch excluded the mention's row, which is the asymmetry that made a table-shaped
    # "重定向到 `/`" invisible. Only the mention's own row is added - not the header, not
    # sibling rows, not the rest of the document.
    parts = [
        *_preceding_context_lines(lines, table_start, section_headings),
        lines[line_index].strip(),
    ]
    return " ".join(part for part in parts if part).strip()


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
