"""Archive-status marker parsing for markdown documents.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Parses the
``docs/governance/DOC_STATUS.md`` front-matter and section markers that let the
stale-route and placeholder-path families downgrade a hit to historical
evidence, including the completeness rules per status value."""

from __future__ import annotations

import re
from typing import Literal

from scripts.governance.entropy_audit.constants import (
    ARCHIVE_STATUS_FRONT_MATTER_MAX_LINES,
    ARCHIVE_STATUS_NON_CURRENT_VALUES,
    ARCHIVE_STATUS_RECOGNIZED_FIELDS,
    ARCHIVE_STATUS_REQUIRED_FIELDS_BY_STATUS,
)
from scripts.governance.entropy_audit.schema import _ArchiveStatusMarkerRange


def _complete_archive_status_marker_ranges(lines: list[str]) -> tuple[_ArchiveStatusMarkerRange, ...]:
    marker_ranges: list[_ArchiveStatusMarkerRange] = []
    whole_document_marker = _whole_document_archive_status_marker_range(lines)
    if whole_document_marker is not None:
        marker_ranges.append(whole_document_marker)
    marker_ranges.extend(_section_archive_status_marker_ranges(lines))
    return tuple(marker_ranges)


def _whole_document_archive_status_marker_range(lines: list[str]) -> _ArchiveStatusMarkerRange | None:
    if not lines or lines[0].strip() != "---":
        return None
    max_index = min(len(lines), ARCHIVE_STATUS_FRONT_MATTER_MAX_LINES + 1)
    for index in range(1, max_index):
        if lines[index].strip() != "---":
            continue
        fields = _archive_status_front_matter_fields(lines[1:index])
        if _archive_status_fields_are_complete(fields, expected_scope="whole-document"):
            return _ArchiveStatusMarkerRange(start_line=1, end_line=len(lines))
        return None
    return None


def _section_archive_status_marker_ranges(lines: list[str]) -> tuple[_ArchiveStatusMarkerRange, ...]:
    marker_ranges: list[_ArchiveStatusMarkerRange] = []
    index = 0
    in_fenced_code = False
    while index < len(lines):
        if _line_starts_markdown_fence(lines[index]):
            in_fenced_code = not in_fenced_code
            index += 1
            continue
        if in_fenced_code:
            index += 1
            continue
        if lines[index].strip().lower() != "archive status:":
            index += 1
            continue
        cursor = index + 1
        block_lines: list[str] = []
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if not stripped:
                break
            if stripped.startswith("-") or lines[cursor].startswith((" ", "\t")):
                block_lines.append(lines[cursor])
                cursor += 1
                continue
            break
        fields = _archive_status_section_block_fields(block_lines)
        content_start = cursor
        while content_start < len(lines) and not lines[content_start].strip():
            content_start += 1
        if _archive_status_fields_are_complete(fields, expected_scope="section"):
            start_line = content_start + 1
            end_line = _archive_status_section_end_line(lines, content_start)
            if start_line <= end_line:
                marker_ranges.append(_ArchiveStatusMarkerRange(start_line=start_line, end_line=end_line))
        index = max(cursor, index + 1)
    return tuple(marker_ranges)


def _archive_status_front_matter_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] not in {" ", "\t"} and not stripped.startswith("-"):
            parsed = _archive_status_key_value(stripped)
            if parsed is None or parsed[0] not in ARCHIVE_STATUS_RECOGNIZED_FIELDS:
                current_key = None
                continue
            current_key, value = parsed
            fields[current_key] = _archive_status_join_value(fields.get(current_key, ""), value)
            continue
        if current_key is not None:
            fields[current_key] = _archive_status_join_value(fields.get(current_key, ""), stripped)
    return fields


def _archive_status_section_block_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = stripped[1:].strip() if stripped.startswith("-") else stripped
        parsed = _archive_status_key_value(entry)
        if parsed is not None and parsed[0] in ARCHIVE_STATUS_RECOGNIZED_FIELDS:
            current_key, value = parsed
            fields[current_key] = _archive_status_join_value(fields.get(current_key, ""), value)
            continue
        if current_key is not None:
            fields[current_key] = _archive_status_join_value(fields.get(current_key, ""), stripped)
    return fields


def _archive_status_key_value(text: str) -> tuple[str, str] | None:
    key, separator, value = text.partition(":")
    if not separator:
        return None
    normalized_key = key.strip().lower().replace("-", "_")
    return normalized_key, value.strip()


def _archive_status_join_value(existing: str, value: str) -> str:
    value = value.strip()
    if not value:
        return existing
    return f"{existing} {value}".strip() if existing else value


def _archive_status_fields_are_complete(
    fields: dict[str, str],
    *,
    expected_scope: Literal["whole-document", "section"],
) -> bool:
    status = _archive_status_scalar(fields.get("status", ""))
    if status not in ARCHIVE_STATUS_NON_CURRENT_VALUES:
        return False
    required_fields = ARCHIVE_STATUS_REQUIRED_FIELDS_BY_STATUS[status]
    if not required_fields <= fields.keys():
        return False
    scope = _archive_status_scope(fields["archive_scope"])
    if scope != expected_scope:
        return False
    return all(
        _archive_status_field_has_required_value(field, fields[field])
        for field in required_fields
    )


def _archive_status_field_has_required_value(field: str, value: str) -> bool:
    scalar = _archive_status_scalar(value)
    if scalar in {"", "[]", "{}", "null", "~"}:
        return False
    if field != "superseded_by" and scalar == "none":
        return False
    return True


def _archive_status_scalar(value: str) -> str:
    return value.strip().strip("'\"").lower()


def _archive_status_scope(value: str) -> str:
    return _archive_status_scalar(value).replace("_", "-").replace(" ", "-")


def _archive_status_section_end_line(lines: list[str], content_start: int) -> int:
    if content_start >= len(lines):
        return len(lines)
    first_heading_level = _markdown_heading_level(lines[content_start])
    for index in range(content_start + 1, len(lines)):
        heading_level = _markdown_heading_level(lines[index])
        if heading_level is None:
            continue
        if first_heading_level is None or heading_level <= first_heading_level:
            return index
    return len(lines)


def _markdown_heading_level(line: str) -> int | None:
    match = re.match(r"^\s{0,3}(#{1,6})\s+", line)
    return len(match.group(1)) if match else None


def _line_starts_markdown_fence(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def _line_has_complete_archive_status_marker(
    line_no: int,
    archive_status_markers: tuple[_ArchiveStatusMarkerRange, ...],
) -> bool:
    return any(marker.start_line <= line_no <= marker.end_line for marker in archive_status_markers)
