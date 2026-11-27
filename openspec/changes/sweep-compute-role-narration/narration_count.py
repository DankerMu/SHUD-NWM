"""Count the lines that still carry the compute-role codename (#3345).

Every PR of the sweep quotes this output before and after its diff.

A line is filed under the strongest class any of its occurrences has:

- bare: the codename stands alone in running text (narration, or a statement
  about the retired host);
- quoted: the codename stands alone inside code font;
- name: the codename is part of a longer token (file, unit, env key, schema id,
  path) and is not touched by the sweep.

History is listed in one row and is not swept. Two more counts cover the stale
text that does not contain the codename: literal ssh lines to the retired host
and the bare "22" shorthand.

Usage (repository root): python -I <this file> [--tokens] [--files]
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

OWN_DIR = "openspec/changes/sweep-compute-role-narration/"
HISTORY = (
    "openspec/changes/archive/",
    "docs/plans/",
    "docs/runbooks/receipts/",
    "docs/adr/",
    "docs/bugs.md",
    "docs/review-loop-log.jsonl",
    "docs/stage-pipeline-log.jsonl",
    "docs/governance/entropy-audit-2026-10-16.",
    "progress.md",
)
AREAS = (
    "docs/runbooks/",
    "docs/",
    "openspec/specs/",
    "openspec/",
    "instructions/",
    "apps/",
    "packages/",
    "services/",
    "workers/",
    "scripts/",
    "infra/",
    "tests/",
    "schemas/",
    "db/",
)
CODENAME = re.compile(r"node-?22", re.IGNORECASE)
TOKEN_CHARS = re.compile(r"[\w./@#-]")
HOST_LIST = re.compile(r"(?:node-?2[27]|w[23])(?:/(?:node-?2[27]|w[23]))*", re.IGNORECASE)
SSH_RETIRED_HOST = re.compile(r"ssh\b.*210\.77\.77\.22")
SHORTHAND = re.compile(r"(?<![\w.:-])22 ?(?:节点|侧|compute-control)")
RANK = {"name": 0, "quoted": 1, "bare": 2}


def occurrences(line: str):
    """Yield (class, token) for every codename occurrence in the line."""
    for match in CODENAME.finditer(line):
        start, end = match.span()
        while start > 0 and TOKEN_CHARS.match(line[start - 1]):
            start -= 1
        while end < len(line) and TOKEN_CHARS.match(line[end]):
            end += 1
        token = line[start:end].strip("./-#@")
        if not HOST_LIST.fullmatch(token):
            yield "name", token
        elif line[max(start - 1, 0) : start] == "`" and line[end : end + 1] == "`":
            yield "quoted", token
        else:
            yield "bare", token


def area_of(path: str) -> str:
    if path.endswith("AGENTS.md") or path == "CLAUDE.md":
        return "instructions/"
    return next((area for area in AREAS if path.startswith(area)), "(root and other)")


def main(argv: list[str]) -> int:
    listed = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True, text=True).stdout
    lines: Counter[tuple[str, str]] = Counter()
    tokens: Counter[str] = Counter()
    bare_files: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    history = 0
    for path in filter(None, listed.split("\0")):
        if path.startswith(OWN_DIR):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        in_history = path.startswith(HISTORY)
        for line in text.splitlines():
            found = list(occurrences(line))
            if in_history:
                history += bool(found)
                continue
            extra["ssh to the retired host"] += bool(SSH_RETIRED_HOST.search(line))
            extra["bare 22 shorthand"] += bool(SHORTHAND.search(line))
            if not found:
                continue
            strongest = max((cls for cls, _ in found), key=RANK.__getitem__)
            lines[area_of(path), strongest] += 1
            bare_files[path] += strongest == "bare"
            tokens.update(token for cls, token in found if cls == "name")

    classes = ("bare", "quoted", "name")
    print(f"{'area':<20}" + "".join(f"{cls:>8}" for cls in classes) + f"{'total':>8}")
    for area in (*AREAS, "(root and other)"):
        row = [lines[area, cls] for cls in classes]
        if sum(row):
            print(f"{area:<20}" + "".join(f"{n:>8}" for n in row) + f"{sum(row):>8}")
    totals = [sum(n for (_, cls), n in lines.items() if cls == each) for each in classes]
    print(f"{'TOTAL':<20}" + "".join(f"{n:>8}" for n in totals) + f"{sum(totals):>8}")
    print(f"history (not swept): {history} lines")
    for label in ("ssh to the retired host", "bare 22 shorthand"):
        print(f"{label}: {extra[label]} lines")
    if "--files" in argv:
        print("\nbare lines per file")
        for path, count in sorted(bare_files.items(), key=lambda item: (-item[1], item[0])):
            if count:
                print(f"{count:>6}  {path}")
    if "--tokens" in argv:
        print("\nname tokens")
        for token, count in sorted(tokens.items(), key=lambda item: (-item[1], item[0])):
            print(f"{count:>6}  {token}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
