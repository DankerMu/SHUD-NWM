"""The ownership-growth signal: base-ref diff, added lines and record shapes.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Reads the
bounded ``git diff`` against the comparison base, matches added lines against
the structural patterns, builds the growth signal and its detail tokens, and
owns the JSON record shapes for budget files, exemptions and signals."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    HASH_CHUNK_BYTES,
    MAX_SCANNED_TEXT_FILE_BYTES,
    STRUCTURAL_COMPATIBILITY_PATTERN,
    STRUCTURAL_DIFF_HUNK_PATTERN,
    STRUCTURAL_DIFF_MAX_LINE_BYTES,
    STRUCTURAL_DIFF_READ_CHUNK_BYTES,
    STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES,
    STRUCTURAL_FILE_BUDGET_TOP_LIMIT,
    STRUCTURAL_PARSER_VALIDATOR_PATTERN,
    STRUCTURAL_PUBLIC_SURFACE_DETAIL_TOKEN_LIMIT,
    STRUCTURAL_PYTHON_DECLARATION_PATTERN,
    STRUCTURAL_TOKEN_HASH_HEX_CHARS,
)
from scripts.governance.entropy_audit.schema import (
    StructuralBudgetClass,
    _BoundedGitBlobText,
    _StructuralAddedLine,
    _StructuralAddedLines,
    _StructuralBudgetExemption,
    _StructuralBudgetFile,
    _StructuralOwnershipGrowthSignal,
    _StructuralUnknownLineCountFile,
)
from scripts.governance.entropy_audit.structural_sources import (
    _structural_added_import_families,
    _structural_bounded_import_families,
    _structural_current_import_families,
)
from scripts.governance.entropy_audit.structural_surface import (
    _structural_bounded_detail_token,
    _structural_bounded_public_surface_tokens,
    _structural_current_public_surface_tokens,
    _structural_public_surface_tokens,
    _structural_python_partial_public_surface_tokens,
)


def _structural_budget_review_reason(
    budget_class: StructuralBudgetClass,
    line_count: int,
    ownership_surface_signals: tuple[str, ...],
) -> str:
    if budget_class == "mandatory-governance":
        return (
            f"exceeds {STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES} physical lines; "
            "structural disposition is required before adding ownership surface"
        )
    if ownership_surface_signals:
        return "yellow-zone with mixed ownership signals: " + ", ".join(ownership_surface_signals)
    return f"yellow-zone line count ({line_count} physical lines); review-only"


def _structural_budget_owner_action(budget_class: StructuralBudgetClass) -> str:
    if budget_class == "mandatory-governance":
        return (
            "Update the structural inventory/disposition when ownership surface grows; "
            "do not require immediate splitting from this report-only signal."
        )
    return "Review ownership before expanding responsibilities; no split is required by this report-only signal."


def _structural_ownership_growth_signals(
    root: Path,
    record: _StructuralBudgetFile,
    comparison_base_ref: str | None,
) -> list[_StructuralOwnershipGrowthSignal]:
    if comparison_base_ref is None:
        return []
    added_result = _git_added_lines(root, record.relative_path, comparison_base_ref)
    added_lines = added_result.lines
    if not added_lines:
        if added_result.truncated:
            return [
                _structural_growth_signal(
                    record,
                    "diff-analysis-truncated",
                    _structural_diff_truncation_detail(added_result),
                )
            ]
        return []
    added_text = "\n".join(line.text for line in added_lines)
    base_blob = _git_blob_text_prefix(root, comparison_base_ref, record.relative_path)
    base_import_families = (
        set(_structural_bounded_import_families(record.relative_path, base_blob))
        if base_blob is not None
        else set()
    )
    current_import_families = _structural_current_import_families(root, record.relative_path)
    detected_import_families = (
        set(current_import_families)
        if current_import_families is not None
        else set(_structural_added_import_families(record.relative_path, added_lines))
    )
    signals: list[_StructuralOwnershipGrowthSignal] = []
    new_import_families = sorted(detected_import_families - base_import_families)
    if new_import_families:
        signals.append(
            _structural_growth_signal(
                record,
                "new-import-family",
                _structural_import_family_detail(new_import_families),
            )
        )
    current_public_tokens = _structural_current_public_surface_tokens(root, record.relative_path)
    public_tokens = _structural_new_public_surface_tokens(
        root,
        record.relative_path,
        added_lines,
        added_text,
        base_blob,
        current_public_tokens,
    )
    if public_tokens:
        signals.append(
            _structural_growth_signal(
                record,
                "public-entrypoint",
                _structural_public_surface_detail(public_tokens),
            )
        )
    compatibility_lines = _matching_structural_added_lines(added_lines, STRUCTURAL_COMPATIBILITY_PATTERN)
    if compatibility_lines:
        signals.append(
            _structural_growth_signal(
                record,
                "compatibility-symbol",
                "new compatibility/alias surface: " + _summarize_added_line_matches(compatibility_lines),
            )
        )
    parser_validator_lines = _matching_structural_added_lines(added_lines, STRUCTURAL_PARSER_VALIDATOR_PATTERN)
    if parser_validator_lines:
        signals.append(
            _structural_growth_signal(
                record,
                "parser-validator-responsibility",
                "new parser/validator responsibility: "
                + _summarize_added_line_matches(parser_validator_lines),
            )
        )
    if added_result.truncated:
        signals.append(
            _structural_growth_signal(
                record,
                "diff-analysis-truncated",
                _structural_diff_truncation_detail(added_result),
            )
        )
    return signals


def _structural_new_public_surface_tokens(
    root: Path,
    relative_path: str,
    added_lines: tuple[_StructuralAddedLine, ...],
    added_text: str,
    base_blob: _BoundedGitBlobText | None,
    current_public_tokens: tuple[str, ...] | None,
) -> tuple[str, ...]:
    base_public_tokens = (
        set(_structural_bounded_public_surface_tokens(relative_path, base_blob))
        if base_blob is not None
        else set()
    )
    if base_blob is not None and current_public_tokens is not None:
        return tuple(sorted(set(current_public_tokens) - base_public_tokens))
    detected_tokens = set(_structural_public_surface_tokens(relative_path, added_text, python_partial=True))
    if Path(relative_path).suffix == ".py":
        detected_tokens.update(
            _structural_python_current_context_public_surface_tokens(root / relative_path, added_lines)
        )
    return tuple(sorted(detected_tokens - base_public_tokens))


def _structural_public_surface_detail(public_tokens: tuple[str, ...]) -> str:
    bounded_tokens = tuple(_structural_bounded_detail_token(token) for token in public_tokens)
    if len(bounded_tokens) <= STRUCTURAL_PUBLIC_SURFACE_DETAIL_TOKEN_LIMIT:
        return "new public surface tokens: " + ", ".join(bounded_tokens)
    shown = bounded_tokens[:STRUCTURAL_PUBLIC_SURFACE_DETAIL_TOKEN_LIMIT]
    remaining = len(bounded_tokens) - len(shown)
    return (
        f"new public surface tokens ({len(bounded_tokens)} total): "
        + ", ".join(shown)
        + f" (+{remaining} more)"
    )


def _structural_import_family_detail(import_families: list[str]) -> str:
    tokens = tuple(_structural_import_family_detail_token(family) for family in import_families)
    if len(tokens) <= STRUCTURAL_PUBLIC_SURFACE_DETAIL_TOKEN_LIMIT:
        return "new import family tokens: " + ", ".join(tokens)
    shown = tokens[:STRUCTURAL_PUBLIC_SURFACE_DETAIL_TOKEN_LIMIT]
    remaining = len(tokens) - len(shown)
    return (
        f"new import family tokens ({len(tokens)} total): "
        + ", ".join(shown)
        + f" (+{remaining} more)"
    )


def _structural_import_family_detail_token(import_family: str) -> str:
    digest = hashlib.sha256(import_family.encode("utf-8", errors="replace")).hexdigest()[
        :STRUCTURAL_TOKEN_HASH_HEX_CHARS
    ]
    return f"import-sha256-{digest}"


def _structural_python_current_context_public_surface_tokens(
    path: Path,
    added_lines: tuple[_StructuralAddedLine, ...],
) -> tuple[str, ...]:
    target_lines = sorted(
        {
            line.line_number
            for line in added_lines
            if line.line_number is not None and STRUCTURAL_PYTHON_DECLARATION_PATTERN.match(line.text)
        }
    )
    if not target_lines:
        return ()
    ranges = [
        (
            1,
            line_number,
        )
        for line_number in target_lines
    ]
    tokens: set[str] = set()
    for context_text in _read_structural_python_line_ranges(path, ranges):
        tokens.update(_structural_python_partial_public_surface_tokens(context_text))
    return tuple(sorted(tokens))


def _read_structural_python_line_ranges(
    path: Path,
    ranges: list[tuple[int, int]],
) -> tuple[str, ...]:
    merged_ranges: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged_ranges or start > merged_ranges[-1][1] + 1:
            merged_ranges.append((start, end))
            continue
        previous_start, previous_end = merged_ranges[-1]
        merged_ranges[-1] = (previous_start, max(previous_end, end))
    if not merged_ranges:
        return ()
    max_end = merged_ranges[-1][1]
    range_index = 0
    line_number = 1
    bytes_read = 0
    selected_lines: list[list[str]] = [[] for _ in merged_ranges]
    try:
        with path.open("rb") as handle:
            while line_number <= max_end:
                raw_line = handle.readline(STRUCTURAL_DIFF_MAX_LINE_BYTES + 1)
                if not raw_line:
                    break
                bytes_read += len(raw_line)
                if bytes_read > MAX_SCANNED_TEXT_FILE_BYTES:
                    break
                while range_index < len(merged_ranges) and line_number > merged_ranges[range_index][1]:
                    range_index += 1
                if range_index >= len(merged_ranges):
                    break
                start, end = merged_ranges[range_index]
                if start <= line_number <= end:
                    selected_lines[range_index].append(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
                line_number += 1
    except OSError:
        return ()
    return tuple("\n".join(lines) for lines in selected_lines if lines)


def _structural_diff_truncation_detail(added_result: _StructuralAddedLines) -> str:
    reason = added_result.truncation_reason or "unknown reason"
    return (
        f"git diff scan truncated ({reason}) after bounded parsing; "
        "ownership growth analysis may be incomplete"
    )


def _structural_growth_signal(
    record: _StructuralBudgetFile,
    signal_type: str,
    detail: str,
) -> _StructuralOwnershipGrowthSignal:
    return _StructuralOwnershipGrowthSignal(
        relative_path=record.relative_path,
        module=record.module,
        line_count=record.line_count,
        signal_type=signal_type,
        detail=detail,
        owner_action=(
            "Update structural inventory or move the new surface to the owning module; "
            "no immediate split is required by this report-only signal."
        ),
    )


def _matching_structural_added_lines(
    lines: tuple[_StructuralAddedLine, ...],
    pattern: re.Pattern[str],
) -> tuple[_StructuralAddedLine, ...]:
    return tuple(line for line in lines if pattern.search(line.text))


def _summarize_added_line_matches(lines: tuple[_StructuralAddedLine, ...]) -> str:
    line_numbers = [line.line_number for line in lines if line.line_number is not None]
    if not line_numbers:
        return f"{len(lines)} matching added line(s)"
    shown = line_numbers[:5]
    suffix = f" (+{len(line_numbers) - len(shown)} more)" if len(line_numbers) > len(shown) else ""
    return f"{len(lines)} matching added line(s) at line(s) {', '.join(str(number) for number in shown)}{suffix}"


def _git_added_lines(
    root: Path,
    relative_path: str,
    comparison_base_ref: str,
) -> _StructuralAddedLines:
    command = [
        "git",
        "diff",
        "--no-ext-diff",
        "--unified=0",
        comparison_base_ref,
        "--",
        relative_path,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return _StructuralAddedLines(lines=(), truncated=False)
    if process.stdout is None:
        process.kill()
        process.communicate()
        return _StructuralAddedLines(lines=(), truncated=False)
    added: list[_StructuralAddedLine] = []
    new_line_number: int | None = None
    scanned_bytes = 0
    line_buffer = bytearray()
    truncated = False
    truncation_reason: str | None = None

    def parse_diff_line(raw_line: bytes) -> None:
        nonlocal new_line_number
        line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
        hunk_match = STRUCTURAL_DIFF_HUNK_PATTERN.match(line)
        if hunk_match:
            new_line_number = int(hunk_match.group("start"))
            return
        if line.startswith("+") and not line.startswith("+++"):
            added.append(_StructuralAddedLine(line_number=new_line_number, text=line[1:]))
            if new_line_number is not None:
                new_line_number += 1
        elif line.startswith(" ") and new_line_number is not None:
            new_line_number += 1

    def consume_chunk(chunk: bytes) -> None:
        nonlocal line_buffer, truncated, truncation_reason
        start = 0
        while start < len(chunk):
            newline_index = chunk.find(b"\n", start)
            end = len(chunk) if newline_index == -1 else newline_index
            segment = chunk[start:end]
            remaining_line_bytes = STRUCTURAL_DIFF_MAX_LINE_BYTES - len(line_buffer)
            if len(segment) > remaining_line_bytes:
                line_buffer.extend(segment[:remaining_line_bytes])
                truncated = True
                truncation_reason = "diff-line-byte-cap"
                process.kill()
                return
            line_buffer.extend(segment)
            if newline_index == -1:
                return
            parse_diff_line(bytes(line_buffer))
            line_buffer.clear()
            start = newline_index + 1

    try:
        while True:
            chunk = process.stdout.read(STRUCTURAL_DIFF_READ_CHUNK_BYTES)
            if not chunk:
                break
            remaining_scan_bytes = MAX_SCANNED_TEXT_FILE_BYTES - scanned_bytes
            if len(chunk) > remaining_scan_bytes:
                if remaining_scan_bytes > 0:
                    consume_chunk(chunk[:remaining_scan_bytes])
                truncated = True
                truncation_reason = truncation_reason or "diff-output-byte-cap"
                process.kill()
                break
            scanned_bytes += len(chunk)
            consume_chunk(chunk)
            if truncated:
                break
            if scanned_bytes >= MAX_SCANNED_TEXT_FILE_BYTES:
                truncated = True
                truncation_reason = "diff-output-byte-cap"
                process.kill()
                break
        if not truncated and line_buffer:
            parse_diff_line(bytes(line_buffer))
            line_buffer.clear()
        if process.wait() != 0 and not truncated:
            return _StructuralAddedLines(lines=(), truncated=False)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    return _StructuralAddedLines(
        lines=tuple(added),
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


def _git_blob_text_prefix(root: Path, ref: str, relative_path: str) -> _BoundedGitBlobText | None:
    blob = f"{ref}:{relative_path}"
    size_bytes: int | None = None
    try:
        size_result = subprocess.run(
            ["git", "cat-file", "-s", blob],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        size_bytes = int(size_result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    try:
        process = subprocess.Popen(
            ["git", "show", blob],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if process.stdout is None:
        process.kill()
        process.communicate()
        return None
    chunks: list[bytes] = []
    remaining = MAX_SCANNED_TEXT_FILE_BYTES
    truncated = size_bytes > MAX_SCANNED_TEXT_FILE_BYTES
    try:
        while remaining > 0:
            chunk = process.stdout.read(min(HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if truncated and process.poll() is None:
            process.kill()
        returncode = process.wait()
        if returncode != 0 and not truncated:
            return None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    return _BoundedGitBlobText(
        text=b"".join(chunks).decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def _git_blob_text(root: Path, ref: str, relative_path: str) -> str | None:
    blob = _git_blob_text_prefix(root, ref, relative_path)
    if blob is None or blob.truncated:
        return None
    return blob.text


def _structural_budget_file_record(item: _StructuralBudgetFile) -> dict[str, object]:
    return {
        "path": item.relative_path,
        "line_count": item.line_count,
        "line_count_is_truncated": item.line_count_is_truncated,
        "line_count_lower_bound": item.line_count_lower_bound,
        "size_bytes": item.size_bytes,
        "module": item.module,
        "budget_class": item.budget_class,
        "import_family_tokens": [
            _structural_import_family_detail_token(family) for family in item.import_families
        ],
        "import_family_count": len(item.import_families),
        "ownership_surface_signals": list(item.ownership_surface_signals),
        "review_reason": item.review_reason,
        "owner_action": item.owner_action,
    }


def _structural_budget_exemption_record(item: _StructuralBudgetExemption) -> dict[str, object]:
    return {
        "path": item.relative_path,
        "line_count": item.line_count,
        "line_count_is_truncated": item.line_count_is_truncated,
        "line_count_lower_bound": item.line_count_lower_bound,
        "size_bytes": item.size_bytes,
        "module": item.module,
        "budget_class": "governed-exemption",
        "exemption_family": item.exemption_family,
        "exemption_reason": item.exemption_reason,
    }


def _structural_unknown_line_count_file_record(
    item: _StructuralUnknownLineCountFile,
) -> dict[str, object]:
    return {
        "path": item.relative_path,
        "line_count": item.line_count,
        "line_count_is_truncated": item.line_count_is_truncated,
        "line_count_lower_bound": item.line_count_lower_bound,
        "size_bytes": item.size_bytes,
        "module": item.module,
        "budget_class": "unknown-line-count",
        "review_reason": item.review_reason,
        "owner_action": item.owner_action,
    }


def _structural_growth_signal_record(item: _StructuralOwnershipGrowthSignal) -> dict[str, object]:
    return {
        "path": item.relative_path,
        "module": item.module,
        "line_count": item.line_count,
        "signal_type": item.signal_type,
        "detail": item.detail,
        "owner_action": item.owner_action,
    }


def _structural_top_oversized_modules(
    mandatory_files: list[_StructuralBudgetFile],
) -> list[dict[str, object]]:
    grouped: dict[str, list[_StructuralBudgetFile]] = defaultdict(list)
    for item in mandatory_files:
        grouped[item.module].append(item)
    rows: list[dict[str, object]] = []
    for module, items in grouped.items():
        sorted_items = sorted(items, key=lambda item: (-item.line_count, item.relative_path))
        rows.append(
            {
                "module": module,
                "oversized_file_count": len(items),
                "max_line_count": max(item.line_count for item in items),
                "total_line_count": sum(item.line_count for item in items),
                "paths": [item.relative_path for item in sorted_items[:3]],
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row["oversized_file_count"]),
            -int(row["max_line_count"]),
            str(row["module"]),
        ),
    )[:STRUCTURAL_FILE_BUDGET_TOP_LIMIT]


def _structural_top_oversized_files_by_module(
    mandatory_files: list[_StructuralBudgetFile],
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[_StructuralBudgetFile]] = defaultdict(list)
    for item in mandatory_files:
        grouped[item.module].append(item)
    return {
        module: [
            _structural_budget_file_record(item)
            for item in sorted(items, key=lambda row: (-row.line_count, row.relative_path))[:3]
        ]
        for module, items in sorted(grouped.items())
    }
