"""Structural budget inputs: which files count, their line counts and imports.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Decides
which tracked paths are source-like, measures physical line counts under the
read bounds, resolves generated/fixture exemptions, and derives the import
family of a Python or TypeScript file (including the TS ignored-span scanner
that keeps string and regex literals out of the import match)."""

from __future__ import annotations

import ast
import hashlib
import re
import stat
import sys
import textwrap
from bisect import bisect_right
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    HASH_CHUNK_BYTES,
    MAX_SCANNED_TEXT_FILE_BYTES,
    SCAN_SKIP_DIRS,
    SCAN_SKIP_PREFIXES,
    STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES,
    STRUCTURAL_FIXTURE_LABELS,
    STRUCTURAL_GENERATED_HEADER_BYTES,
    STRUCTURAL_GENERATED_HEADER_LINES,
    STRUCTURAL_GENERATED_LABELS,
    STRUCTURAL_PROTOCOL_ROOTS,
    STRUCTURAL_SOURCE_EXTENSIONS,
    STRUCTURAL_SOURCE_FILENAMES,
    STRUCTURAL_SOURCE_ROOTS,
    STRUCTURAL_TOKEN_COMPONENT_MAX_CHARS,
    STRUCTURAL_TOKEN_HASH_HEX_CHARS,
    STRUCTURAL_TS_DYNAMIC_IMPORT_PATTERN,
    STRUCTURAL_TS_REQUIRE_PATTERN,
    STRUCTURAL_TS_STATIC_BARE_IMPORT_PATTERN,
    STRUCTURAL_TS_STATIC_FROM_IMPORT_PATTERN,
    STRUCTURAL_TS_STATIC_IMPORT_MAX_BLOCK_LINES,
)
from scripts.governance.entropy_audit.repo_files import _git_tracked_paths
from scripts.governance.entropy_audit.schema import (
    _BoundedGitBlobText,
    _StructuralAddedLine,
    _StructuralFileExemption,
    _StructuralPhysicalLineCount,
)


def _structural_tracked_source_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for relative_path in _git_tracked_paths(root):
        if _structural_path_is_source_like(relative_path):
            paths.append(relative_path)
    return sorted(paths)


def _structural_path_is_source_like(relative_path: str) -> bool:
    path = Path(relative_path)
    parts = path.parts
    if not parts:
        return False
    if any(part in SCAN_SKIP_DIRS for part in parts):
        return False
    if any(part.startswith(SCAN_SKIP_PREFIXES) for part in parts):
        return False
    if path.name in STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES:
        return True
    has_source_name = path.suffix in STRUCTURAL_SOURCE_EXTENSIONS or path.name in STRUCTURAL_SOURCE_FILENAMES
    if not has_source_name:
        return False
    return parts[0] in STRUCTURAL_SOURCE_ROOTS or path.name in STRUCTURAL_SOURCE_FILENAMES


