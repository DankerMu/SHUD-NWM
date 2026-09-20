"""Shared node-22 entrypoint-invariant helpers (non-collectible support module).

#1103 partitioned the 1003-line ``tests/test_node22_entrypoint_invariant.py``
into ``tests/test_node22_entrypoint_invariant.py`` (governed surfaces) and
``tests/test_node22_entrypoint_invariant_python_scan.py`` (the substituted-python
classifier). This is that monolith's module prefix (line 9-16: the repo root,
the node-22/node-27 root constants and the surface reader), moved here verbatim
so both partitions import one owner instead of copying it.

The filename deliberately does not start with ``test_``: pytest must not
collect it, and ``scripts/select_ci_tests.py`` routes it through
``SUPPORT_MODULE_TEST_RULES`` rather than the ``tests/**`` suite branch.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE22_ACTIVE = "/scratch/frd_muziyao/NWM"
NODE22_VENV_PY = f"{NODE22_ACTIVE}/.venv/bin/python"
NODE27_PY = "/home/nwm/NWM"


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")
