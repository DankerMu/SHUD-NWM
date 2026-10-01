"""The hard line-number reference check (#2648).

A comment or docstring that points at a source position by file name plus line
number rots silently: the target file is edited, the number keeps pointing at
whatever now sits there, and nothing fails. This family scans the comments
(``tokenize``) and docstrings (``ast``) of shipped Python under
``LINE_REFERENCE_SCAN_ROOTS`` for two shapes -- a ``.py`` file name followed by a
colon and digits, and a backtick immediately followed by a colon and digits --
and compares each file's per-reference count with a frozen baseline.

The baseline is keyed by (path, reference text), never by line, so moving a
frozen reference does not fail and deleting one never fails. Only a count above
the baseline does. String literals and code are not scanned, and neither is
markdown. The patterns live in ``LINE_REFERENCE_PATTERNS`` rather than being
spelled out here, which keeps this docstring itself out of its own scan."""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

from scripts.governance.entropy_audit.repo_files import (
    _iter_python_files,
    _module_for_path,
    _read_repo_text,
    _rel,
)
from scripts.governance.entropy_audit.schema import FindingSpec

LINE_REFERENCE_CHECK_ID = "hard-line-reference"
LINE_REFERENCE_SCAN_ROOTS = ("apps", "packages", "services", "workers", "scripts")
LINE_REFERENCE_BASELINE_PATH = "scripts/governance/entropy_audit/line_reference_baseline.json"
LINE_REFERENCE_PATTERNS = (
    re.compile(r"\b[\w./-]+\.py:\d+(?:[-–]\d+)?"),
    re.compile(r"`:\d+"),
)


def _check_line_references(root: Path) -> list[FindingSpec]:
    baseline = _load_line_reference_baseline(root)
    findings: list[FindingSpec] = []
    for path, references in sorted(_scan_line_references(root).items()):
        allowed = baseline.get(path, {})
        by_text: dict[str, list[int]] = {}
        for line, text in references:
            by_text.setdefault(text, []).append(line)
        for text, lines in sorted(by_text.items()):
            frozen = allowed.get(text, 0)
            # A text-keyed baseline cannot tell which occurrence is the new
            # one, so an over-count names every occurrence line.
            occurrences = tuple(lines) if frozen else ()
            for line in lines[frozen:]:
                findings.append(_line_reference_finding(root, path, line, text, occurrences))
    return findings


def _line_reference_finding(
    root: Path, path: str, line: int, text: str, occurrences: tuple[int, ...] = ()
) -> FindingSpec:
    where = ""
    if occurrences:
        where = (
            f" ({len(occurrences)} occurrences on lines {', '.join(map(str, occurrences))}; "
            "the baseline freezes fewer, and any of them may be the new one)"
        )
    return FindingSpec(
        check_id=LINE_REFERENCE_CHECK_ID,
        title="New hard line-number reference in a comment or docstring",
        axis="context",
        governance_face="comment drift",
        role="shared_contract",
        evidence_path=path,
        line=line,
        severity="medium",
        priority="P2",
        owner_area="code-comments",
        module=_module_for_path(root, root / path),
        description=(
            f"new hard line reference {text} in {path}{where}; replace it with a symbol reference "
            "(function/class/constant name)"
        ),
        recommendation="Name the function, class or constant that owns the behaviour instead of a line number.",
    )


def _load_line_reference_baseline(root: Path) -> dict[str, dict[str, int]]:
    path = root / LINE_REFERENCE_BASELINE_PATH
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(refs, dict) and all(isinstance(count, int) and count > 0 for count in refs.values())
        for refs in data.values()
    ):
        raise ValueError(f"{LINE_REFERENCE_BASELINE_PATH}: expected {{path: {{reference: positive count}}}}")
    return data


def _scan_line_references(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Map each scanned repo-relative path to its (line, reference text) hits."""

    result: dict[str, list[tuple[int, str]]] = {}
    roots = [root / name for name in LINE_REFERENCE_SCAN_ROOTS]
    for path in _iter_python_files(root, roots):
        text = _read_repo_text(root, path)
        # Cheap whole-file prefilter: most files carry no candidate at all, and
        # tokenizing every file in the scan roots dominates the audit's runtime.
        if not any(pattern.search(text) for pattern in LINE_REFERENCE_PATTERNS):
            continue
        hits = _line_references_in_source(text)
        if hits:
            result[_rel(root, path)] = hits
    return result


def _line_references_in_source(source: str) -> list[tuple[int, str]]:
    """(line, reference text) for every hit in a comment or a docstring."""

    try:
        docstring_starts = _docstring_starts(ast.parse(source))
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return []
    hits: list[tuple[int, str]] = []
    for token in tokens:
        if token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING and token.start in docstring_starts
        ):
            hits.extend(_line_references_in_text(token.string, token.start[0]))
    return hits


def _docstring_starts(tree: ast.AST) -> set[tuple[int, int]]:
    starts: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            starts.add((body[0].value.lineno, body[0].value.col_offset))
    return starts


def _line_references_in_text(text: str, first_line: int) -> list[tuple[int, str]]:
    hits: list[tuple[int, int, str]] = []
    for pattern in LINE_REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            hits.append((match.start(), first_line + text.count("\n", 0, match.start()), match.group(0)))
    return [(line, ref) for _offset, line, ref in sorted(hits)]


def _line_reference_counts(root: Path) -> dict[str, dict[str, int]]:
    """The baseline shape for the current tree: path -> {reference: count}."""

    return {
        path: dict(sorted(Counter(text for _line, text in hits).items()))
        for path, hits in sorted(_scan_line_references(root).items())
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=f"freeze the current references into {LINE_REFERENCE_BASELINE_PATH}",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    counts = _line_reference_counts(args.root.resolve())
    if args.write_baseline:
        (args.root / LINE_REFERENCE_BASELINE_PATH).write_text(
            json.dumps(counts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    total = sum(sum(refs.values()) for refs in counts.values())
    print(json.dumps({"files": len(counts), "references": total}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
