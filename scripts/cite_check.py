#!/usr/bin/env python3
"""Resolve ``<path>:<line>`` citations in markdown and print what they point at.

Usage::

    uv run python scripts/cite_check.py docs/adr/0009-....md openspec/changes/<c>/design.md

For every citation of the form ``<path>:<line>`` or ``<path>:<line>-<line>``
found in the given markdown files (including inside backticks), the receipt
prints the resolved repository path, the line spec, and the source text the
citation currently lands on.  Abbreviated citations -- the ones prose commonly
writes, e.g. ``scheduler_config/config.py:792`` instead of the full
``services/orchestrator/scheduler_config/config.py:792`` -- are resolved by
matching the cited path as a trailing path-suffix of the tracked tree
(``git ls-files``).  A unique suffix match resolves; zero matches are reported
as ``UNRESOLVED PATH``; two or more as ``AMBIGUOUS PATH`` with the candidates.

The exit status is non-zero when any citation is UNRESOLVED, AMBIGUOUS or
PAST EOF, so the receipt can be regenerated and gated after the citing change
directory is archived.

What this tool CANNOT do -- stated so the receipt is not over-read:

* It cannot decide whether a citation that resolves points at the RIGHT
  content.  A line number that has drifted onto some other plausible line of
  the same file resolves cleanly here and is still a rotten citation.  Judging
  "does this line support the sentence citing it" stays a human read of the
  receipt.
* It does not resolve bare continuation anchors (``:806``, ``:652-661``) that
  name no path and inherit one from the surrounding prose.  Only the
  ``<path>:<line>`` form defined above is extracted.
* Suffix resolution answers "which tracked file does this name?", not "which
  tracked file did the author mean".  An AMBIGUOUS verdict means the prose has
  to spell the path out, not that the tool failed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A cited path is a slash-joined run of path characters whose final component
# carries a file extension, immediately followed by ``:<line>`` or
# ``:<line>-<line>``.  The lookbehind pins the match to a token boundary so the
# full ``a/b/config.py:1`` wins over the inner ``config.py:1``.
CITATION_RE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.+\-]+/)*[\w+\-][\w.+\-]*\.[A-Za-z][0-9A-Za-z]{0,7})"
    r":(\d+)(?:-(\d+))?"
    r"(?!\d)"
)

# How much of a cited source line to echo before eliding.
SNIPPET_WIDTH = 100
# How many candidates to name on an AMBIGUOUS row before summarising the rest.
MAX_CANDIDATES_SHOWN = 3
# Column the source text starts at, when the citation label is short enough.
LABEL_WIDTH = 76

RANGE_JOIN = " …→ "


def tracked_files() -> list[str]:
    """Return every tracked path, repo-relative, as ``git ls-files`` sees it."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [entry for entry in completed.stdout.decode("utf-8").split("\0") if entry]


def extract_citations(text: str) -> list[tuple[str, int, int | None]]:
    """Extract ``(path, start, end)`` citations, deduped, in first-seen order."""
    seen: set[tuple[str, int, int | None]] = set()
    citations: list[tuple[str, int, int | None]] = []
    for match in CITATION_RE.finditer(text):
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) is not None else None
        citation = (path, start, end)
        if citation in seen:
            continue
        seen.add(citation)
        citations.append(citation)
    return citations


def resolve_path(cited: str, tracked: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a cited path to one tracked path, or report the candidate set.

    Returns ``(resolved, candidates)``: ``resolved`` is set only when the
    citation is repo-relative or matches exactly one tracked path-suffix.
    """
    if (REPO_ROOT / cited).is_file():
        return cited, [cited]
    suffix = "/" + cited
    candidates = [entry for entry in tracked if entry == cited or entry.endswith(suffix)]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def read_lines(path: str) -> list[str]:
    """Split on ``\\n`` only -- ``splitlines()`` also breaks on `\\x0c`/`\\u2028`,
    which would desynchronise the receipt from every editor's line numbering.
    A trailing newline is not a line, so the empty tail element is dropped."""
    raw = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def snippet(line: str) -> str:
    line = line.rstrip("\r")
    if len(line) > SNIPPET_WIDTH:
        return line[:SNIPPET_WIDTH] + "…"
    return line


def line_spec(start: int, end: int | None) -> str:
    return f"{start}-{end}" if end is not None else str(start)


def format_row(label: str, body: str) -> str:
    padding = " " * max(2, LABEL_WIDTH - len(label))
    return f"  {label}{padding}{body}"


def describe(cited: str, start: int, end: int | None, tracked: list[str]) -> tuple[str, str]:
    """Return ``(status, row)`` for one citation."""
    spec = line_spec(start, end)
    resolved, candidates = resolve_path(cited, tracked)
    if resolved is None:
        if not candidates:
            return "UNRESOLVED", format_row(f"UNRESOLVED PATH  {cited}:{spec}", "no tracked path ends with this suffix")
        shown = ", ".join(candidates[:MAX_CANDIDATES_SHOWN])
        extra = len(candidates) - MAX_CANDIDATES_SHOWN
        if extra > 0:
            shown += f" (+{extra} more)"
        return "AMBIGUOUS", format_row(f"AMBIGUOUS PATH  {cited}:{spec}", f"{len(candidates)} candidates: {shown}")

    lines = read_lines(resolved)
    last_wanted = end if end is not None else start
    # Show the expansion when the prose cited an abbreviated path, so the
    # receipt itself records which suffix match was taken.
    label = f"{resolved}:{spec}" if resolved == cited else f"{cited}:{spec} => {resolved}"
    if start < 1 or last_wanted > len(lines):
        return "PAST_EOF", format_row(f"PAST EOF  {label}", f"file has {len(lines)} lines")

    body = snippet(lines[start - 1])
    if end is not None:
        body += RANGE_JOIN + snippet(lines[end - 1])
    return "OK", format_row(label, body)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: cite_check.py <markdown> [<markdown> ...]", file=sys.stderr)
        return 2

    tracked = tracked_files()
    counts = {"OK": 0, "UNRESOLVED": 0, "AMBIGUOUS": 0, "PAST_EOF": 0}
    out: list[str] = [
        "# cite-check receipt — generated by `scripts/cite_check.py`; do not hand-edit.",
        "# Re-run: uv run python scripts/cite_check.py " + " ".join(argv),
    ]

    for arg in argv:
        source = Path(arg)
        if not source.is_absolute():
            source = REPO_ROOT / arg
        if not source.is_file():
            print(f"cite_check: no such markdown file: {arg}", file=sys.stderr)
            return 2
        out.append("")
        out.append(f"########## {arg}")
        for cited, start, end in extract_citations(source.read_text(encoding="utf-8", errors="replace")):
            status, row = describe(cited, start, end, tracked)
            counts[status] += 1
            out.append(row)

    total = sum(counts.values())
    hard = counts["UNRESOLVED"] + counts["AMBIGUOUS"] + counts["PAST_EOF"]
    out.append("")
    out.append(
        f"==== citations: {total} | resolved: {counts['OK']} | unresolved: {counts['UNRESOLVED']}"
        f" | ambiguous: {counts['AMBIGUOUS']} | past EOF: {counts['PAST_EOF']} | hard failures: {hard}"
    )
    print("\n".join(out))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
