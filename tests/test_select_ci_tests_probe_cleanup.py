"""Selector coverage for disposable-probe ownership/cleanup suites (#1892/#1900).

``packages/common/compressed_chunk_cold_probe/**`` must select both the original
probe contract and the sibling cleanup suite. Core-smoke and the #1656 invariant
rider remain additive.
"""

from __future__ import annotations

from pathlib import Path

from scripts.select_ci_tests import CORE_SMOKE_TESTS, select_tests

INVARIANT_SUITE_PATH = "tests/test_timescale_write_guard_wire_site_invariant.py"
ORIGINAL_PROBE_SUITE = "tests/test_probe_compressed_chunk_cold_tablespace.py"
CLEANUP_SUITE = "tests/test_probe_compressed_chunk_cold_tablespace_cleanup.py"
OWNERSHIP_SOURCES = (
    "packages/common/compressed_chunk_cold_probe/cluster.py",
    "packages/common/compressed_chunk_cold_probe/runner.py",
    "packages/common/compressed_chunk_cold_probe/types.py",
)


def test_select_tests_maps_probe_support_ownership_modules_to_cleanup_suite() -> None:
    for source in OWNERSHIP_SOURCES:
        selected = set(select_tests([source], repo_root=Path(".")))
        assert ORIGINAL_PROBE_SUITE in selected, f"{source} did not select the original probe suite"
        assert CLEANUP_SUITE in selected, f"{source} did not select the cleanup suite"
        assert set(CORE_SMOKE_TESTS) <= selected
        assert INVARIANT_SUITE_PATH in selected
