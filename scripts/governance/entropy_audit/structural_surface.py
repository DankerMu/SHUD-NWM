"""Public-surface token extraction for the structural ownership-growth signal.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Derives the
comparable public surface of a file: Python top-level public names, TypeScript
exports (named, default, class methods, CJS object exports) and the route
entrypoint/owner tokens the growth signal reports on."""

from __future__ import annotations

import ast
import hashlib
import re
from bisect import bisect_left
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    STRUCTURAL_CJS_EXPORTS_ASSIGNMENT_PATTERN,
    STRUCTURAL_CJS_MODULE_EXPORTS_DEFAULT_PATTERN,
    STRUCTURAL_CJS_MODULE_EXPORTS_OBJECT_START_PATTERN,
    STRUCTURAL_COMPATIBILITY_PATTERN,
    STRUCTURAL_IMPORT_FAMILY_MIXED_COUNT,
    STRUCTURAL_PARSER_VALIDATOR_PATTERN,
    STRUCTURAL_PUBLIC_TS_PATTERN,
    STRUCTURAL_PYTHON_DECLARATION_PATTERN,
    STRUCTURAL_ROUTE_ENTRYPOINT_PATTERN,
    STRUCTURAL_ROUTE_OWNER_ASSIGNMENT_PATTERN,
    STRUCTURAL_TOKEN_HASH_HEX_CHARS,
    STRUCTURAL_TS_DEFAULT_EXPORT_PATTERN,
    STRUCTURAL_TS_EXPORTED_CLASS_PATTERN,
    STRUCTURAL_TS_NAMED_EXPORT_PATTERN,
)
from scripts.governance.entropy_audit.schema import _BoundedGitBlobText
from scripts.governance.entropy_audit.structural_sources import (
    _read_structural_analysis_text,
    _structural_brace_index,
    _structural_line_spans,
    _structural_ts_ignored_spans,
    _structural_ts_position_is_ignored,
)


def _structural_bounded_public_surface_tokens(
    relative_path: str,
    blob: _BoundedGitBlobText,
) -> tuple[str, ...]:
    return _structural_public_surface_tokens(
        relative_path,
        blob.text,
        python_partial=blob.truncated,
    )


def _structural_current_public_surface_tokens(
    root: Path,
    relative_path: str,
) -> tuple[str, ...] | None:
    text = _read_structural_analysis_text(root / relative_path)
    if text is None:
        return None
    return _structural_public_surface_tokens(relative_path, text)


def _structural_route_path_token(path: str) -> str:
    normalized = path.strip()
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[
        :STRUCTURAL_TOKEN_HASH_HEX_CHARS
    ]
    return f"path-sha256-{digest}"


def _structural_bounded_detail_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()[
        :STRUCTURAL_TOKEN_HASH_HEX_CHARS
    ]
    return f"public-sha256-{digest}"


def _structural_ownership_surface_signals(
    relative_path: str,
    text: str,
    import_families: tuple[str, ...],
) -> tuple[str, ...]:
    signals: list[str] = []
    if len(import_families) >= STRUCTURAL_IMPORT_FAMILY_MIXED_COUNT:
        signals.append("many-import-families")
    if _structural_public_surface_tokens(relative_path, text):
        signals.append("public-entrypoint-surface")
    if STRUCTURAL_COMPATIBILITY_PATTERN.search(text):
        signals.append("compatibility-surface")
    if STRUCTURAL_PARSER_VALIDATOR_PATTERN.search(text):
        signals.append("parser-validator-responsibility")
    return tuple(signals)


def _structural_public_entrypoint_names(
    relative_path: str,
    text: str,
    *,
    python_partial: bool = False,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _structural_public_surface_label(token)
            for token in _structural_public_surface_tokens(
                relative_path,
                text,
                python_partial=python_partial,
            )
        )
    )


def _structural_public_surface_label(token: str) -> str:
    return token.split(":", 1)[1] if ":" in token else token


def _structural_public_surface_tokens(
    relative_path: str,
    text: str,
    *,
    python_partial: bool = False,
) -> tuple[str, ...]:
    suffix = Path(relative_path).suffix
    tokens: set[str] = set()
    if suffix == ".py":
        tokens.update(_structural_python_public_surface_tokens(text, partial=python_partial))
    elif suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        tokens.update(_structural_ts_public_surface_tokens(text))
    tokens.update(_structural_route_entrypoint_tokens(text))
    return tuple(sorted(token for token in tokens if token))


