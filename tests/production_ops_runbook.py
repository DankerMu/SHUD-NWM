"""The production-ops runbook surface set (non-collectible support module).

#1103 split ``docs/runbooks/current-production-ops.md`` into an index landing
page plus the ``docs/runbooks/production-ops/`` sub-runbooks. Every static
guard that used to ``read_text`` the monolith has to scan the whole tree now: a
guard left reading only the index finds no commands at all, its ``remaining ==
[]`` holds vacuously and it goes green on exactly the drift it exists to catch.
That silent weakening is the failure mode the split was required not to
introduce, so the surface set lives in one owner instead of being re-derived by
each reader.

The filename deliberately does not start with ``test_``: pytest must not
collect it, and ``scripts/select_ci_tests.py`` routes it through
``SUPPORT_MODULE_TEST_RULES`` rather than the ``tests/**`` suite branch.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = "docs/runbooks/current-production-ops.md"
SUBRUNBOOK_DIR = "docs/runbooks/production-ops"

# Index links are relative to `docs/runbooks/`: `](production-ops/<name>.md)`
# for the navigation table and `](production-ops/<name>.md#<anchor>)` for the
# per-section stubs.
_INDEX_LINK = re.compile(r"\]\(production-ops/([A-Za-z0-9._-]+\.md)[)#]")


def read_surface(relative: str) -> str:
    """Read one repo-relative text surface."""
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def subrunbook_names() -> tuple[str, ...]:
    """Sub-runbook filenames, pinned by index/tree mutual agreement.

    Fail-closed on both halves of the split. An emptied or renamed directory
    can never silently shrink a caller's scan back to the index alone, a
    sub-runbook nothing links to is invisible to an operator following the
    landing page, and an index link with no file behind it is a dead pointer.
    """
    on_disk = {path.name for path in (REPO_ROOT / SUBRUNBOOK_DIR).glob("*.md")}
    linked = set(_INDEX_LINK.findall(read_surface(INDEX_PATH)))
    assert on_disk, f"{SUBRUNBOOK_DIR}: no sub-runbook found"
    assert on_disk == linked, (
        f"index and {SUBRUNBOOK_DIR} disagree: {sorted(on_disk ^ linked)}"
    )
    return tuple(sorted(on_disk))


def surfaces() -> tuple[str, ...]:
    """Index page first, then every sub-runbook, as repo-relative POSIX paths."""
    return (INDEX_PATH, *(f"{SUBRUNBOOK_DIR}/{name}" for name in subrunbook_names()))


def combined_text() -> str:
    """Every production-ops surface joined, for whole-document content pins."""
    return "\n".join(read_surface(relative) for relative in surfaces())


def subrunbook_text(name: str) -> str:
    """One sub-runbook by filename; an unknown name fails closed."""
    assert name in subrunbook_names(), f"unknown sub-runbook: {name}"
    return read_surface(f"{SUBRUNBOOK_DIR}/{name}")