def _structural_physical_line_count(path: Path) -> _StructuralPhysicalLineCount | None:
    try:
        file_stat = path.stat()
    except OSError:
        file_stat = None
    size_bytes = file_stat.st_size if file_stat is not None else None
    scan_limit = MAX_SCANNED_TEXT_FILE_BYTES
    try:
        line_count = 0
        saw_bytes = False
        last_byte = b""
        remaining = scan_limit
        with path.open("rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                saw_bytes = True
                line_count += chunk.count(b"\n")
                last_byte = chunk[-1:]
                remaining -= len(chunk)
    except OSError:
        return None
    if saw_bytes and last_byte != b"\n":
        line_count += 1
    truncated = size_bytes > scan_limit if size_bytes is not None else remaining == 0
    return _StructuralPhysicalLineCount(
        line_count=line_count,
        line_count_is_truncated=truncated,
        line_count_lower_bound=line_count,
        size_bytes=size_bytes,
    )


def _read_structural_header_text(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(STRUCTURAL_GENERATED_HEADER_BYTES)
    except OSError:
        return None
    decoded = data.decode("utf-8", errors="replace")
    return "\n".join(decoded.splitlines()[:STRUCTURAL_GENERATED_HEADER_LINES])


def _read_structural_analysis_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SCANNED_TEXT_FILE_BYTES:
            return None
        with path.open("rb") as handle:
            data = handle.read(MAX_SCANNED_TEXT_FILE_BYTES)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _structural_source_rejection_reason(root: Path, path: Path) -> str | None:
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
    if not _structural_path_is_source_like(relative.as_posix()):
        return "unsupported-source-name"
    return None


def _physical_line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _structural_source_exemption(relative_path: str, text: str) -> _StructuralFileExemption | None:
    path = Path(relative_path)
    parts = tuple(part.lower() for part in path.parts)
    labels = _structural_path_labels(relative_path)
    top_context = text.lower()
    if path.name in STRUCTURAL_DEPENDENCY_LOCKFILE_NAMES:
        return _StructuralFileExemption(
            family="dependency-lockfile",
            reason="well-known dependency lockfile is a machine-readable dependency artifact",
        )
    if labels & STRUCTURAL_GENERATED_LABELS or ("generated" in top_context and "do not edit" in top_context):
        return _StructuralFileExemption(
            family="generated",
            reason="tracked source is explicitly generated or generated-labeled",
        )
    if any(part in STRUCTURAL_FIXTURE_LABELS for part in parts):
        return _StructuralFileExemption(
            family="fixture",
            reason="tracked source is fixture/mock/snapshot-labeled",
        )
    if parts and parts[0] == "data":
        return _StructuralFileExemption(
            family="data",
            reason="tracked source is under the top-level data root",
        )
    if (
        (parts and parts[0] in STRUCTURAL_PROTOCOL_ROOTS)
        or path.suffix.lower() == ".proto"
        or parts[:2] == ("db", "migrations")
    ):
        return _StructuralFileExemption(
            family="protocol",
            reason="tracked source is a protocol/schema/contract artifact",
        )
    return None


def _structural_path_labels(relative_path: str) -> set[str]:
    return {label for label in re.split(r"[^a-z0-9]+", relative_path.lower()) if label}


def _structural_import_families(relative_path: str, text: str) -> tuple[str, ...]:
    suffix = Path(relative_path).suffix
    if suffix == ".py":
        return _structural_python_import_families(text)
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return _structural_ts_import_families(text)
    return ()


def _structural_bounded_import_families(
    relative_path: str,
    blob: _BoundedGitBlobText,
) -> tuple[str, ...]:
    suffix = Path(relative_path).suffix
    if suffix == ".py" and blob.truncated:
        added_lines = tuple(
            _StructuralAddedLine(line_number=None, text=line)
            for line in blob.text.splitlines()
        )
        return _structural_python_added_import_families(added_lines)
    return _structural_import_families(relative_path, blob.text)


def _structural_python_import_families(text: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    return _structural_python_import_families_from_tree(tree)


def _structural_python_import_families_from_tree(tree: ast.AST) -> tuple[str, ...]:
    families: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                families.add(_structural_import_family(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                families.add("relative")
            elif node.module:
                families.add(_structural_import_family(node.module))
    return tuple(sorted(family for family in families if family))


def _structural_ts_import_families(text: str) -> tuple[str, ...]:
    families: set[str] = set()
    ignored_spans = _structural_ts_ignored_spans(text)
    lines = tuple(_structural_line_spans(text))
    for line_start, line in lines:
        for pattern in (STRUCTURAL_TS_REQUIRE_PATTERN, STRUCTURAL_TS_DYNAMIC_IMPORT_PATTERN):
            for match in pattern.finditer(line):
                if _structural_ts_position_is_ignored(line_start + match.start(), ignored_spans):
                    continue
                families.add(_structural_import_family(match.group("module")))
    for block_start, block_text in _structural_ts_static_import_blocks(lines, ignored_spans):
        if _structural_ts_position_is_ignored(block_start, ignored_spans):
            continue
        module = _structural_ts_static_import_module(block_text)
        if module:
            families.add(_structural_import_family(module))
    return tuple(sorted(family for family in families if family))


def _structural_line_spans(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    position = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        lines.append((position, line))
        position += len(raw_line)
    return lines


def _structural_ts_static_import_blocks(
    lines: tuple[tuple[int, str], ...],
    ignored_spans: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, str], ...]:
    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line_start, line = lines[index]
        stripped = line.strip()
        if not _structural_ts_line_starts_static_import(stripped):
            index += 1
            continue
        import_column = line.find("import")
        import_position = line_start + max(import_column, 0)
        if _structural_ts_position_is_ignored(import_position, ignored_spans):
            index += 1
            continue
        block_lines = [line]
        if _structural_ts_static_import_module(line):
            blocks.append((import_position, line))
            index += 1
            continue
        if not _structural_ts_static_import_can_continue(stripped):
            index += 1
            continue
        block_end = min(len(lines), index + STRUCTURAL_TS_STATIC_IMPORT_MAX_BLOCK_LINES)
        scan_index = index + 1
        while scan_index < block_end:
            _, next_line = lines[scan_index]
            block_lines.append(next_line)
            block_text = "\n".join(block_lines)
            if _structural_ts_static_import_module(block_text):
                blocks.append((import_position, block_text))
                break
            if next_line.rstrip().endswith(";"):
                break
            scan_index += 1
        index += 1
    return tuple(blocks)


def _structural_ts_line_starts_static_import(stripped: str) -> bool:
    if not stripped.startswith("import"):
        return False
    next_char = stripped[6:7]
    if next_char and not next_char.isspace() and next_char not in {"'", '"'}:
        return False
    return not stripped.startswith("import(")


def _structural_ts_static_import_can_continue(stripped: str) -> bool:
    return any(token in stripped for token in ("{", "}", "*", ",", " from ", "\tfrom\t"))


def _structural_ts_static_import_module(block_text: str) -> str | None:
    bare_match = STRUCTURAL_TS_STATIC_BARE_IMPORT_PATTERN.match(block_text)
    if bare_match:
        return bare_match.group("module")
    from_match = STRUCTURAL_TS_STATIC_FROM_IMPORT_PATTERN.search(block_text)
    if from_match:
        return from_match.group("module")
    return None


def _structural_ts_ignored_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        next_char = text[index + 1] if index + 1 < length else ""
        if char == "/" and next_char == "/":
            start = index
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            spans.append((start, index))
            continue
        if char == "/" and next_char == "*":
            start = index
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            spans.append((start, index))
            continue
        if char == "/" and next_char not in {"/", "*"}:
            regex_end = _structural_ts_regex_literal_end(text, index)
            if regex_end is not None:
                spans.append((index, regex_end))
                index = regex_end
                continue
        if char in {"'", '"', "`"}:
            quote = char
            start = index
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            spans.append((start, index))
            continue
        index += 1
    return tuple(spans)


def _structural_ts_regex_literal_end(text: str, start: int) -> int | None:
    if not _structural_ts_regex_literal_allowed_at(text, start):
        return None
    index = start + 1
    length = len(text)
    in_character_class = False
    saw_pattern_char = False
    while index < length:
        char = text[index]
        if char in "\r\n":
            return None
        if char == "\\":
            index += 2
            saw_pattern_char = True
            continue
        if char == "[":
            in_character_class = True
            saw_pattern_char = True
            index += 1
            continue
        if char == "]" and in_character_class:
            in_character_class = False
            index += 1
            continue
        if char == "/" and not in_character_class:
            if not saw_pattern_char:
                return None
            index += 1
            while index < length and (text[index].isalpha() or text[index].isdigit() or text[index] in {"_", "$"}):
                index += 1
            return index
        saw_pattern_char = True
        index += 1
    return None


def _structural_ts_regex_literal_allowed_at(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    if index < 0:
        return True
    previous = text[index]
    if previous in "({[=,:;!&|?+-*~^<>":
        return True
    if previous == "}":
        return False
    word_end = index + 1
    while index >= 0 and (text[index].isalpha() or text[index] in {"_", "$"}):
        index -= 1
    previous_word = text[index + 1 : word_end]
    return previous_word in {
        "await",
        "case",
        "delete",
        "instanceof",
        "new",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }


def _structural_ts_position_is_ignored(
    position: int,
    ignored_spans: tuple[tuple[int, int], ...],
) -> bool:
    span_index = bisect_right(ignored_spans, (position, sys.maxsize)) - 1
    if span_index < 0:
        return False
    start, end = ignored_spans[span_index]
    return start <= position < end


def _structural_brace_index(
    text: str,
    ignored_spans: tuple[tuple[int, int], ...],
) -> tuple[dict[int, tuple[int, int]], tuple[int, ...]]:
    ranges: dict[int, tuple[int, int]] = {}
    open_braces: list[int] = []
    stack: list[int] = []
    index = 0
    span_index = 0
    while index < len(text):
        while span_index < len(ignored_spans) and ignored_spans[span_index][1] <= index:
            span_index += 1
        if span_index < len(ignored_spans):
            span_start, span_end = ignored_spans[span_index]
            if span_start <= index < span_end:
                index = span_end
                continue
        char = text[index]
        if char == "{":
            open_braces.append(index)
            stack.append(index)
        elif char == "}" and stack:
            open_brace_index = stack.pop()
            ranges[open_brace_index] = (open_brace_index + 1, index)
        index += 1
    return ranges, tuple(open_braces)


def _structural_added_import_families(
    relative_path: str,
    added_lines: tuple[_StructuralAddedLine, ...],
) -> tuple[str, ...]:
    suffix = Path(relative_path).suffix
    if suffix == ".py":
        return _structural_python_added_import_families(added_lines)
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return _structural_ts_import_families("\n".join(line.text for line in added_lines))
    return ()


def _structural_python_added_import_families(
    added_lines: tuple[_StructuralAddedLine, ...],
) -> tuple[str, ...]:
    families: set[str] = set()
    index = 0
    while index < len(added_lines):
        stripped = added_lines[index].text.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            index += 1
            continue
        block_lines = [added_lines[index].text]
        paren_balance = _structural_python_import_paren_balance(added_lines[index].text)
        continued = stripped.endswith("\\") or paren_balance > 0
        index += 1
        while continued and index < len(added_lines):
            block_lines.append(added_lines[index].text)
            paren_balance += _structural_python_import_paren_balance(added_lines[index].text)
            continued = added_lines[index].text.rstrip().endswith("\\") or paren_balance > 0
            index += 1
        families.update(_structural_python_import_block_families(block_lines))
    return tuple(sorted(family for family in families if family))


def _structural_python_import_paren_balance(text: str) -> int:
    return text.count("(") - text.count(")")


def _structural_python_import_block_families(lines: list[str]) -> tuple[str, ...]:
    source = textwrap.dedent("\n".join(lines)).strip()
    if not source:
        return ()
    try:
        parsed = ast.parse(source)
    except SyntaxError:
        return ()
    families: set[str] = set()
    for node in parsed.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                families.add(_structural_import_family(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                families.add("relative")
            elif node.module:
                families.add(_structural_import_family(node.module))
    return tuple(sorted(family for family in families if family))


def _structural_current_import_families(root: Path, relative_path: str) -> tuple[str, ...] | None:
    text = _read_structural_analysis_text(root / relative_path)
    if text is None:
        return None
    suffix = Path(relative_path).suffix
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        return _structural_python_import_families_from_tree(tree)
    return _structural_import_families(relative_path, text)


def _structural_import_family(module: str) -> str:
    normalized = module.strip()
    if not normalized:
        return ""
    if normalized.startswith((".", "/")):
        return "relative"
    if normalized.startswith("@"):
        parts = [part for part in normalized.split("/") if part]
        family = "/".join(parts[:2]) if len(parts) >= 2 else normalized
        return _structural_bounded_token_component(family, kind="import")
    path_parts = [part for part in re.split(r"[./]+", normalized) if part]
    if not path_parts:
        return ""
    if path_parts[0] in {"apps", "services", "workers", "packages"} and len(path_parts) >= 2:
        return _structural_bounded_token_component(f"{path_parts[0]}/{path_parts[1]}", kind="import")
    return _structural_bounded_token_component(path_parts[0], kind="import")


def _structural_bounded_token_component(value: str, *, kind: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip())
    if not normalized or len(normalized) <= STRUCTURAL_TOKEN_COMPONENT_MAX_CHARS:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[
        :STRUCTURAL_TOKEN_HASH_HEX_CHARS
    ]
    return f"{kind}-sha256-{digest}"
