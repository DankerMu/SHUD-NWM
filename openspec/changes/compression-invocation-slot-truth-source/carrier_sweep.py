"""Carrier sweep for the compression-invocation-slot-truth-source change (tasks.md E6).

Mechanizes the regression half of E6: no carrier of this change may *assert* a
formulation that design D2 has archived as refuted.  Three properties matter, and each
one is a defect this sweep's hand-run predecessors actually shipped:

* The refuted set is **derived** from D2's archive region, never maintained here, so
  falsifying a sixth draft does not require editing this file.
* Matching is **whitespace-normalized over the whole file and case-insensitive**, because
  a line-oriented grep cannot see a phrase wrapped across two lines and a case-sensitive
  one cannot see a re-cased opening word -- between them that is how the ``Wrong #4``
  quote in the test module escaped round 3's sweep.
* Exemptions are detected **by position**, not by an allowlist of names, because every
  enumerated allowlist this item has carried went stale within one delta.

What this does NOT do: prove the carriers are true.  It pins archived strings.  A
paraphrase that says the same false thing in different words (``design.md``'s
"is never closure-checked" against the archived "not closure-checked at all") passes
this sweep and is caught only by a reviewer reading it.  E6 states that split explicitly.

Usage: ``uv run python openspec/changes/compression-invocation-slot-truth-source/carrier_sweep.py``
Exit 0 means every hit is a classified citation; nonzero lists the assertions.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CHANGE_DIR = Path(__file__).resolve().parent
REPO = CHANGE_DIR.parents[2]
BASE = "master"

# The PR body is a carrier but is not a tracked file; it is authored in the scratchpad and
# posted with --body-file.  Passed as argv[1] when available, else skipped with a notice.
DEFAULT_BODY_HINT = "pass the PR body file as argv[1] to include it in the sweep"


def normalize(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space; return the flat text and a char->line map."""
    out: list[str] = []
    lines: list[int] = []
    line = 1
    pending_space = False
    for ch in text:
        if ch.isspace():
            if ch == "\n":
                line += 1
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            lines.append(line)
            pending_space = False
        out.append(ch)
        lines.append(line)
    return "".join(out), lines


def line_span(text: str, start_marker: str, end_marker: str) -> tuple[int, int]:
    """1-based line range [start, end) delimited by two literal markers."""
    lines = text.splitlines()
    start = end = None
    for i, ln in enumerate(lines, 1):
        if start is None and start_marker in ln:
            start = i
        elif start is not None and end is None and end_marker in ln:
            end = i
            break
    if start is None:
        raise SystemExit(f"carrier_sweep: marker {start_marker!r} not found; D2 was restructured")
    return start, end if end is not None else len(lines) + 1


def refuted_phrases(design: str) -> list[str]:
    """Derive the refuted set from D2's archive region -- the single source of truth."""
    flat, _ = normalize(design)
    start = flat.index("**Wrong #1")
    end = flat.index("So the canonical sentence,")
    archive = flat[start:end]
    quoted = re.findall(r'[“"]([^“”"]{4,160})[”"]', archive)
    bracketed = re.findall(r"「([^」]{4,60})」", archive)
    phrases = set()
    for raw in quoted + bracketed:
        cleaned = raw.strip(" .*…")
        # Drop schema key tuples such as ``path, sha256, bytes`` -- not prose claims.
        if not cleaned or cleaned.startswith("path"):
            continue
        # CJK carries a claim in far fewer characters than English does.
        if len(cleaned) >= 8 or re.search(r"[\u4e00-\u9fff]", cleaned):
            phrases.add(cleaned)
    return sorted(phrases)


def docstring_lines(source: str) -> set[int]:
    """1-based line numbers that sit inside a triple-quoted string literal."""
    inside: set[int] = set()
    open_delim: str | None = None
    for i, ln in enumerate(source.splitlines(), 1):
        rest = ln
        if open_delim is not None:
            inside.add(i)
            if open_delim in rest:
                open_delim = None
            continue
        for delim in ('"""', "'''"):
            first = rest.find(delim)
            if first == -1:
                continue
            if rest.find(delim, first + 3) == -1:
                open_delim = delim
                inside.add(i)
            break
    return inside


def main() -> int:
    design_path = CHANGE_DIR / "design.md"
    design = design_path.read_text()
    phrases = refuted_phrases(design)

    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{BASE}...HEAD"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    carriers = [REPO / f for f in changed if (REPO / f).is_file()]
    body = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if body and body.is_file():
        carriers.append(body)

    # Structural exemption regions, all position-detected.
    d2_start, d2_end = line_span(design, "**Wrong #1", "So the canonical sentence,")
    exempt: dict[Path, object] = {design_path: range(d2_start, d2_end)}

    proposal_path = CHANGE_DIR / "proposal.md"
    prop = proposal_path.read_text().splitlines()
    exempt[proposal_path] = {
        i for i, ln in enumerate(prop, 1) if "formulations were refuted" in ln
    }

    tasks_path = CHANGE_DIR / "tasks.md"
    tasks = tasks_path.read_text().splitlines()
    exempt[tasks_path] = {
        i for i, ln in enumerate(tasks, 1) if re.match(r"- \[[ x]\] E6 ", ln)
    }

    if body and body.is_file():
        body_lines = body.read_text().splitlines()
        span: set[int] = set()
        collecting = False
        for i, ln in enumerate(body_lines, 1):
            if ln.startswith("## ") and "canonical" in ln:
                collecting = True
                continue
            if collecting and ln.startswith("## "):
                break
            if collecting:
                span.add(i)
        exempt[body] = span

    findings = 0
    print(f"carrier_sweep: {len(phrases)} refuted phrase(s) derived from design D2 archive")
    for p in phrases:
        print(f"  - {p}")
    print(f"carrier_sweep: {len(carriers)} carrier(s) in scope ({BASE}...HEAD"
          f"{' + PR body' if body and body.is_file() else '; ' + DEFAULT_BODY_HINT})")
    for path in carriers:
        try:
            raw = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        flat, linemap = normalize(raw)
        docs = docstring_lines(raw) if path.suffix == ".py" else None
        for phrase in phrases:
            for match in re.finditer(re.escape(phrase), flat, re.IGNORECASE):
                line = linemap[match.start()]
                allowed = exempt.get(path)
                verdict = None
                if isinstance(allowed, range) and line in allowed:
                    verdict = "cited in D2 archive region"
                elif isinstance(allowed, set) and line in allowed:
                    verdict = "cited in refutation paragraph / E6 item"
                elif docs is not None and line in docs:
                    verdict = "cited in a docstring"
                rel = path if not str(path).startswith(str(REPO)) else path.relative_to(REPO)
                if verdict is None:
                    findings += 1
                    print(f"  FINDING {rel}:{line} asserts {phrase!r}")
                else:
                    print(f"  ok      {rel}:{line} {verdict}")
    print(f"carrier_sweep: {findings} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