def _structural_python_public_surface_tokens(text: str, *, partial: bool) -> tuple[str, ...]:
    if partial:
        return _structural_python_partial_public_surface_tokens(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _structural_python_partial_public_surface_tokens(text)
    tokens: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if _structural_python_public_name(node.name):
                tokens.add(f"function:{node.name}")
            continue
        if isinstance(node, ast.ClassDef):
            if not _structural_python_public_name(node.name):
                continue
            tokens.add(f"class:{node.name}")
            for member in node.body:
                if isinstance(member, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
                    _structural_python_public_name(member.name)
                ):
                    tokens.add(f"method:{node.name}.{member.name}")
    return tuple(sorted(tokens))


def _structural_python_partial_public_surface_tokens(text: str) -> tuple[str, ...]:
    tokens: set[str] = set()
    contexts: list[tuple[str, int, str, bool]] = []
    for line in text.splitlines():
        match = STRUCTURAL_PYTHON_DECLARATION_PATTERN.match(line)
        if match is None:
            continue
        indent = len(match.group("indent").expandtabs(4))
        while contexts and contexts[-1][1] >= indent:
            contexts.pop()
        name = match.group("name")
        if match.group("class"):
            public_top_level_class = indent == 0 and _structural_python_public_name(name)
            if public_top_level_class:
                tokens.add(f"class:{name}")
            contexts.append(("class", indent, name, public_top_level_class))
            continue
        if indent == 0:
            if _structural_python_public_name(name):
                tokens.add(f"function:{name}")
        elif contexts and contexts[-1][0] == "class":
            class_name = contexts[-1][2]
            public_top_level_class = contexts[-1][3]
            if public_top_level_class and _structural_python_public_name(name):
                tokens.add(f"method:{class_name}.{name}")
        contexts.append(("function", indent, name, False))
    return tuple(sorted(tokens))


def _structural_python_public_name(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _structural_ts_public_surface_tokens(text: str) -> tuple[str, ...]:
    tokens: set[str] = set()
    ignored_spans = _structural_ts_ignored_spans(text)
    brace_ranges, open_braces = _structural_brace_index(text, ignored_spans)
    for match in STRUCTURAL_PUBLIC_TS_PATTERN.finditer(text):
        if _structural_ts_position_is_ignored(match.start(), ignored_spans):
            continue
        if match.group("default"):
            tokens.add("export:default")
        else:
            tokens.add(f"export:{match.group('name')}")
    for match in STRUCTURAL_TS_NAMED_EXPORT_PATTERN.finditer(text):
        if _structural_ts_position_is_ignored(match.start(), ignored_spans):
            continue
        tokens.update(_structural_ts_named_export_tokens(match.group("body")))
    for match in STRUCTURAL_TS_DEFAULT_EXPORT_PATTERN.finditer(text):
        if not _structural_ts_position_is_ignored(match.start(), ignored_spans):
            tokens.add("export:default")
    for match in STRUCTURAL_CJS_EXPORTS_ASSIGNMENT_PATTERN.finditer(text):
        if not _structural_ts_position_is_ignored(match.start(), ignored_spans):
            tokens.add(f"export:{match.group('name')}")
    for body in _structural_cjs_module_exports_object_bodies(text, ignored_spans, brace_ranges):
        tokens.update(_structural_cjs_object_export_tokens(body))
    for match in STRUCTURAL_CJS_MODULE_EXPORTS_DEFAULT_PATTERN.finditer(text):
        if not _structural_ts_position_is_ignored(match.start(), ignored_spans):
            tokens.add("export:default")
    tokens.update(
        _structural_ts_exported_class_method_tokens(
            text,
            ignored_spans,
            brace_ranges,
            open_braces,
        )
    )
    return tuple(sorted(token for token in tokens if token))


def _structural_route_entrypoint_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    route_owner_names = _structural_route_owner_names(text)
    for match in STRUCTURAL_ROUTE_ENTRYPOINT_PATTERN.finditer(text):
        owner = match.group("owner")
        if not _structural_route_owner_name_is_allowed(owner, route_owner_names):
            continue
        method = match.group("method").lower()
        tokens.add(f"route:{method}:{_structural_route_path_token(match.group('path'))}")
    return tokens


def _structural_route_owner_names(text: str) -> set[str]:
    return {
        match.group("name")
        for match in STRUCTURAL_ROUTE_OWNER_ASSIGNMENT_PATTERN.finditer(text)
    } | {"app", "router"}


def _structural_route_owner_name_is_allowed(owner: str, route_owner_names: set[str]) -> bool:
    return owner in route_owner_names or owner.endswith(("_router", "_app"))


def _structural_ts_exported_class_method_tokens(
    text: str,
    ignored_spans: tuple[tuple[int, int], ...],
    brace_ranges: dict[int, tuple[int, int]],
    open_braces: tuple[int, ...],
) -> set[str]:
    tokens: set[str] = set()
    for match in STRUCTURAL_TS_EXPORTED_CLASS_PATTERN.finditer(text):
        if _structural_ts_position_is_ignored(match.start(), ignored_spans):
            continue
        class_name = match.group("name")
        body_start = _structural_next_open_brace(open_braces, match.end())
        if body_start is None:
            continue
        body_range = brace_ranges.get(body_start)
        if body_range is None:
            continue
        body = text[body_range[0] : body_range[1]]
        for method_name in _structural_ts_class_method_names(body):
            tokens.add(f"method:{class_name}.{method_name}")
    return tokens


def _structural_next_open_brace(open_braces: tuple[int, ...], start: int) -> int | None:
    index = bisect_left(open_braces, start)
    if index >= len(open_braces):
        return None
    return open_braces[index]


def _structural_ts_class_method_names(body: str) -> set[str]:
    ignored_spans = _structural_ts_ignored_spans(body)
    names: set[str] = set()
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    span_index = 0
    for line_start, line in _structural_line_spans(body):
        if not (brace_depth or bracket_depth or paren_depth):
            method_match = _structural_ts_class_method_line_match(line)
            if method_match is not None:
                names.add(method_match.group("name"))
        line_end = line_start + len(line)
        index = line_start
        while index < line_end:
            while span_index < len(ignored_spans) and ignored_spans[span_index][1] <= index:
                span_index += 1
            if span_index < len(ignored_spans):
                span_start, span_end = ignored_spans[span_index]
                if span_start <= index < span_end:
                    index = min(span_end, line_end)
                    continue
            char = body[index]
            if char == "{":
                brace_depth += 1
            elif char == "}" and brace_depth:
                brace_depth -= 1
            elif char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth:
                bracket_depth -= 1
            elif char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth:
                paren_depth -= 1
            index += 1
    return names


def _structural_ts_class_method_line_match(line: str) -> re.Match[str] | None:
    match = re.match(
        r"^\s*"
        r"(?P<modifiers>(?:(?:public|protected|private|static|async|override|abstract|declare)\s+)*)"
        r"(?:get\s+|set\s+)?"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
        r"\s*(?:<[^>{}\n]*>)?\s*\(",
        line,
    )
    if match is None:
        return None
    modifiers = set(match.group("modifiers").split())
    if modifiers & {"private", "protected"}:
        return None
    if match.group("name") in {"constructor", "if", "for", "while", "switch", "catch", "function"}:
        return None
    return match


def _structural_ts_named_export_tokens(body: str) -> set[str]:
    tokens: set[str] = set()
    identifier = r"[A-Za-z_$][A-Za-z0-9_$]*"
    for item in body.split(","):
        normalized = item.strip()
        if not normalized:
            continue
        alias_match = re.fullmatch(
            rf"(?:type\s+)?{identifier}\s+as\s+(?P<exported>{identifier}|default)",
            normalized,
        )
        if alias_match:
            exported = alias_match.group("exported")
        else:
            name_match = re.fullmatch(rf"(?:type\s+)?(?P<exported>{identifier}|default)", normalized)
            if name_match is None:
                continue
            exported = name_match.group("exported")
        tokens.add("export:default" if exported == "default" else f"export:{exported}")
    return tokens


def _structural_cjs_module_exports_object_bodies(
    text: str,
    ignored_spans: tuple[tuple[int, int], ...],
    brace_ranges: dict[int, tuple[int, int]],
) -> tuple[str, ...]:
    bodies: list[str] = []
    for match in STRUCTURAL_CJS_MODULE_EXPORTS_OBJECT_START_PATTERN.finditer(text):
        if _structural_ts_position_is_ignored(match.start(), ignored_spans):
            continue
        body_range = brace_ranges.get(match.end() - 1)
        if body_range is not None:
            bodies.append(text[body_range[0] : body_range[1]])
    return tuple(bodies)


def _structural_cjs_object_export_tokens(body: str) -> set[str]:
    tokens: set[str] = set()
    identifier = r"[A-Za-z_$][A-Za-z0-9_$]*"
    for item in _structural_split_top_level_object_items(body):
        normalized = item.strip()
        if not normalized or normalized.startswith("..."):
            continue
        quoted_match = re.match(r"(?P<quote>['\"])(?P<name>[^'\"]+)(?P=quote)[ \t]*:", normalized)
        if quoted_match:
            name = quoted_match.group("name")
        else:
            name_match = re.match(rf"(?P<name>{identifier})\b(?:[ \t]*[:(=]|$)", normalized)
            if name_match is None:
                continue
            name = name_match.group("name")
        if name == "default":
            tokens.add("export:default")
        elif re.fullmatch(identifier, name):
            tokens.add(f"export:{name}")
    return tokens


def _structural_split_top_level_object_items(body: str) -> tuple[str, ...]:
    ignored_spans = _structural_ts_ignored_spans(body)
    items: list[str] = []
    item_start = 0
    index = 0
    span_index = 0
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    while index < len(body):
        while span_index < len(ignored_spans) and ignored_spans[span_index][1] <= index:
            span_index += 1
        if span_index < len(ignored_spans):
            span_start, span_end = ignored_spans[span_index]
            if span_start <= index < span_end:
                index = span_end
                continue
        char = body[index]
        if char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
        elif char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth:
            paren_depth -= 1
        elif char == "," and not (brace_depth or bracket_depth or paren_depth):
            items.append(body[item_start:index])
            item_start = index + 1
        index += 1
    items.append(body[item_start:])
    return tuple(items)
