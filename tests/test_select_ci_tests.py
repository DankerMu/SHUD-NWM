from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath

import pytest

from scripts.select_ci_tests import (
    CHAIN_IMPORTER_TESTS,
    CHANGED_TEST_FILE_RULES,
    CHANGED_TEST_SUITE_BASENAME_PATTERNS,
    CORE_SMOKE_TESTS,
    DIRECT_GRID_CONTRACT_IMPORTER_TESTS,
    DIRECT_GRID_CONTRACT_TESTS,
    DIRECT_GRID_E2E_TESTS,
    DIRECT_GRID_SURFACE_TESTS,
    FILE_JOURNAL_READ_STATE_TESTS,
    FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS,
    ORCHESTRATOR_CLI_IMPORTER_TESTS,
    ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
    PATH_TEST_RULES,
    SCHEDULER_IMPORTER_TESTS,
    SELECTOR_META_GUARD_TEST,
    SUPPORT_MODULE_TEST_RULES,
    PathTestRule,
    is_test_suite_path,
    main,
    select_tests,
)


def test_select_tests_includes_changed_test_file(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_example.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_example(): pass\n", encoding="utf-8")

    assert select_tests(["tests/test_example.py"], repo_root=tmp_path) == ["tests/test_example.py"]


def test_select_tests_maps_adapter_changes_to_adapter_tests() -> None:
    selected = select_tests(
        ["workers/data_adapters/gfs_adapter.py", "workers/data_adapters/cycle_hours.py"],
        repo_root=Path("."),
    )

    assert "tests/test_gfs_adapter.py" in selected
    assert "tests/test_ifs_adapter.py" in selected
    assert "tests/test_data_adapter_resolution.py" in selected
    assert "tests/test_production_scheduler.py" in selected


def test_select_tests_maps_runtime_changes_to_runtime_contract_tests() -> None:
    # The three contract suites are the original pin. The four additions are
    # #1455's narrow `workers/shud_runtime/runtime.py` rule: every one is a
    # non-gated top-level importer of runtime.py that no rule reached before.
    selected = select_tests(["workers/shud_runtime/runtime.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_direct_grid_e2e.py",
        "tests/test_e2e.py",
        "tests/test_runtime_ic_header.py",
        "tests/test_runtime_mode.py",
        "tests/test_shud_runtime.py",
        "tests/test_warm_start.py",
        "tests/test_warm_start_chaining.py",
    ]


def test_select_tests_maps_direct_grid_producer_surface_to_compact_e2e_fixture() -> None:
    selected = select_tests(["workers/forcing_producer/direct_grid_contract.py"], repo_root=Path("."))

    # The compact fixture is still the core of the selection; #1455 appended the
    # module's five other non-gated top-level importer suites AT THIS RULE SITE
    # (the stop rule makes `workers/forcing_producer/**` unreachable here, and
    # DIRECT_GRID_SURFACE_TESTS itself must not move — the openspec-change rule
    # below shares it). The redirect intent is unchanged: the whole
    # tests/test_forcing_producer.py never comes back.
    assert selected == sorted({*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS})
    assert list(DIRECT_GRID_E2E_TESTS) == ["tests/test_direct_grid_e2e.py"]
    assert all(
        target.startswith("tests/test_forcing_producer.py::test_direct_grid_contract_")
        for target in DIRECT_GRID_CONTRACT_TESTS
    )
    assert "tests/test_forcing_producer.py" not in selected


def test_select_tests_maps_direct_grid_openspec_change_to_compact_e2e_fixture() -> None:
    selected = select_tests(
        ["openspec/changes/direct-grid-forcing/specs/direct-grid-forcing-production/spec.md"],
        repo_root=Path("."),
    )

    assert selected == sorted(DIRECT_GRID_SURFACE_TESTS)


def test_select_tests_keeps_issue_548_direct_grid_change_set_bounded() -> None:
    selected = select_tests(
        [
            "workers/forcing_producer/direct_grid_contract.py",
            "openspec/changes/direct-grid-forcing/proposal.md",
            "openspec/changes/direct-grid-forcing/design.md",
            "openspec/changes/direct-grid-forcing/specs/direct-grid-forcing-production/spec.md",
        ],
        repo_root=Path("."),
    )

    # Still bounded, just by a bigger constant: the compact e2e fixture plus the
    # five #1455 importer suites (all seconds-scale), and no core-smoke blowout.
    assert selected == sorted({*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS})
    assert len(selected) == 1 + len(DIRECT_GRID_CONTRACT_TESTS) + len(DIRECT_GRID_CONTRACT_IMPORTER_TESTS)
    assert "tests/test_forcing_producer.py" not in selected
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_maps_orchestrator_chain_types_to_manifest_surface_nodes() -> None:
    selected = select_tests(["services/orchestrator/chain_types.py"], repo_root=Path("."))

    assert selected == sorted(ORCHESTRATOR_MANIFEST_SURFACE_TESTS)
    assert "tests/test_orchestration_chain.py" not in selected
    assert "tests/test_orchestrator.py" not in selected
    assert "tests/test_scheduler_backfill.py" not in selected
    assert "tests/test_warm_start_chaining.py" not in selected


def test_select_tests_maps_orchestrator_manifest_surface_without_whole_slow_suites() -> None:
    selected = select_tests(["services/orchestrator/chain_manifests.py"], repo_root=Path("."))

    assert selected == sorted(ORCHESTRATOR_MANIFEST_SURFACE_TESTS)
    assert all("::" in test_path for test_path in selected)


def test_select_tests_maps_scheduler_facade_to_manifest_and_file_journal_surfaces() -> None:
    selected = select_tests(["services/orchestrator/scheduler.py"], repo_root=Path("."))

    assert set(FILE_JOURNAL_READ_STATE_TESTS) <= set(selected)
    assert set(ORCHESTRATOR_MANIFEST_SURFACE_TESTS) <= set(selected)
    assert "tests/test_file_orchestration_journal.py" in selected
    assert "tests/test_file_orchestration_migration.py" in selected
    assert "tests/test_orchestration_chain.py" not in selected
    assert "tests/test_production_scheduler.py" not in selected


def test_select_tests_maps_file_journal_read_state_without_whole_legacy_suites() -> None:
    selected = select_tests(
        [
            "packages/common/safe_fs.py",
            "services/orchestrator/file_orchestration_journal.py",
            "services/orchestrator/scheduler_runtime.py",
            "tests/test_production_scheduler.py",
        ],
        repo_root=Path("."),
    )

    # The changed test file adds the selector meta-guards (#1254); the redirect
    # itself is untouched — no whole legacy suite comes back. The two extra
    # file-level targets are #1455's at-site extension of the
    # file_orchestration_journal.py rule (its own read-cache suite and the
    # backfill suite that drives it), which is additive to the redirect rather
    # than a widening of it.
    assert selected == sorted(
        {
            *FILE_JOURNAL_READ_STATE_TESTS,
            *FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS,
            "tests/test_select_ci_tests.py",
        }
    )
    assert "tests/test_orchestration_chain.py" not in selected
    assert "tests/test_production_scheduler.py" not in selected


def test_select_tests_maps_known_slow_manifest_test_file_changes_with_surface_changes_to_focused_nodes() -> None:
    selected = select_tests(
        ["services/orchestrator/chain_types.py", "tests/test_orchestration_chain.py"],
        repo_root=Path("."),
    )

    # Focused nodes plus the selector meta-guards (#1254). The redirect intent —
    # never the whole slow suite — survives: the meta-guard suite costs ~6s.
    assert selected == sorted({*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, "tests/test_select_ci_tests.py"})
    assert "tests/test_orchestration_chain.py" not in selected


def test_select_tests_keeps_standalone_changed_test_file_whole_file_selection() -> None:
    selected = select_tests(["tests/test_orchestration_chain.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_orchestration_chain.py",
        "tests/test_select_ci_tests.py",
    ]


def test_select_tests_keeps_broad_orchestrator_fallback_for_other_orchestrator_changes() -> None:
    # What this pins is the ROUTING: an orchestrator module no narrow or stop
    # rule owns falls through to the broad `services/orchestrator/**` rule and
    # gets exactly that rule's targets — nothing more, and no core-smoke
    # fallback. The list grew from 5 to 28 in #1455 and stays FROZEN here as a
    # literal: reading it back from the rule under test would make the size
    # dimension self-referential, and size is exactly what matters on the widest
    # PR class in the tree. Growing the rule means consciously editing this list
    # and recording the new lane wall-clock (design risk 1: the 35-min Unit
    # Tests cap). The five original members are asserted separately so a silent
    # removal reds even if someone re-freezes carelessly.
    selected = select_tests(["services/orchestrator/retry.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_cli_publish_qdown.py",
        "tests/test_e2e_m3.py",
        "tests/test_file_orchestration_journal.py",
        "tests/test_file_orchestration_journal_read_cache.py",
        "tests/test_file_orchestration_migration.py",
        "tests/test_live_monitoring.py",
        "tests/test_monitoring_api.py",
        "tests/test_orchestration_chain.py",
        "tests/test_orchestrator.py",
        "tests/test_pipeline_persistence.py",
        "tests/test_production_scheduler.py",
        "tests/test_publish_scheduler_file_registry.py",
        "tests/test_reconcile_sacct_parse.py",
        "tests/test_replay_lineage.py",
        "tests/test_retention.py",
        "tests/test_retry.py",
        "tests/test_retry_cancel_consistency.py",
        "tests/test_run_tree_copyback.py",
        "tests/test_scheduler_backfill.py",
        "tests/test_scheduler_backfill_predecessor.py",
        "tests/test_scheduler_file_provider_refresh.py",
        "tests/test_scheduler_generation.py",
        "tests/test_scheduler_timing.py",
        "tests/test_source_cycle_raw_manifest.py",
        "tests/test_source_scoped_dispatch.py",
        "tests/test_state_clone.py",
        "tests/test_variant_activation_cutover.py",
        "tests/test_warm_start_chaining.py",
    ]
    assert {
        "tests/test_orchestration_chain.py",
        "tests/test_orchestrator.py",
        "tests/test_production_scheduler.py",
        "tests/test_scheduler_backfill.py",
        "tests/test_warm_start_chaining.py",
    } <= set(selected)


def test_select_tests_maps_compute_compose_to_two_node_runtime_tests() -> None:
    selected = select_tests(["infra/compose.compute.yml"], repo_root=Path("."))

    assert selected == ["tests/test_two_node_docker_runtime.py"]


def test_select_tests_maps_forecast_store_without_core_smoke_fallback() -> None:
    selected = select_tests(["packages/common/forecast_store.py"], repo_root=Path("."))
    fallback_only_tests = set(CORE_SMOKE_TESTS) - {"tests/test_migrations.py"}

    assert selected == [
        "tests/test_forecast_api.py",
        "tests/test_list_search_contract.py",
        "tests/test_migrations.py",
        "tests/test_model_registry_list_basins.py",
        "tests/test_qhh_latest_fallback_pushdown.py",
    ]
    assert not fallback_only_tests & set(selected)


def test_select_tests_maps_mvt_tiles_without_core_smoke_fallback() -> None:
    selected = select_tests(["services/tiles/mvt.py"], repo_root=Path("."))
    fallback_only_tests = set(CORE_SMOKE_TESTS) - {"tests/test_migrations.py"}

    assert selected == [
        "tests/test_api_contract.py",
        "tests/test_display_publish_status_only.py",
        "tests/test_migrations.py",
        "tests/test_openapi_drift.py",
    ]
    assert not fallback_only_tests & set(selected)


def test_select_tests_maps_autopipeline_script_without_core_smoke_fallback() -> None:
    # scripts/node27_autopipeline.py has no same-name tests/test_node27_autopipeline.py,
    # so before its explicit rule it dropped into the core-smoke fallback and none
    # of its own suites (preflight, handoff, publish status-only) ran on a PR that
    # changed it.
    assert not Path("tests/test_node27_autopipeline.py").exists()

    selected = select_tests(["scripts/node27_autopipeline.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_display_publish_status_only.py",
        "tests/test_node27_autopipeline_handoff.py",
        "tests/test_node27_autopipeline_preflight.py",
    ]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_maps_autopipe_cron_wrapper_without_core_smoke_fallback() -> None:
    # scripts/node27_autopipe_cron.sh is a shell script, so it is not a backend
    # python path and the core-smoke fallback never arms for it. Before its
    # explicit rule a wrapper-only PR selected nothing at all and CI degraded to
    # --collect-only, even though the wrapper is covered by real assertions in
    # tests/test_node27_autopipeline_preflight.py.
    assert Path("scripts/node27_autopipe_cron.sh").exists()

    selected = select_tests(["scripts/node27_autopipe_cron.sh"], repo_root=Path("."))

    assert selected == ["tests/test_node27_autopipeline_preflight.py"]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_maps_governance_entropy_scripts_without_core_smoke_fallback() -> None:
    selected = select_tests(
        [
            "scripts/governance/audit_repo_entropy.py",
            "scripts/governance/write_entropy_baseline.py",
        ],
        repo_root=Path("."),
    )

    assert selected == ["tests/test_entropy_audit_script.py"]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_maps_script_to_its_same_name_suite_without_core_smoke_fallback() -> None:
    selected = select_tests(
        ["scripts/scheduler_state_index_copyback_replay.py"],
        repo_root=Path("."),
    )

    assert "tests/test_scheduler_state_index_copyback_replay.py" in selected
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_maps_subdirectory_script_to_same_name_suite_by_basename(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_nested_helper_probe.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_nested_helper_probe(): pass\n", encoding="utf-8")

    selected = select_tests(["scripts/nested/sub/nested_helper_probe.py"], repo_root=tmp_path)

    assert selected == ["tests/test_nested_helper_probe.py"]


def test_select_tests_keeps_core_smoke_fallback_for_script_without_same_name_suite() -> None:
    selected = select_tests(["scripts/no_such_helper_xyz.py"], repo_root=Path("."))

    assert not Path("tests/test_no_such_helper_xyz.py").exists()
    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def test_select_tests_keeps_explicit_differently_named_script_rule() -> None:
    selected = select_tests(["scripts/validate_readonly_db_boundary.py"], repo_root=Path("."))

    assert selected == ["tests/test_readonly_db_validation.py"]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_same_name_derivation_is_scoped_to_scripts_paths() -> None:
    # packages/common/state_qc.py has a same-name tests/test_state_qc.py, but the
    # derivation is scripts/-only: this path must keep today's fallback behavior.
    assert Path("tests/test_state_qc.py").is_file()

    selected = select_tests(["packages/common/state_qc.py"], repo_root=Path("."))

    assert "tests/test_state_qc.py" not in selected
    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def _tracked_script_same_name_pairs() -> list[tuple[str, str]]:
    listing = subprocess.run(
        ["git", "ls-files", "--", "scripts"],
        check=True,
        capture_output=True,
        text=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in listing.stdout.splitlines():
        script_path = line.strip()
        if not script_path.endswith(".py"):
            continue
        same_name_test = f"tests/test_{PurePosixPath(script_path).stem}.py"
        if Path(same_name_test).is_file():
            pairs.append((script_path, same_name_test))
    return pairs


def test_every_tracked_script_with_a_same_name_suite_selects_it_without_core_smoke() -> None:
    # Mechanized completeness guard: the pair list is derived from the tracked
    # tree, never frozen here, so a newly added script/test pair is covered the
    # moment it lands instead of silently falling into the core-smoke fallback.
    pairs = _tracked_script_same_name_pairs()
    assert pairs, "expected tracked scripts/<name>.py <-> tests/test_<name>.py pairs"

    offenders: list[str] = []
    for script_path, same_name_test in pairs:
        selected = select_tests([script_path], repo_root=Path("."))
        if same_name_test not in selected:
            offenders.append(f"{script_path}: {same_name_test} not selected (got {selected})")
            continue
        if same_name_test in CORE_SMOKE_TESTS:
            # Exemption: a same-name test that IS a core-smoke file makes the
            # no-smoke clause meaningless. No such pair exists today.
            continue
        smoke_overlap = sorted(set(CORE_SMOKE_TESTS) & set(selected))
        if smoke_overlap:
            offenders.append(f"{script_path}: still drags core smoke {smoke_overlap}")
    assert not offenders, "script/test same-name mapping incomplete: " + "; ".join(offenders)


CONTRACT_SOURCE_PATH = "packages/common/node27_container_contract.py"
CONTRACT_SNAPSHOT_FIXTURE_PATH = "packages/common/node27_external_contract_snapshot.json"
CONTRACT_MODULE = "packages.common.node27_container_contract"
CONTRACT_TRANSITIVE_ONLY_TEST = "tests/test_node27_timeseries_compression_live_evidence.py"

# Independent of the AST walk on purpose: a line-shaped reading of both import
# spellings, used only as a cross-derivation floor under the AST closure.
_CONTRACT_IMPORT_LINE = re.compile(
    r"^[ \t]*(?:"
    r"from[ \t]+packages\.common[ \t]+import[ \t]+[^\n]*\bnode27_container_contract\b"
    r"|from[ \t]+packages\.common\.node27_container_contract[ \t]+import\b"
    r"|import[ \t]+packages\.common\.node27_container_contract\b"
    r")",
    re.MULTILINE,
)


def _tracked_python_files(pathspec: str) -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in listing.stdout.splitlines() if line.strip().endswith(".py")]


def _import_from_base(path: str, node: ast.ImportFrom) -> str | None:
    """Dotted prefix an ``ImportFrom`` in ``path`` resolves against, or ``None``.

    Absolute imports keep their own module. Relative ones resolve against the
    importer's package, derived from the repo-relative POSIX path (as emitted by
    ``git ls-files``), not the process cwd: ``packages/common/x.py`` sits in
    ``packages.common``, ``level == 1`` means that package and each further
    level strips one more part. A level deeper than the path allows contributes
    nothing rather than raising — the walk runs over arbitrary tracked files.
    """
    if node.level == 0:
        return node.module or None
    package_parts = list(PurePosixPath(path).parent.parts)
    strip = node.level - 1
    if strip > len(package_parts):
        return None
    parts = package_parts[: len(package_parts) - strip]
    if node.module:
        parts.append(node.module)
    return ".".join(parts) or None


# One suite run re-parses the same ~500 files ~10^4 times (every guard rederives
# its own view of the tree). Memoizing collapses that to one parse per file.
# Released by tests/conftest.py's pytest_unconfigure: carrying the trees into
# interpreter shutdown costs seconds of CPython finalization (see the hook).
_PARSE_CACHE: dict[tuple[str, int, int], ast.Module] = {}


def _parse_tracked(path: str) -> ast.Module:
    """Parse ``path``, reusing an earlier parse of the same file identity.

    The key is the RESOLVED absolute path plus stat identity, never the
    caller-supplied spelling: tests chdir into ``tmp_path`` and parse
    repo-shaped relative paths, so a relative key would alias a fixture onto
    the repository file of the same name. ``mtime_ns`` + ``size`` make a
    rewrite a different key, so a file rewritten mid-run is re-parsed.

    Two boundaries this key deliberately does not discriminate: a rewrite
    keeping the resolved path, ``mtime_ns`` AND size identical (unreachable
    here — tracked files are never mutated mid-run and mtimes are
    nanosecond-grained), and the ``filename`` spelling of the first parse,
    which only shapes parse-time ``SyntaxError`` messages and leaves no trace
    on the returned tree.

    Cache hits hand back the same ``ast.Module``; every consumer only reads.
    """
    stat = os.stat(path)
    key = (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size)
    tree = _PARSE_CACHE.get(key)
    if tree is None:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        _PARSE_CACHE[key] = tree
    return tree


def _module_names_from_nodes(path: str, nodes: Iterable[ast.AST]) -> set[str]:
    """Dotted module names the import nodes of ``path`` can refer to.

    All spellings collapse to the same dotted name here:
    ``from packages.common import node27_container_contract`` via the
    module+alias join, ``from packages.common.node27_container_contract import
    X`` via the module itself, and the relative spellings via the same joins
    once resolved against the importer's package path (see
    ``_import_from_base``) — so an in-package importer stays visible.
    """
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(path, node)
            if base is None:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _imported_module_names(path: str) -> set[str]:
    """Every dotted module name imported anywhere in ``path``, nesting included."""
    return _module_names_from_nodes(path, ast.walk(_parse_tracked(path)))


def _top_level_imported_module_names(path: str, tree: ast.Module) -> set[str]:
    """Dotted module names imported by ``path``'s module-level statements only.

    Deliberately narrower than ``_imported_module_names``: a function-body
    import runs when that one test runs, not at collection, and does not make
    the file an importer suite of the module for selector-coverage purposes.
    """
    return _module_names_from_nodes(path, tree.body)


def _resolved_script_modules(names: set[str], tracked_scripts: set[str]) -> set[str]:
    """Map dotted ``scripts.*`` names onto the tracked script files they load."""
    resolved: set[str] = set()
    for name in names:
        parts = name.split(".")
        if parts[0] != "scripts":
            continue
        for end in range(2, len(parts) + 1):
            candidate = "/".join(parts[:end]) + ".py"
            if candidate in tracked_scripts:
                resolved.add(candidate)
    return resolved


def _contract_dependent_test_closure() -> set[str]:
    """Tracked test files that reach the container contract through imports.

    Direct importers (either spelling) plus tests importing a ``scripts/``
    module whose scripts-import graph reaches the contract. Transitivity is run
    to a FIXED POINT, not one hop: two-hop chains already exist today
    (bundle_author -> live_evidence, plan_author -> supervisor), so a future
    same-name suite for such a module must land in the closure too.
    """
    tracked_scripts = set(_tracked_python_files("scripts"))
    script_imports = {path: _imported_module_names(path) for path in tracked_scripts}

    reaching = {path for path, names in script_imports.items() if CONTRACT_MODULE in names}
    while True:
        grown = reaching | {
            path
            for path, names in script_imports.items()
            if _resolved_script_modules(names, tracked_scripts) & reaching
        }
        if grown == reaching:
            break
        reaching = grown

    closure: set[str] = set()
    for test_path in _tracked_python_files("tests"):
        if not PurePosixPath(test_path).name.startswith("test_"):
            continue
        names = _imported_module_names(test_path)
        if CONTRACT_MODULE in names or _resolved_script_modules(names, tracked_scripts) & reaching:
            closure.add(test_path)
    return closure


def _regex_direct_contract_importer_tests() -> set[str]:
    return {
        test_path
        for test_path in _tracked_python_files("tests")
        if _CONTRACT_IMPORT_LINE.search(Path(test_path).read_text(encoding="utf-8"))
    }


def test_container_contract_change_selects_its_derived_dependent_closure() -> None:
    # Mechanized like the same-name pair guard above: the dependent closure is
    # DERIVED from the tracked tree by import analysis, never frozen here, so a
    # newly added dependent suite reddens this test (pointing at the rule to
    # extend) instead of silently dropping into the core-smoke fallback.
    assert Path(CONTRACT_SOURCE_PATH).is_file()
    closure = _contract_dependent_test_closure()

    # Anti-vacuity floor 1: the transitive-only member, whose own text never
    # names the contract — a grep-shaped derivation cannot find it.
    assert CONTRACT_TRANSITIVE_ONLY_TEST in closure
    assert not _CONTRACT_IMPORT_LINE.search(Path(CONTRACT_TRANSITIVE_ONLY_TEST).read_text(encoding="utf-8"))
    # Anti-vacuity floor 2: cross-derivation. A degenerate AST walk fails loudly
    # without freezing the closure's cardinality here.
    regex_direct = _regex_direct_contract_importer_tests()
    assert regex_direct, "expected tracked tests importing the contract directly"
    assert regex_direct <= closure, f"AST closure missed direct importers: {sorted(regex_direct - closure)}"

    selected = select_tests([CONTRACT_SOURCE_PATH], repo_root=Path("."))

    missing = sorted(closure - set(selected))
    assert not missing, f"contract change does not select its dependent suites: {missing}"
    smoke_overlap = sorted(set(CORE_SMOKE_TESTS) & set(selected))
    assert not smoke_overlap, f"contract change still drags core smoke {smoke_overlap}"


def test_contract_snapshot_fixture_change_selects_its_snapshot_suite() -> None:
    # The committed fixture is the hermetic suite's ground truth, so a
    # fixture-only PR (the runbook's patch-version drift disposition) must still
    # select a suite that asserts; otherwise CI degrades to the collect-only
    # smoke and re-baselines drift with zero assertions.
    assert Path(CONTRACT_SNAPSHOT_FIXTURE_PATH).is_file()

    selected = select_tests([CONTRACT_SNAPSHOT_FIXTURE_PATH], repo_root=Path("."))

    assert selected == ["tests/test_node27_external_contract_snapshot.py"]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_container_contract_transitive_walk_stays_scoped_to_scripts() -> None:
    # The closure walk follows scripts/ imports only. That scoping is sound
    # while scripts/ holds every non-test importer of the contract; assert the
    # premise instead of assuming it, so a new packages/services/workers/apps
    # importer reddens here rather than quietly widening the blind spot.
    tracked = [
        path
        for pathspec in ("apps", "packages", "services", "workers")
        for path in _tracked_python_files(pathspec)
        if path != CONTRACT_SOURCE_PATH
    ]
    importers = sorted(path for path in tracked if CONTRACT_MODULE in _imported_module_names(path))

    assert not importers, f"contract importers outside scripts/ are not covered by the closure walk: {importers}"


def test_import_walk_resolves_relative_imports_against_importer_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both guards above read the tree through _imported_module_names, so a
    # relative in-package importer must reach the SAME dotted name as the
    # absolute spellings. Otherwise a future packages/common sibling using
    # `from .` is invisible to the closure AND to the scope guard: both stay
    # green while its suite silently falls back to core smoke.
    package_dir = tmp_path / "packages" / "common"
    package_dir.mkdir(parents=True)
    spellings = {
        "node27_recovery_helper.py": "from .node27_container_contract import RECOVERY_TARGET_CHUNK_NAME\n",
        "node27_recovery_probe.py": "from . import node27_container_contract\n",
        "node27_recovery_sibling.py": "from ..common.node27_container_contract import RECOVERY_TARGET_CHUNK_NAME\n",
    }
    for name, source in spellings.items():
        (package_dir / name).write_text(source, encoding="utf-8")
    # Malformed depth must contribute nothing rather than raise: the walk runs
    # over arbitrary tracked files, not only well-formed packages.
    (package_dir / "node27_recovery_overshoot.py").write_text("from ..... import whatever\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    for name in spellings:
        names = _imported_module_names(f"packages/common/{name}")
        assert CONTRACT_MODULE in names, f"{name}: relative import lost the contract module, got {sorted(names)}"
    assert _imported_module_names("packages/common/node27_recovery_overshoot.py") == set()


# An importer carrying one of these at FILE level contributes no assertions in
# the PR lane and is therefore not required of the owning rule. The PR lane runs
# bare `pytest -q <files>` with no `-m` expression (ci.yml, `unit-test-targeted`),
# so the skipping comes from tests/conftest.py:74-88: pytest_collection_modifyitems
# auto-skips `integration`, `e2e` and `grib` items unless the matching opt-in is
# set (NHMS_RUN_E2E / NHMS_RUN_GRIB, and the integration service config). `grib`
# is left out here only because no tracked file-level `pytestmark` uses it; add
# it the day one does. `real_disk` and `timescaledb_210` must NEVER be added:
# conftest declares them but does not auto-skip them, so such a suite really does
# run its assertions in the PR lane and the owning rule must keep selecting it.
# All of that is mechanically anchored to conftest by
# test_gating_marker_names_anchor_to_the_conftest_auto_skip_set (#1455): this set
# plus the recorded `grib` absence must EQUAL the AST-derived auto-skip set, so a
# conftest that starts or stops auto-skipping a marker forces a visible decision
# here instead of silently desynchronising the two.
GATING_MARKER_NAMES = frozenset({"integration", "e2e"})

# Deliberate absence from GATING_MARKER_NAMES: conftest auto-skips `grib`, but no
# tracked file carries it as a file-level `pytestmark`, so excluding grib-marked
# suites from an owning rule would today only lose coverage. Move it into
# GATING_MARKER_NAMES the day a file-level user appears.
DELIBERATELY_UNGATED_AUTO_SKIP_MARKERS = frozenset({"grib"})

# Registered in conftest.pytest_configure but NOT auto-skipped: suites carrying
# these really do run their assertions in the PR lane, so they must never leak
# into the exclusion set.
NEVER_AUTO_SKIPPED_MARKERS = frozenset({"real_disk", "timescaledb_210"})

CONFTEST_PATH = "tests/conftest.py"


def _conftest_auto_skip_markers(source: str) -> set[str]:
    """Marker names ``pytest_collection_modifyitems`` auto-skips, from its AST.

    ``source`` is the conftest TEXT, not a path, so red evidence can feed a
    modified copy in memory without touching the tracked file. The shape read
    here is conftest's own: ``if "<marker>" in item.keywords and <reason>``
    membership tests inside the hook — NOT ``get_closest_marker``. A derivation
    that finds nothing means the hook was rewritten into a shape this function
    no longer understands; that fails loudly rather than returning an empty set
    that would silently make every marker look non-gating.
    """
    tree = ast.parse(source, filename=CONFTEST_PATH)
    hook = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "pytest_collection_modifyitems"
        ),
        None,
    )
    assert hook is not None, "tests/conftest.py no longer defines pytest_collection_modifyitems"

    markers: set[str] = set()
    for node in ast.walk(hook):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.In):
            continue
        if not (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
            continue
        container = node.comparators[0]
        if isinstance(container, ast.Attribute) and container.attr == "keywords":
            markers.add(node.left.value)
    assert markers, (
        "derived no auto-skipped markers from pytest_collection_modifyitems: "
        'the hook no longer uses `"<marker>" in item.keywords` membership tests'
    )
    return markers


def test_gating_marker_names_anchor_to_the_conftest_auto_skip_set() -> None:
    # GATING_MARKER_NAMES decides which importer suites the guarded-module guard
    # is allowed to skip requiring. Its truth lives in tests/conftest.py, which
    # nothing forced it to track. An EQUALITY is the binding assertion here: a
    # difference/subset framing stays green if conftest STOPS auto-skipping
    # `e2e`, which would wrongly keep excluding suites that really run.
    derived = _conftest_auto_skip_markers(Path(CONFTEST_PATH).read_text(encoding="utf-8"))

    assert derived == GATING_MARKER_NAMES | DELIBERATELY_UNGATED_AUTO_SKIP_MARKERS, (
        f"conftest auto-skip set drifted: derived {sorted(derived)}, "
        f"recorded {sorted(GATING_MARKER_NAMES | DELIBERATELY_UNGATED_AUTO_SKIP_MARKERS)}"
    )
    assert not NEVER_AUTO_SKIPPED_MARKERS & derived, (
        f"markers wrongly derived as auto-skipped: {sorted(NEVER_AUTO_SKIPPED_MARKERS & derived)}"
    )
    assert not NEVER_AUTO_SKIPPED_MARKERS & GATING_MARKER_NAMES


def test_conftest_auto_skip_derivation_reads_membership_tests_and_fails_loudly() -> None:
    # The seam that makes the anchor falsifiable without touching the tracked
    # conftest: a dropped marker reds the equality, and a hook rewritten to a
    # shape this derivation cannot read fails loudly instead of returning an
    # empty set (which would silently disarm the anchor and the closure guard).
    source = Path(CONFTEST_PATH).read_text(encoding="utf-8")

    without_e2e = source.replace('if "e2e" in item.keywords and e2e_skip_reason:', "if e2e_skip_reason and False:")
    assert without_e2e != source
    assert _conftest_auto_skip_markers(without_e2e) == {"integration", "grib"}

    rewritten = source.replace(" in item.keywords", " in item.nodeid")
    assert rewritten != source
    with pytest.raises(AssertionError, match="derived no auto-skipped markers"):
        _conftest_auto_skip_markers(rewritten)

# (module source path, dotted module, a known member of the derived importer
# set). The third element is the anti-vacuity floor: a derivation that breaks
# into silence — bad pathspec, AST regression, marker filter gone wide — must
# fail loudly instead of passing on an empty set.
GUARDED_MODULE_CLOSURES: tuple[tuple[str, str, str], ...] = (
    (
        "packages/common/display_coverage.py",
        "packages.common.display_coverage",
        "tests/test_display_coverage_parallel.py",
    ),
    (
        "services/slurm_gateway/real_backend.py",
        "services.slurm_gateway.real_backend",
        "tests/test_real_slurm_gateway.py",
    ),
)

DISPLAY_COVERAGE_GATED_IMPORTER = "tests/test_display_coverage_residual_debt_integration.py"

# Anti-vacuity floor for the one-hop extension (#1455): this suite pins the
# sacct parsing constants that services/orchestrator/reconcile.py consumes, and
# reconcile.py is what imports real_backend at file level — the suite itself
# never names real_backend, so a direct-importer-only derivation cannot see it.
REAL_BACKEND_ONE_HOP_MEMBER = "tests/test_reconcile_sacct_parse.py"


def _tracked_top_level_test_files() -> list[str]:
    """Tracked files directly under `tests/` that pytest collects by name.

    Classification goes through `is_test_suite_path` — the selector's own
    predicate, both `python_files` patterns, matched on the basename — rather
    than a hand-rolled `tests/test_*.py` fnmatch. That path-shaped spelling was
    wrong twice (PR #1486): fnmatch's `*` crosses `/`, so it reads
    `tests/test_pkg/helper.py` as a suite, and its single pattern reads
    `tests/x_test.py` as a support module. Every derivation below — the
    importer index, the guarded-module closure, the disposition guard — inherits
    that classification, so it has to equal pytest's.

    "Top level" is enforced by the parent check, not by the pattern: `git
    ls-files` glob magic also crosses `/`, and the derivations here are about
    collectible top-level suites.
    """
    return [
        path
        for path in _tracked_python_files("tests")
        if PurePosixPath(path).parent == PurePosixPath("tests") and is_test_suite_path(path)
    ]


def _file_level_gating_markers(tree: ast.Module) -> set[str]:
    """Gating marker names a module-level ``pytestmark`` assignment applies.

    Read from the AST, not the file text: a `pytest.mark.integration` decorator
    on one function gates that function, not the file, and a substring scan
    cannot tell the two apart. Scalar, list and tuple ``pytestmark`` spellings
    all collapse here, and a `pytest.mark.X(...)` call contributes ``X``.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            continue
        for element in ast.walk(value):
            if (
                isinstance(element, ast.Attribute)
                and isinstance(element.value, ast.Attribute)
                and element.value.attr == "mark"
            ):
                names.add(element.attr)
    return names & GATING_MARKER_NAMES


def _non_gated_top_level_importer_tests(module: str) -> set[str]:
    """Tracked `tests/test_*.py` importing ``module`` at file level, un-gated."""
    importers: set[str] = set()
    for test_path in _tracked_top_level_test_files():
        tree = _parse_tracked(test_path)
        if module not in _top_level_imported_module_names(test_path, tree):
            continue
        if _file_level_gating_markers(tree):
            continue
        importers.add(test_path)
    return importers


def _dotted_module_name(path: str) -> str:
    """Dotted name a tracked `.py` path is imported under.

    A package's `__init__.py` is imported as the package itself, so the trailing
    component is stripped. Without that, a re-exporting package would be looked
    up under a name nothing ever imports and would silently contribute an empty
    importer set to the one-hop derivation — a guard that goes quiet rather than
    red. No tracked `__init__.py` re-exports a guarded module today; this closes
    the channel before one does.
    """
    dotted = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
    return dotted.removesuffix(".__init__")


def _tracked_non_test_modules() -> list[str]:
    """The one-hop derivation domain: every tracked `.py` outside `tests/**`.

    Pinned as the domain so the derivation is deterministic — not "whatever
    happens to be importable", not the process's sys.modules.
    """
    return [path for path in _tracked_python_files("*.py") if not path.startswith("tests/")]


def _one_hop_importer_modules(module: str, *, domain: Sequence[str] | None = None) -> set[str]:
    """Tracked non-test modules importing ``module`` at file level — ONE hop.

    Deliberately NOT recursive. The bound is forward-looking policy, not a
    measurement: today the top-level-edge fixed point already equals this set,
    so recursing buys nothing, while an unbounded walk is what turns one
    guarded module into a PR lane running most of the suite. ``domain`` is a
    parameter so the non-recursion property is testable on a fixture tree.

    Top-level edges only, at BOTH levels (module->module here, test->module in
    ``_non_gated_top_level_importer_tests``): a function-body import runs when
    that one function runs, not at collection, so it never contributes — which
    is exactly why the function-body exclusion pin below stays green.
    """
    paths = _tracked_non_test_modules() if domain is None else list(domain)
    importers: set[str] = set()
    for path in paths:
        if _dotted_module_name(path) == module:
            continue
        if module in _top_level_imported_module_names(path, _parse_tracked(path)):
            importers.add(path)
    return importers


def _one_hop_importer_tests(module: str, *, domain: Sequence[str] | None = None) -> set[str]:
    """Non-gated importer suites contributed by ``module``'s one-hop importers."""
    contributed: set[str] = set()
    for hop_path in _one_hop_importer_modules(module, domain=domain):
        contributed |= _non_gated_top_level_importer_tests(_dotted_module_name(hop_path))
    return contributed


def test_dotted_module_name_maps_a_package_init_to_the_package() -> None:
    # `services/slurm_gateway/__init__.py` is imported as
    # `services.slurm_gateway`; a `.__init__` name is what nothing imports, so
    # deriving importers for it returns the empty set — a guard failing quiet.
    assert _dotted_module_name("services/slurm_gateway/__init__.py") == "services.slurm_gateway"
    assert _dotted_module_name("services/slurm_gateway/real_backend.py") == "services.slurm_gateway.real_backend"


def test_one_hop_importer_derivation_does_not_recurse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bound is the whole point of decision 4, so pin it on a fixture tree
    # instead of on today's tracked graph (which happens to have no second hop
    # and would let a recursive implementation pass unnoticed).
    package = tmp_path / "services" / "probe"
    package.mkdir(parents=True)
    (package / "guarded.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "first_hop.py").write_text("from services.probe import guarded\n", encoding="utf-8")
    (package / "second_hop.py").write_text("from services.probe import first_hop\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    domain = ["services/probe/guarded.py", "services/probe/first_hop.py", "services/probe/second_hop.py"]

    assert _one_hop_importer_modules("services.probe.guarded", domain=domain) == {"services/probe/first_hop.py"}
    assert _one_hop_importer_modules("services.probe.first_hop", domain=domain) == {"services/probe/second_hop.py"}


def test_one_hop_extension_reaches_suites_no_direct_importer_scan_can_see() -> None:
    # Anti-vacuity + coverage for the hop itself: without it this suite is
    # invisible to the guard (it imports reconcile, not real_backend) and a
    # real_backend-only PR never runs the sacct parsing assertions its
    # constants feed. Derived, never frozen: if reconcile.py stops importing
    # real_backend at file level, this reddens rather than rotting green.
    module = "services.slurm_gateway.real_backend"

    assert REAL_BACKEND_ONE_HOP_MEMBER not in _non_gated_top_level_importer_tests(module)
    assert REAL_BACKEND_ONE_HOP_MEMBER in _one_hop_importer_tests(module)
    assert REAL_BACKEND_ONE_HOP_MEMBER in select_tests(
        ["services/slurm_gateway/real_backend.py"], repo_root=Path(".")
    )


def test_guarded_module_rules_cover_their_non_gated_importer_closure() -> None:
    # Fourth recurrence of the same defect class (#1191 -> #1247 -> #1283 ->
    # #1447): a rule is written without a "who imports this module" scan, the PR
    # lane goes green on a partial selection, and the gap only shows up on the
    # post-merge master full run. The importer set is DERIVED from the tracked
    # tree here, never frozen, so a new importer suite reddens this test
    # (pointing at the rule to extend) instead of falling out of the PR lane.
    # The required set is direct importers UNION the one-hop contribution
    # (#1455): a module that imports the guarded module at file level carries
    # its behavior into its own importer suites, and those were falling out of
    # the PR lane (8 of them for real_backend). One hop only — see
    # _one_hop_importer_modules for why the bound is policy, not a measurement.
    # For display_coverage the hop contributes nothing today (its single one-hop
    # module, scripts/node27_refresh_coverage.py, has no non-gated top-level
    # importer suite), so the union is derived rather than asserted per module.
    offenders: list[str] = []
    for source_path, module, known_member in GUARDED_MODULE_CLOSURES:
        assert Path(source_path).is_file(), f"guarded module source missing: {source_path}"
        direct = _non_gated_top_level_importer_tests(module)
        assert direct, f"{module}: derived no non-gated top-level importer suites"
        assert known_member in direct, (
            f"{module}: expected {known_member} among derived importers, got {sorted(direct)}"
        )
        required = direct | _one_hop_importer_tests(module)

        selected = set(select_tests([source_path], repo_root=Path(".")))
        missing = sorted(required - selected)
        if missing:
            offenders.append(f"{source_path}: rule misses {module} importer suites {missing}")
    assert not offenders, "guarded-module importer closure incomplete: " + "; ".join(offenders)


def test_gated_display_coverage_importer_is_excluded_from_the_guarded_closure() -> None:
    # The #1447 ruling, pinned: the one integration-marked importer on master
    # skips in the PR lane, so requiring it in the rule buys constant skips and
    # zero assertions. If its marker is ever dropped, the closure grows and the
    # guard above starts demanding it — that is the intended coupling.
    assert Path(DISPLAY_COVERAGE_GATED_IMPORTER).is_file()
    tree = _parse_tracked(DISPLAY_COVERAGE_GATED_IMPORTER)

    assert "packages.common.display_coverage" in _top_level_imported_module_names(
        DISPLAY_COVERAGE_GATED_IMPORTER, tree
    )
    assert _file_level_gating_markers(tree) == {"integration"}
    assert DISPLAY_COVERAGE_GATED_IMPORTER not in _non_gated_top_level_importer_tests(
        "packages.common.display_coverage"
    )
    assert DISPLAY_COVERAGE_GATED_IMPORTER not in select_tests(
        ["packages/common/display_coverage.py"], repo_root=Path(".")
    )


def test_top_level_import_walk_ignores_function_body_imports(tmp_path: Path) -> None:
    # tests/test_analysis_pipeline.py and tests/test_gateway_reconcile.py reach
    # real_backend only from inside a test body; treating those as importer
    # suites would drag whole slow files into every gateway PR. Pin the
    # distinction on a fixture instead of on those two files, which may move.
    probe = tmp_path / "tests" / "test_probe.py"
    probe.parent.mkdir()
    probe.write_text(
        "from services.slurm_gateway import real_backend\n"
        "\n"
        "def test_lazy() -> None:\n"
        "    from packages.common import display_coverage\n"
        "    assert display_coverage is not None\n",
        encoding="utf-8",
    )
    rel = "tests/test_probe.py"
    tree = ast.parse(probe.read_text(encoding="utf-8"), filename=rel)

    top_level = _top_level_imported_module_names(rel, tree)
    assert "services.slurm_gateway.real_backend" in top_level
    assert "packages.common.display_coverage" not in top_level


def test_file_level_gating_marker_detection_is_ast_shaped(tmp_path: Path) -> None:
    # Substring guesswork would call all four of these gated. Only the first two
    # gate the file; a per-function decorator and a non-gating file-level marker
    # must leave the suite inside the closure.
    sources = {
        "scalar": ("pytestmark = pytest.mark.integration\n", {"integration"}),
        "list": ("pytestmark = [pytest.mark.e2e, pytest.mark.real_disk]\n", {"e2e"}),
        "decorator_only": (
            "@pytest.mark.integration\ndef test_one() -> None:\n    pass\n",
            set(),
        ),
        "non_gating": (
            'pytestmark = pytest.mark.skipif(True, reason="x")\n',
            set(),
        ),
    }
    for name, (body, expected) in sources.items():
        path = tmp_path / f"test_{name}.py"
        path.write_text("import pytest\n" + body, encoding="utf-8")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        assert _file_level_gating_markers(tree) == expected, name


def _assigned_names(tree: ast.Module) -> list[str]:
    """Module-level assignment target names — the two cache guards' content probe."""
    return [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]


def test_parse_cache_keeps_a_chdir_fixture_off_the_repository_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _parse_tracked is memoized, and two guards above parse repo-shaped
    # relative spellings from inside tmp_path (the relative-import walk and the
    # one-hop recursion guard). Keying the cache on the caller's spelling
    # instead of the resolved path would hand those fixtures the repository
    # file of the same name the day one reuses a tracked name — a false green.
    rel = "scripts/select_ci_tests.py"
    repo_tree = _parse_tracked(rel)
    assert "select_tests" in {node.name for node in repo_tree.body if isinstance(node, ast.FunctionDef)}

    fixture = tmp_path / rel
    fixture.parent.mkdir(parents=True)
    fixture.write_text("PARSE_CACHE_FIXTURE_MARKER = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _assigned_names(_parse_tracked(rel)) == ["PARSE_CACHE_FIXTURE_MARKER"], (
        "the cache aliased a tmp_path fixture onto the repository parse"
    )


def test_parse_cache_observes_a_rewrite_of_an_already_parsed_file(tmp_path: Path) -> None:
    # Stat identity, not just the path, is what makes a rewritten file a new
    # cache key. The rewrite below keeps the byte count identical so the guard
    # bites on the mtime_ns half of the key too, and bumps mtime explicitly
    # rather than leaning on filesystem timestamp granularity.
    probe = tmp_path / "probe_module.py"
    probe.write_text("ALPHA = 1\n", encoding="utf-8")
    assert _assigned_names(_parse_tracked(str(probe))) == ["ALPHA"]

    probe.write_text("OMEGA = 2\n", encoding="utf-8")
    before = os.stat(probe)
    os.utime(probe, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    assert _assigned_names(_parse_tracked(str(probe))) == ["OMEGA"], (
        "the cache served a stale parse of a rewritten file"
    )


def test_changed_test_file_also_selects_the_selector_meta_guards() -> None:
    # #1254: the tree-derived meta-guards above are only worth having if they
    # run on the PR class that can invalidate them — a PR touching test files.
    selected = select_tests(["tests/test_node27_timeseries_compression_capture.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_node27_timeseries_compression_capture.py",
        "tests/test_select_ci_tests.py",
    ]


def test_changed_selector_suite_selects_only_itself() -> None:
    # Self-selection must not double up now that the meta-guard target is the
    # same file.
    assert select_tests(["tests/test_select_ci_tests.py"], repo_root=Path(".")) == [
        "tests/test_select_ci_tests.py",
    ]


@pytest.mark.parametrize(
    "changed_path",
    ["tests/conftest.py", "tests/integration_helpers.py"],
)
def test_meta_guard_accumulation_is_scoped_to_test_file_names(changed_path: str) -> None:
    # The changed-test branch condition is wider than `tests/test_*.py`; the
    # meta-guard accumulation is not, and neither is self-selection any more.
    # A support module used to select ITSELF, which pytest answers with
    # NO_TESTS_COLLECTED (exit 5) and ci.yml's check=True turns into a
    # misleading, zero-assertion red (#1453). It now maps to the meta-guard
    # suite. The pin's original intent survives unchanged: a support-file change
    # still spills nothing — no whole suite, no core smoke, exactly one target.
    assert Path(changed_path).is_file()

    assert select_tests([changed_path], repo_root=Path(".")) == [SELECTOR_META_GUARD_TEST]


def _tracked_tests_support_modules() -> list[str]:
    """Tracked `tests/**.py` that pytest cannot collect as a suite.

    Deliberately NOT `_tracked_top_level_test_files()`: that helper matches the
    repo-relative path against `tests/test_*.py` for the importer-closure
    domain, which would count a nested `tests/pkg/test_x.py` as a support module
    here and cement the misclassification this test exists to catch.

    It calls the selector's own `is_test_suite_path` rather than restating the
    pattern list. That looks like the expectation moving with the bug, and it
    would be — except the anchor test below ties that predicate to what pytest
    ACTUALLY collects. A second hand-written mirror is what produced two wrong
    derivations in a row (path-shaped, then single-pattern); one predicate with
    an external oracle is the version that cannot drift quietly.
    """
    return sorted(path for path in _tracked_python_files("tests") if not is_test_suite_path(path))


# Support modules #1487 deliberately leaves on the collapse route even though
# they DO derive non-gated importer suites. Issue #1487 excludes both by name, so
# this is an inherited scope boundary, not a coverage claim. The factual
# predicate it cites: ci.yml's `database` paths-filter lists both paths and
# starts `real-db-integration`, which runs `pytest -q -m integration` — measured
# coverage of their importers' tests is 75 of 245 (integration_helpers) and 0 of
# 19 (conftest), because the non-gated derivation and `-m integration` select
# near-disjoint sets by construction. PARTIAL, not full compensation. The
# closure guard pins each path inside that filter block, so if the filter drops
# one the carve-out reds and has to be re-decided rather than rotting into a
# silent hole. Full routing for these two is a candidate follow-up.
ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES = frozenset(
    {
        "tests/integration_helpers.py",
        "tests/conftest.py",
    }
)


def test_unrouted_tests_support_modules_select_only_the_meta_guard_suite() -> None:
    # Derived from the tracked tree, never frozen: the class is not just
    # conftest.py/integration_helpers.py/__init__.py but every non-suite module
    # under tests/ (fixture builders, fakes, template helpers), and a support
    # module added tomorrow is covered the moment it lands. Each must map to a
    # COLLECTIBLE target, or ci.yml's check=True red carries no assertion
    # information at all.
    #
    # RESCOPED by #1487, which is why the name is no longer "every tracked": a
    # support module with a SUPPORT_MODULE_TEST_RULES entry now selects its
    # derived importer suites plus the meta-guard, so asserting the collapse for
    # it would assert the inverse of the routing. What stays here is the collapse
    # route itself — today exactly the recorded carve-outs plus the modules that
    # derive no importer suites. The scoping reads the rule table rather than
    # re-deriving importers so the two guards cannot contradict each other: an
    # importer-bearing module with NO rule stays in this domain (it still
    # collapses, correctly asserted here) and is red over in the closure guard,
    # which is the single authority on whether a rule was owed.
    support_modules = _tracked_tests_support_modules()
    assert support_modules, "expected tracked tests/ modules that are not test_*.py suites"
    routed = {rule.pattern for rule in SUPPORT_MODULE_TEST_RULES}
    collapsing = [path for path in support_modules if path not in routed]
    assert collapsing, "expected tracked tests/ support modules outside the routing table"
    # Anti-vacuity for the carve-outs specifically: routing one of them would
    # otherwise shrink this domain in silence instead of forcing the decision.
    assert ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES <= set(collapsing)

    offenders = [
        f"{path}: selected {selected}"
        for path in collapsing
        if (selected := select_tests([path], repo_root=Path("."))) != [SELECTOR_META_GUARD_TEST]
    ]
    assert not offenders, "unrouted tests/ support modules must map to the meta-guard suite: " + "; ".join(offenders)
    # The class boundary the derivation depends on: a nested suite is not a
    # support module. Zero tracked instances today, which is exactly why the
    # boundary needs asserting rather than observing.
    assert not [path for path in support_modules if PurePosixPath(path).name.startswith("test_")]


def test_nested_test_suite_self_selects_and_drags_the_meta_guards(tmp_path: Path) -> None:
    # `tests/pkg/test_x.py` is a collectible suite: pytest runs it, and a PR
    # changing it can invalidate the tree-derived meta-guards exactly like a
    # top-level suite can. A path-shaped `tests/test_*.py` predicate calls it a
    # support module, which loses BOTH — self-selection is replaced by the
    # meta-guard mapping (#1453 misfiring on a real suite) and the #1254
    # accumulation never fires. One basename predicate decides both, so this
    # pins them together.
    nested = tmp_path / "tests" / "pkg" / "test_nested_probe.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("def test_nested_probe(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_select_ci_tests.py").write_text("def test_x(): pass\n", encoding="utf-8")

    selected = select_tests(["tests/pkg/test_nested_probe.py"], repo_root=tmp_path)

    assert selected == ["tests/pkg/test_nested_probe.py", SELECTOR_META_GUARD_TEST]


def test_suffix_named_test_suite_self_selects_and_drags_the_meta_guards(tmp_path: Path) -> None:
    # `x_test.py` is pytest's OTHER default `python_files` pattern, and the
    # first basename predicate covered only `test_*.py` — so this shape was
    # classified as a support module, its assertions never ran on a PR that
    # changed it, and the tree-derived invariant test above would have cemented
    # that the day someone added one. Zero tracked instances today; the pin is
    # what keeps the second pattern from being dropped again.
    suffix_named = tmp_path / "tests" / "gateway_probe_test.py"
    suffix_named.parent.mkdir()
    suffix_named.write_text("def test_gateway_probe(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_select_ci_tests.py").write_text("def test_x(): pass\n", encoding="utf-8")

    selected = select_tests(["tests/gateway_probe_test.py"], repo_root=tmp_path)

    assert selected == ["tests/gateway_probe_test.py", SELECTOR_META_GUARD_TEST]


def test_nested_suffix_named_test_suite_self_selects_and_drags_the_meta_guards(tmp_path: Path) -> None:
    # Both axes at once — nested directory AND suffix naming — because the two
    # previous derivations each got exactly one axis right.
    nested = tmp_path / "tests" / "pkg" / "gateway_probe_test.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("def test_gateway_probe(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_select_ci_tests.py").write_text("def test_x(): pass\n", encoding="utf-8")

    selected = select_tests(["tests/pkg/gateway_probe_test.py"], repo_root=tmp_path)

    assert selected == ["tests/pkg/gateway_probe_test.py", SELECTOR_META_GUARD_TEST]


# Probe tree for the pytest anchor: every naming shape whose classification the
# selector has to get right, each carrying a trivial test function so that being
# uncollected is a statement about the NAME, never about the contents.
# `helper.py` and `testing_helpers.py` are the discriminators — both define a
# `test_*` function, and pytest still ignores them, so a predicate that looked at
# file contents (or at a "starts with test" prefix) would disagree here.
PYTEST_COLLECTION_PROBE_FILES: tuple[str, ...] = (
    "tests/test_x.py",
    "tests/x_test.py",
    "tests/pkg/test_y.py",
    "tests/pkg/y_test.py",
    "tests/helper.py",
    "tests/testing_helpers.py",
    "tests/conftest.py",
)


def _pytest_collected_files(tree_root: Path) -> set[str]:
    """Repo-relative files pytest collects at least one test from under ``tree_root``.

    Runs the real collector under THIS repo's ini options (`-c pyproject.toml`),
    not pytest's bare defaults, so that a future `python_files` override in
    pyproject is what the anchor tracks. `--rootdir` is the probe tree, which
    makes the reported node ids relative to it and keeps the repo's own
    conftest.py out of the run (the tree is not below the repo).
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-c",
            str(Path("pyproject.toml").resolve()),
            "--rootdir",
            str(tree_root),
            "-p",
            "no:cacheprovider",
            # zarr's pytest plugin costs ~1s to import and has no bearing on
            # name-based collection; ini options (including any future
            # `python_files` override) still come from -c above.
            "-p",
            "no:zarr",
            str(tree_root),
        ],
        capture_output=True,
        text=True,
        cwd=tree_root,
    )
    assert completed.returncode == 0, f"probe collection failed:\n{completed.stdout}\n{completed.stderr}"
    return {line.split("::", 1)[0].strip() for line in completed.stdout.splitlines() if "::" in line}


def test_selector_suite_classification_equals_pytest_collection(tmp_path: Path) -> None:
    # THE closure. Twice now the suite-vs-support predicate was derived by hand
    # from a reading of pytest's behavior, and twice it was incomplete — each
    # time costing a real suite its self-selection and the meta-guard
    # accumulation, and each time invisible because no test compared the
    # predicate to the thing it was mirroring. This one does: it asks pytest
    # what it collects and asserts the selector agrees, file for file. A third
    # drift (new `python_files` pattern upstream, a pyproject override, a
    # predicate edit) reddens here instead of shipping.
    for relative in PYTEST_COLLECTION_PROBE_FILES:
        probe = tmp_path / relative
        probe.parent.mkdir(parents=True, exist_ok=True)
        body = "" if relative.endswith("conftest.py") else "def test_probe(): pass\n"
        probe.write_text(body, encoding="utf-8")

    collected = _pytest_collected_files(tmp_path)
    classified = {relative for relative in PYTEST_COLLECTION_PROBE_FILES if is_test_suite_path(relative)}

    assert collected, "probe tree collected nothing — the anchor would pass vacuously"
    assert classified == collected, (
        "selector suite-classification drifted from pytest collection: "
        f"selector-only {sorted(classified - collected)}, pytest-only {sorted(collected - classified)}"
    )


def test_selector_suite_patterns_equal_the_effective_pytest_python_files(pytestconfig: pytest.Config) -> None:
    # Second floor under the anchor above, covering patterns the probe tree does
    # not happen to exercise: the pattern LIST itself must be the effective
    # `python_files`, read from the running config rather than from pytest's
    # documented defaults (which an ini override would silently displace).
    assert tuple(pytestconfig.getini("python_files")) == CHANGED_TEST_SUITE_BASENAME_PATTERNS


def test_meta_guard_target_is_dropped_with_a_warning_under_a_root_without_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tmp-root selections go through the same global missing-target drop as any
    # other stale target: the meta-guard is announced and dropped, not special
    # cased. Pinned so the drop path is not re-litigated per target.
    test_path = tmp_path / "tests" / "test_example.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_example(): pass\n", encoding="utf-8")
    assert not (tmp_path / "tests" / "test_select_ci_tests.py").exists()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    assert select_tests(["tests/test_example.py"], repo_root=tmp_path) == ["tests/test_example.py"]

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tests/test_select_ci_tests.py" in captured.err


def test_select_tests_falls_back_to_core_smoke_for_unknown_backend_python_path() -> None:
    selected = select_tests(["services/new_surface/new_module.py"], repo_root=Path("."))

    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def test_select_tests_adds_core_smoke_for_unknown_backend_path_mixed_with_known_path() -> None:
    selected = select_tests(
        ["workers/data_adapters/gfs_adapter.py", "services/new_surface/new_module.py"],
        repo_root=Path("."),
    )

    assert "tests/test_gfs_adapter.py" in selected
    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def test_select_tests_ignores_docs_only_changes() -> None:
    assert select_tests(["docs/runbooks/current-production-ops.md"], repo_root=Path(".")) == []


INTENTIONAL_DUPLICATE_PATTERNS = frozenset({"services/orchestrator/scheduler.py"})


def _duplicated_rule_patterns(rules: Sequence[PathTestRule]) -> set[str]:
    """Patterns appearing more than once in ``rules`` — parameterized on purpose.

    Taking the rule list as an argument is what lets the collision below be
    simulated on a constructed list instead of on the live table.
    """
    counts = Counter(rule.pattern for rule in rules)
    return {pattern for pattern, count in counts.items() if count > 1}


def test_path_rule_duplicate_patterns_are_allowlisted_decisions() -> None:
    # A pattern listed twice splits rule ownership: the second entry's targets
    # are easy to miss when reading the first, and a `stop_on_match=True` entry
    # earlier in the table can silently amputate the later one. Today exactly
    # one duplicate is deliberate — services/orchestrator/scheduler.py carries a
    # narrow non-stop entry plus a stop-on-match layering — so duplication must
    # be an allowlisted decision, not an accident.
    duplicates = _duplicated_rule_patterns(PATH_TEST_RULES)

    unexplained = sorted(duplicates - INTENTIONAL_DUPLICATE_PATTERNS)
    assert not unexplained, (
        f"unallowlisted duplicate PATH_TEST_RULES patterns {unexplained}: "
        "consolidate the entries, or record the layering in INTENTIONAL_DUPLICATE_PATTERNS"
    )
    # Anti-rot in the other direction: an allowlist entry that stopped being
    # duplicated is a stale exemption waiting to hide the next accident.
    stale = sorted(INTENTIONAL_DUPLICATE_PATTERNS - duplicates)
    assert not stale, f"INTENTIONAL_DUPLICATE_PATTERNS members that are no longer duplicated: {stale}"


def test_duplicate_pattern_guard_flags_an_unmerged_sibling_collision() -> None:
    # #1443 adds a second packages/common/display_coverage.py entry. On merge it
    # would split that module's ownership across two rules with nothing saying
    # so. Simulated on a constructed list — the live table stays untouched — so
    # the day it lands the guard above reds by name instead of going quiet.
    collision_pattern = "packages/common/display_coverage.py"
    would_be_merged = (
        *PATH_TEST_RULES,
        PathTestRule(collision_pattern, ("tests/test_display_coverage_refresh.py",)),
    )

    duplicates = _duplicated_rule_patterns(would_be_merged)

    assert collision_pattern in duplicates
    assert collision_pattern not in INTENTIONAL_DUPLICATE_PATTERNS
    assert sorted(duplicates - INTENTIONAL_DUPLICATE_PATTERNS) == [collision_pattern]


def _unconditional_duplicate_rules(rules: Sequence[PathTestRule]) -> list[str]:
    """Duplicated patterns whose entries are not all `only_when_any_changed`."""
    duplicates = _duplicated_rule_patterns(rules)
    return sorted(
        {
            rule.pattern
            for rule in rules
            if rule.pattern in duplicates and not rule.only_when_any_changed
        }
    )


def test_changed_test_rule_duplicates_stay_out_of_the_guard_domain() -> None:
    # CHANGED_TEST_FILE_RULES duplicates its patterns by design (#1254): the
    # same changed test file gets different focused targets depending on which
    # surface files moved with it, expressed through only_when_any_changed. The
    # guard is deliberately scoped to PATH_TEST_RULES, so record the exemption
    # as a fact about the table rather than as silence.
    #
    # Scoped to the DUPLICATED patterns on purpose: an unconditional rule that
    # appears once splits no ownership and is a legitimate table entry (open PR
    # #1443 adds one), so demanding only_when_any_changed of every rule would
    # red on it with a message about duplicates it does not create. What the
    # exemption actually rests on is that each duplicate's entries discriminate
    # by only_when_any_changed — without that they would be two unconditional
    # entries for one pattern, i.e. exactly the split the PATH_TEST_RULES guard
    # forbids.
    duplicates = _duplicated_rule_patterns(CHANGED_TEST_FILE_RULES)

    assert duplicates == {"tests/test_orchestration_chain.py", "tests/test_production_scheduler.py"}
    assert not _unconditional_duplicate_rules(CHANGED_TEST_FILE_RULES)


def test_changed_test_rule_exemption_reds_on_an_unconditional_duplicate() -> None:
    # The hazard the narrowed assert still has to catch, simulated on a
    # constructed list: a second entry for an already-duplicated pattern with no
    # only_when_any_changed fires on every PR touching that suite, silently
    # widening the redirect the #1254 design deliberately keeps conditional.
    hazard = (
        *CHANGED_TEST_FILE_RULES,
        PathTestRule("tests/test_orchestration_chain.py", ORCHESTRATOR_MANIFEST_SURFACE_TESTS),
    )

    assert _unconditional_duplicate_rules(hazard) == ["tests/test_orchestration_chain.py"]
    # ...and a single unconditional non-duplicate entry (the #1443 shape) does not.
    benign = (*CHANGED_TEST_FILE_RULES, PathTestRule("tests/test_display_coverage_refresh.py", CORE_SMOKE_TESTS))
    assert not _unconditional_duplicate_rules(benign)


# The first six inputs below sit inside ci.yml's `backend` paths-filter (so the
# "Unit Tests" gate opens) yet map to no test file, leaving the job in its
# collect-only, zero-assertion branch. Those six PIN that route-C contract —
# empty selection is allowed, but ci.yml now labels it loudly (warning
# annotation + step summary) instead of reporting an informationless green.
# The seventh, `scripts/run_x.sh`, matches NO `backend` pattern, so the Unit
# Tests job never starts for it at all; that param pins selector emptiness
# only and says nothing about the collect-only branch. All seven are pins, NOT
# endorsements: the `scripts/**/*.sh` class is #1138's layer to flip, the
# remaining classes belong to a future route-A/B (selector-widening or
# empty-selection-fails) decision. Flipping any of them must change a visible
# assertion here.
# Note the classes that are NOT here any more: a PR deleting a `tests/test_*.py`
# and a PR touching only an UNROUTED `tests/` support module (the recorded
# carve-outs, or one deriving no importer suites) both leave a one-element
# selection (the meta-guard suite), so they never reached this empty-selection
# branch and lost the full-tree collect-only smoke they used to get. #1454's
# `meta_guard_only` output field is what runs the smoke for them, in addition to
# the targeted run — this branch's own semantics are untouched. A support module
# WITH a SUPPORT_MODULE_TEST_RULES entry (#1487) is in neither class: it selects
# real importer suites, so `meta_guard_only` is false and the targeted lane runs
# assertions.
@pytest.mark.parametrize(
    "changed_path",
    [
        pytest.param("schemas/x.schema.json", id="schemas"),
        pytest.param("infra/nginx/site.conf", id="unmapped-infra"),
        pytest.param("openspec/tools/x.py", id="py-outside-backend-prefixes"),
        pytest.param("apps/frontend/scripts/gen.py", id="py-under-apps-frontend"),
        pytest.param("packages/common/sql/x.sql", id="non-py-under-backend-prefix"),
        pytest.param("tests/fixtures/sample.json", id="non-py-under-tests"),
        pytest.param("scripts/run_x.sh", id="shell-script"),
    ],
)
def test_select_tests_pins_known_empty_selection_classes(changed_path: str) -> None:
    assert select_tests([changed_path], repo_root=Path(".")) == []


def test_select_tests_warns_when_a_rule_target_no_longer_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `db/**` maps to tests/test_migrations.py, which is absent from this tmp
    # root — the same shape a test-file rename leaves behind. The target is
    # still dropped (return semantics unchanged), but no longer in silence.
    assert not (tmp_path / "tests" / "test_migrations.py").exists()

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert select_tests(["db/README.md"], repo_root=tmp_path) == []

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tests/test_migrations.py" in captured.err
    # Local command-substitution usage (`pytest -q $(select_ci_tests.py ...)`)
    # reads stdout: the annotation must not leak into it off-runner.
    assert "::warning" not in captured.out

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert select_tests(["db/README.md"], repo_root=tmp_path) == []

    captured = capsys.readouterr()
    assert "::warning" in captured.out
    assert "tests/test_migrations.py" in captured.out
    assert "WARNING" in captured.err


def test_select_tests_emits_no_stale_target_warning_when_every_target_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Noise regression guard: the warning must fire on real drops only, or
    # every CI run grows an annotation nobody reads.
    assert select_tests(["db/README.md"], repo_root=Path(".")) == ["tests/test_migrations.py"]

    captured = capsys.readouterr()
    assert "WARNING" not in captured.err
    assert "::warning" not in captured.out


def test_main_writes_json_github_output(tmp_path: Path) -> None:
    changed_file = tmp_path / "changed.txt"
    output_file = tmp_path / "github-output.txt"
    changed_file.write_text("infra/compose.compute.yml\n", encoding="utf-8")

    assert main(["--changed-file", str(changed_file), "--github-output", str(output_file)]) == 0

    output = output_file.read_text(encoding="utf-8")
    assert "count=1\n" in output
    assert 'tests_json=["tests/test_two_node_docker_runtime.py"]\n' in output


def _github_output_fields(tmp_path: Path, changed: Sequence[str], *, repo_root: Path) -> dict[str, str]:
    changed_file = tmp_path / "changed.txt"
    output_file = tmp_path / "github-output.txt"
    changed_file.write_text("".join(f"{path}\n" for path in changed), encoding="utf-8")

    assert (
        main(
            [
                "--changed-file",
                str(changed_file),
                "--repo-root",
                str(repo_root),
                "--github-output",
                str(output_file),
            ]
        )
        == 0
    )
    return dict(
        line.split("=", 1)
        for line in output_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def test_github_output_flags_the_deleted_test_file_meta_guard_collapse(tmp_path: Path) -> None:
    # The class #1254 silently created: before it, a PR whose only backend
    # change deletes a test file selected nothing and got ci.yml's full-tree
    # collect-only smoke; after it, the unconditional meta-guard accumulation
    # survives the missing-target filter, so count is 1 and the smoke is lost —
    # even though a deletion is exactly what breaks cross-test imports. The flag
    # is computed on the POST-filter list, which is what makes this shape
    # visible at all.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_select_ci_tests.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert not (tmp_path / "tests" / "test_gone.py").exists()

    fields = _github_output_fields(tmp_path, ["tests/test_gone.py"], repo_root=tmp_path)

    assert fields["count"] == "1"
    assert fields["tests"] == SELECTOR_META_GUARD_TEST
    assert fields["meta_guard_only"] == "true"


def test_github_output_flags_the_support_module_collapse(tmp_path: Path) -> None:
    fields = _github_output_fields(tmp_path, ["tests/conftest.py"], repo_root=Path("."))

    assert fields["tests"] == SELECTOR_META_GUARD_TEST
    assert fields["meta_guard_only"] == "true"


@pytest.mark.parametrize(
    "changed_path",
    ["scripts/select_ci_tests.py", "tests/test_select_ci_tests.py"],
)
def test_github_output_flags_selector_development_diffs_honestly(tmp_path: Path, changed_path: str) -> None:
    # Accepted shape-not-provenance semantics (design decision 2): these diffs
    # have the meta-guard suite as their diff-specific target, so they fire the
    # flag and pay one extra collection pass. Special-casing them would trade a
    # two-line predicate for a provenance rule on exactly the PR class that
    # rewrites the gate — the class least well served by a subtle exemption.
    fields = _github_output_fields(tmp_path, [changed_path], repo_root=Path("."))

    assert fields["meta_guard_only"] == "true"


@pytest.mark.parametrize(
    ("changed_path", "expected_count"),
    [
        # Two targets: the changed suite plus the accumulated meta-guard.
        ("tests/test_orchestration_chain.py", "2"),
        # Empty selection: route C, whose own collect-only branch is unchanged.
        ("docs/runbooks/current-production-ops.md", "0"),
        # The discrimination boundary. A single-target selection that is NOT the
        # meta-guard suite must stay false — 15 rules in today's table select
        # exactly one file, so a flag that merely counted targets would arm the
        # extra collection pass on all of them and nothing here would notice.
        ("db/schema.sql", "1"),
    ],
)
def test_github_output_suppresses_the_flag_for_non_collapsed_selections(
    tmp_path: Path,
    changed_path: str,
    expected_count: str,
) -> None:
    fields = _github_output_fields(tmp_path, [changed_path], repo_root=Path("."))

    assert fields["count"] == expected_count
    assert fields["meta_guard_only"] == "false"


COLLAPSE_BRANCH_MARKER = 'if [ "${{ steps.targeted.outputs.meta_guard_only }}"'


def _targeted_job_collapse_block() -> str:
    """ci.yml's meta-guard-collapse branch, from its `if` to its matching `fi`.

    Slicing to the block instead of scanning the whole job is what makes the
    coupling pin killable: a job that merely MENTIONS the field, or that runs
    the smoke somewhere else entirely, no longer satisfies it. The matching
    `fi` is the next one at the branch's own 12-space indent — the inner
    `if pytest … --collect-only` closes at 14 spaces and cannot truncate here.
    """
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    start = workflow.index("\n  unit-test-targeted:")
    end = workflow.index("\n  frontend-build:", start)
    targeted_job = workflow[start:end]

    branch_start = targeted_job.find(COLLAPSE_BRANCH_MARKER)
    assert branch_start != -1, (
        "ci.yml's unit-test-targeted job no longer contains the meta-guard collapse branch "
        f"(looked for {COLLAPSE_BRANCH_MARKER!r}); the #1454 collect-only smoke would be silently dead"
    )
    branch_end = targeted_job.find("\n            fi\n", branch_start)
    assert branch_end != -1, "meta-guard collapse branch in ci.yml has no matching 12-space `fi`"
    return targeted_job[branch_start:branch_end]


def test_ci_workflow_consumes_the_meta_guard_only_output(tmp_path: Path) -> None:
    # String coupling across a boundary no test can execute: the field is
    # written by Python and read by a shell condition in a workflow file. Either
    # side can be renamed alone and nothing else notices — the smoke would just
    # stop running, silently, which is the exact failure mode #1454 exists to
    # end.
    collapse_block = _targeted_job_collapse_block()

    # The condition runs the smoke ON collapse, not on its negation.
    assert collapse_block.startswith(f'{COLLAPSE_BRANCH_MARKER} = "true" ]; then')
    # ...and the smoke it guards is the full-tree collect-only, INSIDE the block.
    assert "pytest tests/ -q --collect-only" in collapse_block
    # The spec's wording constraint (this branch DID execute assertions) gets a
    # pin, not just prose: a copy-paste from the count == 0 branch would lie.
    assert "0 assertions" not in collapse_block
    # ...and the producing side emits that exact key, read from behavior rather
    # than from the selector's source text.
    assert "meta_guard_only" in _github_output_fields(tmp_path, ["tests/conftest.py"], repo_root=Path("."))


def test_every_pinned_node_id_resolves_to_an_existing_test_function() -> None:
    # c4f2a8d4 renamed two scheduler tests but left their node ids pinned in
    # the selector; every scheduler.py PR then failed collection with
    # "ERROR: not found". Pinned node ids must track renames.
    import re
    from pathlib import Path

    source = Path("scripts/select_ci_tests.py").read_text(encoding="utf-8")
    node_ids = sorted(set(re.findall(r'"(tests/[^"]+::[^"]+)"', source)))
    assert node_ids, "expected pinned node ids in the selector"
    stale = []
    for node_id in node_ids:
        test_file, test_name = node_id.split("::", 1)
        if f"def {test_name}(" not in Path(test_file).read_text(encoding="utf-8"):
            stale.append(node_id)
    assert not stale, f"selector pins node ids that no longer exist: {stale}"

    # File-level targets get the same gate. A rule target whose file is gone is
    # dropped at selection time (with a warning since #1182), which can shrink a
    # PR's suite — or empty it — without anyone noticing; the rule set itself
    # must not carry dead targets. Read from the live rule objects, not the
    # source text, so a target added anywhere in the rule set is covered.
    file_targets = sorted(
        {
            target
            for rule in (*PATH_TEST_RULES, *CHANGED_TEST_FILE_RULES, *SUPPORT_MODULE_TEST_RULES)
            for target in rule.tests
            if "::" not in target
        }
        | {target for target in CORE_SMOKE_TESTS if "::" not in target}
    )
    assert file_targets, "expected file-level test targets in the selector rule set"
    stale_files = [target for target in file_targets if not Path(target).is_file()]
    assert not stale_files, f"selector rules point at test files that no longer exist: {stale_files}"


# --------------------------------------------------------------------------
# Directory-rule importer gap disposition (#1455 item (2))
#
# The nine directory rules were audited in PR #1452 and the audit's verdicts
# lived only in that PR's body, where nothing could re-check them. This section
# turns the audit into data the tree carries: every gap pair the derivation
# finds is either closed by a rule or named in INTENTIONAL_RULE_GAP_EXCLUSIONS
# with a reason token.
#
# DOMAIN SPLIT vs test_guarded_module_rules_cover_their_non_gated_importer_closure
# above: that guard owns the two GUARDED_MODULE_CLOSURES modules and derives
# direct importers UNION a ONE-HOP module extension. This guard owns every
# tracked module under the nine audited directory paths and derives DIRECT
# importers only — those directories hold ~150 modules, so one-hop here would
# grow the PR lane without a bound anyone chose. The two domains overlap on
# services/slurm_gateway and neither subsumes the other; each keeps its own
# derivation deliberately.
# --------------------------------------------------------------------------

DIRECTORY_RULE_AUDIT_PATHS: tuple[str, ...] = (
    "workers/output_parser",
    "workers/data_adapters",
    "workers/forcing_producer",
    "workers/shud_runtime",
    "workers/model_registry",
    "services/orchestrator",
    "services/slurm_gateway",
    "services/tile_publisher",
    "services/production_closure",
)

# fn-gated:       gating the file-level marker filter cannot see (function-level
#                 markers, env opt-ins). Admitted only against a recorded
#                 measurement in which the suite executed ZERO assertions
#                 (passed == failed == errors == 0 and skipped == collected).
# redirect:       the owning rule deliberately swaps the whole suite for focused
#                 `::` node ids; the suite IS reached, just not whole-file.
#                 Machine-checked below: the module's own selection must still
#                 carry at least one `<suite>::` node id. Deleting the node ids
#                 from a shared tuple zeroes the coverage, and without this the
#                 claim would have no anchor at all.
# edge-consumer:  the suite belongs to ANOTHER surface's rules, which is where it
#                 is selected from; copying it into this rule would couple
#                 unrelated PR classes. Machine-checked below against exactly
#                 that claim: some rule whose pattern does NOT match the excluded
#                 module must select the suite whole. Membership in any rule is
#                 too weak — the module's own (possibly shadowed) directory rule
#                 would satisfy it and wave a genuine orphan through.
# runtime-budget: the suite executes real assertions but its measured wall clock
#                 does not fit the PR lane. Every entry names its number.
RULE_GAP_REASON_TOKENS: frozenset[str] = frozenset({"fn-gated", "redirect", "edge-consumer", "runtime-budget"})

# The #1452 audit's verdicts, made checkable. 211 pairs derived at d02b4edb;
# 156 of them are now closed by rules and these 55 are the reasoned remainder
# (44 edge-consumer, 7 redirect, 4 runtime-budget, 0 fn-gated).
# Keys are per-pair on purpose — a wildcard would blunt exactly the staleness
# check this table exists for, and the churn a new module causes here is the
# point. Every wall-clock number below was measured once with
# `uv run pytest -q <suite>` in PR-lane conditions (no opt-in env vars) on a
# local macOS box; the hosted runner is slower, so the numbers are a floor.
INTENTIONAL_RULE_GAP_EXCLUSIONS: dict[tuple[str, str], str] = {
    # -- redirect ----------------------------------------------------------
    # These six modules are owned by `stop_on_match` rules that deliberately
    # swap the two slow orchestrator suites for focused `::` node ids
    # (ORCHESTRATOR_MANIFEST_SURFACE_TESTS / FILE_JOURNAL_READ_STATE_TESTS),
    # and direct_grid_contract.py for DIRECT_GRID_CONTRACT_TESTS. The suite is
    # reached, just not whole-file — which is why the derivation still calls it
    # a gap rather than normalizing the `::` away and losing the distinction.
    ("services/orchestrator/chain.py", "tests/test_orchestration_chain.py"): "redirect",
    ("services/orchestrator/chain.py", "tests/test_production_scheduler.py"): "redirect",
    ("services/orchestrator/chain_repository_state.py", "tests/test_production_scheduler.py"): "redirect",
    ("services/orchestrator/cli.py", "tests/test_production_scheduler.py"): "redirect",
    ("services/orchestrator/file_orchestration_journal.py", "tests/test_production_scheduler.py"): "redirect",
    ("services/orchestrator/scheduler.py", "tests/test_production_scheduler.py"): "redirect",
    ("workers/forcing_producer/direct_grid_contract.py", "tests/test_forcing_producer.py"): "redirect",
    # -- edge-consumer: slurm array-job entry points ------------------------
    # tests/test_slurm_array_contract.py contracts the sbatch array entry
    # points, so it top-level-imports the `cli` module (and package) of five
    # directories at once. It belongs to `services/slurm_gateway/**`, which
    # selects it. Copying it into five directory rules would make every adapter,
    # producer, parser and runtime PR pay for a gateway contract.
    ("services/orchestrator/__init__.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("services/orchestrator/cli.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/data_adapters/__init__.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/data_adapters/cli.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/forcing_producer/__init__.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/forcing_producer/cli.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/output_parser/__init__.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/output_parser/cli.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/shud_runtime/__init__.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    ("workers/shud_runtime/cli.py", "tests/test_slurm_array_contract.py"): "edge-consumer",
    # -- edge-consumer: two-node docker runtime -----------------------------
    # tests/test_two_node_docker_runtime.py validates compose files and the
    # deployment topology; it is owned by the `infra/compose.*.yml`,
    # `infra/env/**` and `scripts/validate_two_node_docker_runtime.py` rules.
    # Its imports of these modules are fixture material for that validation.
    ("services/orchestrator/__init__.py", "tests/test_two_node_docker_runtime.py"): "edge-consumer",
    ("services/orchestrator/source_cycle_raw_manifest.py", "tests/test_two_node_docker_runtime.py"): "edge-consumer",
    ("services/production_closure/__init__.py", "tests/test_two_node_docker_runtime.py"): "edge-consumer",
    (
        "services/production_closure/two_node_e2e_docker_security.py",
        "tests/test_two_node_docker_runtime.py",
    ): "edge-consumer",
    ("services/production_closure/two_node_e2e_evidence.py", "tests/test_two_node_docker_runtime.py"): "edge-consumer",
    # -- edge-consumer: data_adapters/base.py cycle-identity helpers --------
    # base.py exports `cycle_id_for`, `format_cycle_time` and `CycleDiscovery`;
    # fourteen orchestration-, API- and journal-layer suites import one of those
    # three names and nothing else from the adapters. Each is selected by the
    # rule that owns its own surface, which is where a regression in it would be
    # investigated. Pulling all fourteen into `workers/data_adapters/**` would
    # put the orchestrator's journal and scheduler suites on every GFS/IFS/ERA5
    # adapter PR. The one importer with NO owning rule anywhere,
    # tests/test_state_clone_cutover_hook.py, is not here: it gets a narrow
    # `workers/data_adapters/base.py` rule instead.
    ("workers/data_adapters/base.py", "tests/test_api_contract.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_e2e_m3.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_file_orchestration_journal.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_file_orchestration_journal_read_cache.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_file_orchestration_migration.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_ifs_forecast_integration.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_monitoring_api.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_pipeline_logs_artifacts.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_production_readiness_validation.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_scheduler_backfill.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_scheduler_backfill_predecessor.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_source_scoped_dispatch.py"): "edge-consumer",
    ("workers/data_adapters/base.py", "tests/test_state_clone.py"): "edge-consumer",
    # -- runtime-budget ----------------------------------------------------
    # Two of the 74 suites do not fit the lane. tests/test_orchestration_chain.py
    # is the extreme one: the capped PR-lane run was killed at 596s having
    # completed 47% of its 306 items (~21 min extrapolated), against a ~5 min
    # per-module line. `services/orchestrator/**` already carries it for the
    # orchestrator's own modules; putting it on an adapter or gateway PR as
    # well is what would actually threaten the 35-min Unit Tests cap. Both
    # importers here take it as a fixture dependency — base.py for
    # `cycle_id_for`, config.py for the gateway settings object.
    ("services/slurm_gateway/config.py", "tests/test_orchestration_chain.py"): "runtime-budget",
    ("workers/data_adapters/base.py", "tests/test_orchestration_chain.py"): "runtime-budget",
    # tests/test_gateway_reconcile.py is the audit's second-heaviest suite:
    # 246.5s for 487 assertions. Its primary subject IS
    # services/orchestrator/reconcile.py (persistence.py supplies the rows it
    # reconciles), so `edge-consumer` would be a lie — the suite does not belong
    # to another surface; `services/slurm_gateway/**` picks it up on a filename
    # coincidence. What actually keeps it off a reconcile.py PR is the 246.5s,
    # against a ~5 min per-module line the orchestrator rule's own targets
    # already spend most of.
    ("services/orchestrator/persistence.py", "tests/test_gateway_reconcile.py"): "runtime-budget",
    ("services/orchestrator/reconcile.py", "tests/test_gateway_reconcile.py"): "runtime-budget",
    # -- edge-consumer: orchestrator modules whose importers live elsewhere --
    # Each of these suites is selected by the rule for the surface it actually
    # tests: production-closure validation, model-registry bootstrap, the slurm
    # gateway, the forcing producer, apps/api.
    ("services/orchestrator/chain.py", "tests/test_production_readiness_validation.py"): "edge-consumer",
    ("services/orchestrator/chain_types.py", "tests/test_file_orchestration_journal.py"): "edge-consumer",
    ("services/orchestrator/chain.py", "tests/test_qhh_scripts_static.py"): "edge-consumer",
    ("services/orchestrator/chain.py", "tests/test_real_slurm_gateway.py"): "edge-consumer",
    ("services/orchestrator/chain.py", "tests/test_source_identity.py"): "edge-consumer",
    ("services/orchestrator/production_contract.py", "tests/test_api_contract.py"): "edge-consumer",
    ("services/orchestrator/retry.py", "tests/test_real_slurm_gateway.py"): "edge-consumer",
    ("services/orchestrator/scheduler.py", "tests/test_production_readiness_validation.py"): "edge-consumer",
    ("services/orchestrator/scheduler.py", "tests/test_qhh_production_bootstrap.py"): "edge-consumer",
    # -- edge-consumer: slurm_gateway/config.py -----------------------------
    # config.py is a settings module every orchestration suite constructs a
    # gateway client from. These three importers are orchestrator-surface suites
    # selected by `services/orchestrator/**` (test_analysis_pipeline.py also by
    # the chain.py and output_parser/parser.py rules); its fourth,
    # tests/test_orchestration_chain.py, is routed by runtime below.
    ("services/slurm_gateway/config.py", "tests/test_analysis_pipeline.py"): "edge-consumer",
    ("services/slurm_gateway/config.py", "tests/test_orchestrator.py"): "edge-consumer",
    ("services/slurm_gateway/config.py", "tests/test_production_scheduler.py"): "edge-consumer",
    # -- edge-consumer: remaining single routings ---------------------------
    # tests/test_production_scheduler.py is a scheduler suite (owned by
    # `services/orchestrator/**`) that imports the SHUD runtime to drive it;
    # tests/test_source_scoped_dispatch.py likewise imports the producer only to
    # assert the scheduler's missing-source dispatch; and
    # tests/test_production_object_store_validation.py is a production-closure
    # suite reading basin geometry.
    ("workers/shud_runtime/__init__.py", "tests/test_production_scheduler.py"): "edge-consumer",
    ("workers/shud_runtime/runtime.py", "tests/test_production_scheduler.py"): "edge-consumer",
    ("workers/forcing_producer/producer.py", "tests/test_source_scoped_dispatch.py"): "edge-consumer",
    ("workers/model_registry/basins_geometry.py", "tests/test_production_object_store_validation.py"): "edge-consumer",
}


def _non_gated_top_level_importer_index() -> dict[str, set[str]]:
    """Inverted index: dotted module name -> non-gated top-level importer suites.

    The inversion is what makes the guard affordable. Asking
    ``_non_gated_top_level_importer_tests`` per module re-parses all ~190 test
    files each call and measured over two minutes for this domain; parsing each
    test file once and inverting measures ~1 s. Same predicate, same marker
    filter — only the loop order differs.
    """
    index: dict[str, set[str]] = {}
    for test_path in _tracked_top_level_test_files():
        tree = _parse_tracked(test_path)
        if _file_level_gating_markers(tree):
            continue
        for name in _top_level_imported_module_names(test_path, tree):
            index.setdefault(name, set()).add(test_path)
    return index


def _directory_rule_audit_modules() -> list[str]:
    """Every tracked module under the nine audited directory paths.

    Includes modules the directory rules never actually reach because an earlier
    `stop_on_match` rule owns them (chain.py, scheduler.py, cli.py,
    direct_grid_contract.py...). Their gaps are gaps all the same — the
    disposition question is about the module, not about which rule answers it.
    """
    modules: set[str] = set()
    for directory in DIRECTORY_RULE_AUDIT_PATHS:
        modules.update(_tracked_python_files(f"{directory}/*.py"))
    return sorted(modules)


def _importer_equivalence_offenders(
    *,
    modules: Sequence[str],
    index: dict[str, set[str]],
    authority: Callable[[str], set[str]] = _non_gated_top_level_importer_tests,
) -> list[str]:
    """Dotted modules where the executed index and the authority helper disagree.

    Both sides are parameters so the divergence can be shown on constructed data
    — the tracked tree agrees today, and a pin whose red arm needs a mutated tree
    is a pin nobody re-runs.
    """
    offenders: list[str] = []
    for module in modules:
        expected = authority(module)
        actual = index.get(module, set())
        if expected != actual:
            offenders.append(f"{module}: authority helper {sorted(expected)} != executed index {sorted(actual)}")
    return offenders


EQUIVALENCE_PIN_PRODUCTION_SAMPLE_SIZE = 3


def _importer_equivalence_sample() -> tuple[list[str], list[str]]:
    """``(support half, production half)`` — derived, never a frozen list.

    Every tracked `tests/` support module is in: that is the live domain of the
    closure guard, which reads the index for exactly these. The production half
    comes off the head of the already-sorted `_directory_rule_audit_modules()`
    (159 modules, 0.07 s) rather than the #1455 gap-map universe, which by
    construction only contains modules the index ALREADY reports an importer for
    — biased away from the regression this pin exists to catch, an index that
    quietly stops seeing a module.

    Returned as two halves rather than one concatenation so the pin can check
    each against its own source: a support half shrinking and a production head
    slice emptying are different regressions, and a single length floor over the
    union passes through both.
    """
    support = [_dotted_module_name(path) for path in _tracked_tests_support_modules()]
    production = [
        _dotted_module_name(path)
        for path in _directory_rule_audit_modules()[:EQUIVALENCE_PIN_PRODUCTION_SAMPLE_SIZE]
    ]
    return support, production


def test_importer_index_equals_the_per_module_authority_helper() -> None:
    # #1499: one relation, two implementations —
    # `_non_gated_top_level_importer_tests` is the semantic authority and
    # `_non_gated_top_level_importer_index` is the affordable executed form that
    # the #1487 closure guard and the #1455 disposition guard both run on. The
    # claim that they agree lived only in a docstring, so widening a marker
    # filter or an import predicate on one side moved two guards' domains with
    # nothing to say so. One index build for the whole comparison; the helper is
    # the expensive side (it re-parses every test file per call), which is why
    # the production sample is a head slice rather than the whole directory
    # audit.
    #
    # Sample integrity is checked per half, each against a source independent of
    # the tree derivation that built it: the support half must still contain
    # every module SUPPORT_MODULE_TEST_RULES routes (a tracked table, so an 8->1
    # shrink in `_tracked_tests_support_modules` reds here instead of shrinking
    # the compared domain in silence), and the production head slice must still
    # be the full slice.
    support, production = _importer_equivalence_sample()
    sample = support + production
    routed = {_dotted_module_name(rule.pattern) for rule in SUPPORT_MODULE_TEST_RULES}
    assert routed <= set(support), (
        "support half no longer covers every module the routing table routes, missing "
        f"{sorted(routed - set(support))}"
    )
    assert len(production) == EQUIVALENCE_PIN_PRODUCTION_SAMPLE_SIZE, (
        f"production half is {production}, expected {EQUIVALENCE_PIN_PRODUCTION_SAMPLE_SIZE} modules "
        "off the head of the directory-rule audit"
    )
    index = _non_gated_top_level_importer_index()

    offenders = _importer_equivalence_offenders(modules=sample, index=index)
    assert not offenders, "importer derivation authority and executed index disagree:\n  " + "\n  ".join(offenders)
    # Anti-vacuity: two derivations that both return nothing agree perfectly.
    assert any(index.get(module) for module in sample), f"no sampled module derives any importer suite: {sample}"


def test_importer_equivalence_pin_reds_when_the_two_derivations_diverge() -> None:
    # Constructed on both sides, tracked tree untouched: the pin's content is the
    # comparison, not today's agreement. The failure must print BOTH sets — "the
    # derivations diverge" alone leaves the next reader unable to tell which side
    # moved, which is the whole question when one of them gates two guards.
    module = "packages.common.probe"
    kept_by_the_helper = "tests/test_probe_authority.py"

    offenders = _importer_equivalence_offenders(
        modules=[module],
        index={module: set()},
        authority=lambda _module: {kept_by_the_helper},
    )

    assert offenders == [f"{module}: authority helper {[kept_by_the_helper]} != executed index []"]


def _selection_for_module(module_path: str) -> set[str]:
    return set(select_tests([module_path], repo_root=Path(".")))


def _directory_rule_importer_map(
    *,
    select: Callable[[str], set[str]] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(universe, gaps)`` for the audited directories.

    ``universe`` maps each audited module with at least one non-gated top-level
    importer suite to that PRE-subtraction importer set; ``gaps`` maps it to the
    importer suites a PR touching only that module does not select.

    "Selected" is judged at FILE level on purpose: a suite the selector reaches
    only through `::`-qualified node ids is partially covered, not covered, and
    normalizing the `::` away would silently shrink this map. Those pairs are
    real gaps that the exclusion table dispositions as `redirect`.

    ``select`` is the seam: `select_tests` reads the module-global rule table, so
    red evidence for "this exclusion went stale because a rule now selects it"
    needs a constructed selection callable rather than a mutated tree.
    """
    resolve = _selection_for_module if select is None else select
    index = _non_gated_top_level_importer_index()
    universe: dict[str, set[str]] = {}
    gaps: dict[str, set[str]] = {}
    for module_path in _directory_rule_audit_modules():
        importers = index.get(_dotted_module_name(module_path), set())
        if not importers:
            continue
        universe[module_path] = importers
        missing = importers - resolve(module_path)
        if missing:
            gaps[module_path] = missing
    return universe, gaps


def _rule_selected_test_files(rules: Sequence[PathTestRule], *, not_matching: str) -> set[str]:
    """Test FILES some rule selects whole, excluding rules matching ``not_matching``.

    Node ids do not count (a `::`-qualified target is partial coverage, the same
    convention the gap derivation uses). ``not_matching`` is a module path whose
    OWN rules are dropped from the union: `edge-consumer` claims the suite is
    selected from ANOTHER surface, and a rule matching the module is that
    module's own surface, not another one. `fnmatch` is the selector's own rule
    matcher (`select_tests` uses it verbatim), so a pattern counts as the
    module's own here exactly when it counts as a match there.
    """
    return {
        target
        for rule in rules
        if not fnmatch.fnmatch(not_matching, rule.pattern)
        for target in rule.tests
        if "::" not in target
    }


def _disposition_offenders(
    *,
    exclusions: dict[tuple[str, str], str],
    gaps: dict[str, set[str]],
    rules: Sequence[PathTestRule] = PATH_TEST_RULES,
    select: Callable[[str], set[str]] | None = None,
) -> list[str]:
    """Everything wrong with ``exclusions`` against the derived gap map.

    All four inputs are parameters so the failure modes below can be shown on
    constructed data — a gap map derived through a selection callable that
    selects more, an entry with a bad token, a missing entry, a rule list where
    only the module's own rule carries the suite, a selection with the redirect
    node ids removed — without touching a tracked file or a module global. The
    caller passes the gap map in rather than having this derive it, so the guard
    below pays for one derivation instead of two.
    """
    resolve = _selection_for_module if select is None else select
    derived = {(module, suite) for module, suites in gaps.items() for suite in suites}
    offenders: list[str] = []

    for pair, token in sorted(exclusions.items()):
        if token not in RULE_GAP_REASON_TOKENS:
            offenders.append(f"{pair[0]} -> {pair[1]}: invalid reason token {token!r}")
    offenders.extend(
        f"{module} -> {suite}: gap is neither selected by a rule nor excluded"
        for module, suite in sorted(derived - set(exclusions))
    )
    offenders.extend(
        f"{module} -> {suite}: stale exclusion — the pair no longer derives as a gap"
        for module, suite in sorted(set(exclusions) - derived)
    )
    offenders.extend(
        f"{module} -> {suite}: orphan edge-consumer — no rule outside the module's own selects that suite"
        for (module, suite), token in sorted(exclusions.items())
        if token == "edge-consumer" and suite not in _rule_selected_test_files(rules, not_matching=module)
    )
    offenders.extend(
        f"{module} -> {suite}: dead redirect — the module's selection no longer reaches the suite via node ids"
        for (module, suite), token in sorted(exclusions.items())
        if token == "redirect" and not any(target.startswith(f"{suite}::") for target in resolve(module))
    )
    return offenders


def test_directory_rule_importer_gaps_are_dispositioned() -> None:
    # The #1452 audit of these nine directory rules lived in a PR body, where a
    # new importer suite silently invalidated it. Here every gap pair must be
    # answered: closed by a rule, or named in the exclusion table with a token.
    # An exclusion that stops deriving as a gap is just as loud as an
    # undispositioned one — that is the anti-rot half.
    universe, gaps = _directory_rule_importer_map()

    # Anti-vacuity anchored on the PRE-subtraction universe, never on the
    # residual gap set: the good outcome shrinks the gaps toward zero, so a gap
    # count is exactly the wrong floor. What must stay nonzero is the importer
    # relation the derivation reads — and every audited directory must still
    # contribute a module to it, so a broken pathspec or AST regression that
    # blanks one directory reds instead of passing quietly.
    assert universe, "derived no non-gated importer suites for any audited module"
    silent = [
        directory
        for directory in DIRECTORY_RULE_AUDIT_PATHS
        if not any(module.startswith(f"{directory}/") for module in universe)
    ]
    assert not silent, f"audited directories contributing no importer pairs: {silent}"

    offenders = _disposition_offenders(exclusions=INTENTIONAL_RULE_GAP_EXCLUSIONS, gaps=gaps)
    assert not offenders, "directory-rule importer gaps undispositioned:\n  " + "\n  ".join(offenders)


def test_disposition_guard_reds_on_an_undispositioned_gap() -> None:
    # Constructed input, not a tracked mutation: drop one live entry and the
    # guard must name that exact pair. Without this the guard could be reporting
    # "no offenders" because it derives nothing.
    pair = next(iter(sorted(INTENTIONAL_RULE_GAP_EXCLUSIONS)))
    without_entry = {key: value for key, value in INTENTIONAL_RULE_GAP_EXCLUSIONS.items() if key != pair}

    offenders = _disposition_offenders(exclusions=without_entry, gaps=_directory_rule_importer_map()[1])

    assert offenders == [f"{pair[0]} -> {pair[1]}: gap is neither selected by a rule nor excluded"]


def test_disposition_guard_reds_on_a_stale_exclusion() -> None:
    # Two ways an entry goes stale, both shown through the injectable selection
    # seam so the live rule table stays untouched: the pair vanishes from the
    # tree, and a rule grows to select it.
    pair = next(iter(sorted(INTENTIONAL_RULE_GAP_EXCLUSIONS)))
    vanished = ("services/orchestrator/module_that_left_the_tree.py", "tests/test_gone.py")

    offenders = _disposition_offenders(
        exclusions={**INTENTIONAL_RULE_GAP_EXCLUSIONS, vanished: "redirect"},
        gaps=_directory_rule_importer_map()[1],
    )
    assert f"{vanished[0]} -> {vanished[1]}: stale exclusion — the pair no longer derives as a gap" in offenders

    def now_selects_the_pair(module_path: str) -> set[str]:
        selection = _selection_for_module(module_path)
        return selection | {pair[1]} if module_path == pair[0] else selection

    offenders = _disposition_offenders(
        exclusions=INTENTIONAL_RULE_GAP_EXCLUSIONS,
        gaps=_directory_rule_importer_map(select=now_selects_the_pair)[1],
    )
    assert offenders == [f"{pair[0]} -> {pair[1]}: stale exclusion — the pair no longer derives as a gap"]


def test_disposition_guard_reds_on_an_invalid_reason_token() -> None:
    # The token vocabulary is the whole disposition contract; a free-text reason
    # would let any pair be waved through under a word nobody reviewed.
    pair = next(iter(sorted(INTENTIONAL_RULE_GAP_EXCLUSIONS)))
    mistyped = {**INTENTIONAL_RULE_GAP_EXCLUSIONS, pair: "slow"}

    offenders = _disposition_offenders(exclusions=mistyped, gaps=_directory_rule_importer_map()[1])

    assert offenders == [f"{pair[0]} -> {pair[1]}: invalid reason token 'slow'"]


def test_disposition_guard_reds_on_an_orphan_edge_consumer() -> None:
    # `edge-consumer` claims another rule owns the suite. When no rule selects
    # it, the claim is false and the pair is simply uncovered — the routing must
    # not be able to launder that. Shown on a constructed rule list so the live
    # table is not touched.
    orphan_candidates = sorted(
        pair for pair, token in INTENTIONAL_RULE_GAP_EXCLUSIONS.items() if token == "edge-consumer"
    )
    assert orphan_candidates, "expected at least one edge-consumer routing to exercise the orphan check"
    pair = orphan_candidates[0]
    rules_without_that_suite = tuple(
        PathTestRule(
            rule.pattern,
            tuple(target for target in rule.tests if target != pair[1]),
            stop_on_match=rule.stop_on_match,
            only_when_any_changed=rule.only_when_any_changed,
        )
        for rule in PATH_TEST_RULES
    )

    offenders = _disposition_offenders(
        exclusions=INTENTIONAL_RULE_GAP_EXCLUSIONS,
        gaps=_directory_rule_importer_map()[1],
        rules=rules_without_that_suite,
    )

    assert (
        f"{pair[0]} -> {pair[1]}: orphan edge-consumer — no rule outside the module's own selects that suite"
        in offenders
    )


def test_disposition_guard_reds_when_only_the_modules_own_rule_carries_the_suite() -> None:
    # The sharper half of the orphan check, and the reason it is not "any rule
    # in the table": a suite carried ONLY by a rule that matches the excluded
    # module is not another surface's — it is the module's own, and if that rule
    # is shadowed by an earlier stop rule the coverage is zero while the routing
    # reads fine. Fully constructed inputs (module, suite, rules, gap map), so
    # nothing here depends on the live table.
    module = "services/orchestrator/persistence.py"
    suite = "tests/test_probe_edge_consumer.py"
    own_rule_only = (PathTestRule("services/orchestrator/**", (suite,)),)

    offenders = _disposition_offenders(
        exclusions={(module, suite): "edge-consumer"},
        gaps={module: {suite}},
        rules=own_rule_only,
    )

    assert offenders == [
        f"{module} -> {suite}: orphan edge-consumer — no rule outside the module's own selects that suite"
    ]

    # Same table, same gap, one rule for a surface that is genuinely elsewhere:
    # now the routing claim is true and the guard passes.
    with_another_surface = (*own_rule_only, PathTestRule("services/slurm_gateway/**", (suite,)))
    assert not _disposition_offenders(
        exclusions={(module, suite): "edge-consumer"},
        gaps={module: {suite}},
        rules=with_another_surface,
    )


def test_disposition_guard_reds_when_a_redirect_no_longer_reaches_the_suite() -> None:
    # `redirect` claims the module still reaches the suite through `::` node
    # ids. Deleting those node ids from a shared tuple (or renaming the tests
    # behind them) zeroes the coverage while the entry keeps reading like a
    # deliberate routing — the one token whose claim had no anchor before. The
    # deletion is modelled through the injectable selection seam, so the live
    # ORCHESTRATOR_MANIFEST_SURFACE_TESTS tuple stays untouched.
    module, suite = "services/orchestrator/chain.py", "tests/test_orchestration_chain.py"
    assert INTENTIONAL_RULE_GAP_EXCLUSIONS[(module, suite)] == "redirect"
    assert any(target.startswith(f"{suite}::") for target in _selection_for_module(module))

    def selection_without_the_node_ids(module_path: str) -> set[str]:
        return {target for target in _selection_for_module(module_path) if not target.startswith(f"{suite}::")}

    offenders = _disposition_offenders(
        exclusions={(module, suite): "redirect"},
        gaps={module: {suite}},
        select=selection_without_the_node_ids,
    )

    assert offenders == [
        f"{module} -> {suite}: dead redirect — the module's selection no longer reaches the suite via node ids"
    ]


# The positive-selection FLOOR. The disposition guard above is, on its own,
# satisfiable by 211 exclusions and zero rule growth — which would re-create
# exactly the rotted-audit failure #1455 exists to end. These pins name the
# audit's confirmed same-subject gaps and demand SELECTION, so an
# all-exclusions delivery reds here even while the guard stays green. A floor,
# not a ceiling: the guard still governs everything not listed.
#
# Two entries deviate from the #1455 fixture's wording, which named a
# "same-name suite" for every orchestrator module: `tests/test_persistence.py`
# and `tests/test_reconcile.py` do not exist in the tree. The nearest real
# same-subject suites are pinned instead (test_pipeline_persistence.py for
# persistence.py; test_reconcile_sacct_parse.py for reconcile.py, which is the
# suite the #1486 one-hop closure already ties to reconcile.py).
POSITIVE_SELECTION_FLOOR: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("services/tile_publisher/publisher.py", ("tests/test_cli_publish_qdown.py",)),
    (
        "workers/output_parser/cli.py",
        ("tests/test_output_parser_cli.py", "tests/test_output_parser_dual_write.py"),
    ),
    (
        "workers/output_parser/parser.py",
        ("tests/test_output_parser_cli.py", "tests/test_output_parser_dual_write.py"),
    ),
    (
        "workers/shud_runtime/runtime.py",
        ("tests/test_warm_start.py", "tests/test_warm_start_chaining.py"),
    ),
    (
        "services/slurm_gateway/app.py",
        ("tests/test_role_boundary_static.py", "tests/test_monitoring_api.py"),
    ),
    ("services/slurm_gateway/gateway.py", ("tests/test_retry_cancel_consistency.py",)),
    ("services/orchestrator/persistence.py", ("tests/test_pipeline_persistence.py",)),
    ("services/orchestrator/retry.py", ("tests/test_retry.py",)),
    ("services/orchestrator/reconcile.py", ("tests/test_reconcile_sacct_parse.py",)),
    ("services/orchestrator/scheduler_generation.py", ("tests/test_scheduler_generation.py",)),
    ("services/orchestrator/scheduler_timing.py", ("tests/test_scheduler_timing.py",)),
    ("services/orchestrator/replay_lineage.py", ("tests/test_replay_lineage.py",)),
    ("services/orchestrator/retention.py", ("tests/test_retention.py",)),
    ("services/orchestrator/run_tree_copyback.py", ("tests/test_run_tree_copyback.py",)),
)


@pytest.mark.parametrize(
    ("module_path", "required"),
    POSITIVE_SELECTION_FLOOR,
    ids=[module_path for module_path, _ in POSITIVE_SELECTION_FLOOR],
)
def test_directory_rule_disposition_selects_the_audit_floor(module_path: str, required: tuple[str, ...]) -> None:
    assert Path(module_path).is_file(), f"floor pin names a module that is not in the tree: {module_path}"
    for suite in required:
        assert Path(suite).is_file(), f"floor pin names a suite that is not in the tree: {suite}"
        assert not _file_level_gating_markers(_parse_tracked(suite)), (
            f"floor pin names a file-level gated suite (it would skip in the PR lane): {suite}"
        )

    selected = set(select_tests([module_path], repo_root=Path(".")))

    missing = sorted(set(required) - selected)
    assert not missing, f"{module_path}: rules stopped selecting audit-floor suites {missing}"


# Modules whose additions had to be placed with `stop_on_match` in mind: an
# earlier stop rule owns each of these paths, so a narrow rule appended at the
# end of PATH_TEST_RULES would never fire for them. Each addition therefore
# extends its OWNING rule at that rule's site, and this pin proves the targets
# really arrive rather than being amputated by the stop.
STOP_RULE_AT_SITE_EXTENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("services/orchestrator/chain.py", CHAIN_IMPORTER_TESTS),
    ("services/orchestrator/scheduler.py", SCHEDULER_IMPORTER_TESTS),
    ("services/orchestrator/cli.py", ORCHESTRATOR_CLI_IMPORTER_TESTS),
    ("services/orchestrator/file_orchestration_journal.py", FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS),
    ("workers/forcing_producer/direct_grid_contract.py", DIRECT_GRID_CONTRACT_IMPORTER_TESTS),
)


@pytest.mark.parametrize(
    ("module_path", "added"),
    STOP_RULE_AT_SITE_EXTENSIONS,
    ids=[module_path for module_path, _ in STOP_RULE_AT_SITE_EXTENSIONS],
)
def test_at_site_rule_extensions_survive_stop_on_match_ordering(module_path: str, added: tuple[str, ...]) -> None:
    selected = set(select_tests([module_path], repo_root=Path(".")))

    missing = sorted(set(added) - selected)
    assert not missing, f"{module_path}: at-site additions never reached the selection (stop rule ordering?): {missing}"


def test_at_site_extensions_did_not_widen_the_stop_rules() -> None:
    # The other half of the ordering contract: the stop rules must still STOP.
    # If an at-site extension had been written as a new rule placed later, or a
    # stop flag had been dropped, these paths would start collecting the broad
    # directory rule's targets — which is the runtime blowup #1455's design
    # calls the primary risk. `test_state_clone.py` is a target only the broad
    # `services/orchestrator/**` rule carries, so it is the tell.
    broad_only_target = "tests/test_state_clone.py"
    assert broad_only_target in select_tests(["services/orchestrator/persistence.py"], repo_root=Path("."))

    for module_path, _ in STOP_RULE_AT_SITE_EXTENSIONS:
        if not module_path.startswith("services/orchestrator/"):
            continue
        selected = select_tests([module_path], repo_root=Path("."))
        assert broad_only_target not in selected, (
            f"{module_path} now reaches the broad services/orchestrator/** rule: a stop rule stopped stopping"
        )


# --------------------------------------------------------------------------
# tests/ support-module importer routing closure (#1487)
#
# Before #1487 a PR touching only a `tests/` support module ran the meta-guard
# suite plus ci.yml's full-tree collect-only smoke: import/syntax only, zero
# assertions on the suites that actually consume the fixture. That is the
# #1191 -> #1247 -> #1283 -> #1447 rot shape one more time, so the routing exists
# and this guard keeps it complete FROM THE TREE — the required sets are derived
# here, never frozen, so a new importer suite reds naming the module and the
# suite instead of quietly falling out of the PR lane.
#
# DOMAIN SPLIT, three guards, no overlap claimed:
#   * this one            -> tracked non-suite modules under `tests/`
#   * the #1455 guard      -> the nine audited production directories
#     (test_directory_rule_importer_gaps_are_dispositioned), direct importers
#     only, dispositioned through INTENTIONAL_RULE_GAP_EXCLUSIONS
#   * the #1486 guard      -> GUARDED_MODULE_CLOSURES, direct UNION one hop
# Each keeps its own derivation and its own exemption vocabulary on purpose.
# --------------------------------------------------------------------------

# One known member of each routed module's derived importer set. The anti-vacuity
# floor per module (the GUARDED_MODULE_CLOSURES pattern): an aggregate count can
# stay plausible while a half-blanked derivation empties one module's set, and
# then the rule for it is unfalsifiable.
SUPPORT_MODULE_ROUTING_ANCHORS: tuple[tuple[str, str], ...] = (
    (
        "tests/fixtures/mapping_builder/in_memory_grid_snapshot.py",
        "tests/test_mapping_builder_algorithm.py",
    ),
    ("tests/slurm_template_helpers.py", "tests/test_production_slurm_validation.py"),
    ("tests/river_identity_backfill_fakes.py", "tests/test_node27_river_identity_backfill.py"),
    ("tests/__init__.py", "tests/test_integration_gate.py"),
    # The literal-path half (#1498): this pair exists only because
    # test_shud_runtime.py carries the exact string "tests/mock_shud_omp.py" and
    # runs it as a subprocess. It imports nothing from the module, so a
    # derivation that loses the consumption edge empties this anchor and reds
    # here rather than quietly re-collapsing the module.
    ("tests/mock_shud_omp.py", "tests/test_shud_runtime.py"),
)

# At least this many support modules must derive a non-empty consumer set (7 of 8
# today — 6 by import, plus mock_shud_omp by literal path). A pure "universe
# non-empty" floor survives a derivation that collapses to a single lucky module.
MIN_SUPPORT_MODULES_WITH_IMPORTERS = 3

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
# The `database:` key inside ci.yml's `filters: |` block literal. Filter keys sit
# at 12 spaces and their entries at 14, which is what makes the block sliceable.
_DATABASE_FILTER_KEY = "\n            database:\n"
_NEXT_FILTER_KEY_LINE = re.compile(r"^ {12}\S", re.MULTILINE)


# The literal-path scan source deliberately EXCLUDES this suite (#1498): it
# enumerates support-module paths as DATA — routing anchors, the carve-out
# allowlist, guard fixtures — so scanning it would register it as a "consumer" of
# every support module, empty the zero-consumer domain the collapse route is
# asserted on, and neuter the anti-vacuity floor. The edge would carry no
# information either way: the meta-guard rider already joins every routed
# selection. Pinned by
# test_literal_path_scan_excludes_the_meta_guard_suite_that_lists_paths_as_data.
LITERAL_CONSUMER_SCAN_EXCLUSIONS = frozenset({SELECTOR_META_GUARD_TEST})


def _literal_path_consumer_index(
    *,
    suites: Sequence[str] | None = None,
    targets: Sequence[str] | None = None,
) -> dict[str, set[str]]:
    """Inverted index: support-module PATH -> non-gated suites consuming it by literal.

    The second edge kind (#1498). `tests/mock_shud_omp.py` is a mock CLI that
    `workers/shud_runtime/runtime.py` runs as `[sys.executable, <path>, *args]`;
    no suite imports it, so an import-only derivation reads it as 0-importer and
    collapses a mock-only PR to the meta-guard while every assertion depending on
    its output contract sits unrun. A suite carrying the module's exact
    repo-relative path as a string constant is consuming it, and that is an edge.

    Keyed by repo-relative PATH, not by dotted name: these modules are executed,
    not imported, so a dotted name is the wrong identity for them.

    EXACT full-path equality only. Basename or suffix matching is rejected: it
    would manufacture phantom edges (any string ending in `build.py`) and force
    spurious rules. The accepted cost is the mirror-image false negative — a
    future consumer spelling `Path(__file__).parent / "mock_shud_omp.py"`
    materializes no full-path constant and derives no edge. All live consumption
    sites today spell the full literal.

    Same shape as `_non_gated_top_level_importer_index`: one AST pass over the
    tracked top-level suites, same file-level gating-marker filter (a
    file-gated consumer would skip in the PR lane, so routing it buys constant
    skips). Implemented ONCE, index-form only, with no per-module authority twin
    — that split is exactly what #1499 had to pin after the fact.

    Both parameters are seams for constructed red evidence; the defaults are the
    tracked tree minus `LITERAL_CONSUMER_SCAN_EXCLUSIONS`.
    """
    scanned = (
        [path for path in _tracked_top_level_test_files() if path not in LITERAL_CONSUMER_SCAN_EXCLUSIONS]
        if suites is None
        else list(suites)
    )
    wanted = set(_tracked_tests_support_modules() if targets is None else targets)
    index: dict[str, set[str]] = {}
    for test_path in scanned:
        tree = _parse_tracked(test_path)
        if _file_level_gating_markers(tree):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in wanted:
                index.setdefault(node.value, set()).add(test_path)
    return index


def _derived_support_module_importers(modules: Sequence[str]) -> dict[str, set[str]]:
    """``module path -> derived non-gated consumer suites``, both edge kinds.

    Semantic authority for the IMPORT half is
    `_non_gated_top_level_importer_tests` — same predicate, same file-level
    marker filter, same package-`__init__`-to-package aliasing (which is why
    `tests/__init__.py` derives the three suites that spell `from tests import
    ...`). It is EXECUTED through the inverted index because the per-module
    helper re-parses every test file on each call: 8 calls measured 8.35 s
    against 0.99 s for one index pass, for an identical result (equivalence
    pinned by test_importer_index_equals_the_per_module_authority_helper).

    UNIONED with the literal-path consumption half (#1498), which is where a
    subprocess-executed module like `tests/mock_shud_omp.py` becomes visible. The
    union happens HERE and only here: `_non_gated_top_level_importer_index`'s
    return value stays purely import-derived because the #1455 disposition guard
    and the #1499 equivalence pin both consume it raw.
    """
    index = _non_gated_top_level_importer_index()
    consumers = _literal_path_consumer_index()
    return {
        module: index.get(_dotted_module_name(module), set()) | consumers.get(module, set()) for module in modules
    }


def _zero_consumer_support_modules() -> list[str]:
    """Support modules reaching no suite by EITHER edge kind — the collapse route."""
    modules = _tracked_tests_support_modules()
    derived = _derived_support_module_importers(modules)
    return sorted(module for module in modules if not derived[module])


def _zero_consumer_collapse_params() -> list[object]:
    """`pytest.param` cases for the collapse route, derived from the tracked tree.

    An empty derived set is a LEGAL terminal state — every support module
    consumed by something — but the two obvious spellings both fail it: a bare
    `assert sample` false-reds a legal tree, and a zero-case parametrize deletes
    the guard without leaving a trace in the run. So it collapses to one
    skip-marked case naming the decision that is then owed.

    Collection-time cost: this runs one import-index build plus one literal-path
    scan (~2 s) whenever the module is collected, including under ci.yml's
    full-tree `--collect-only` smoke.
    """
    sample = _zero_consumer_support_modules()
    if not sample:
        return [
            pytest.param(
                "",
                marks=pytest.mark.skip(
                    reason="zero-consumer domain is empty — collapse-route guard needs re-decision"
                ),
                id="<no-zero-consumer-support-module>",
            )
        ]
    return [pytest.param(module) for module in sample]


def _support_module_closure_offenders(
    *,
    modules: Sequence[str],
    derived: dict[str, set[str]],
    select: Callable[[str], set[str]] | None = None,
    carveouts: frozenset[str] = ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES,
    consumer_edges: dict[str, set[str]] | None = None,
) -> list[str]:
    """Support modules whose selection does not match their derived consumers.

    Every input is a parameter so each failure mode below is provable on
    constructed data — a derived map carrying a suite no rule routes, a selection
    callable that adds a target to a 0-importer module, an empty carve-out set —
    without mutating a tracked file or a module global.

    ``consumer_edges`` is the LABEL source only, defaulting to the real
    literal-path index: a missing pair reads "literal-path consumer suite" when
    that is how the suite reaches the module and "importer suite" otherwise, so
    the reader knows which derivation to go look at. The derived sets still come
    exclusively from ``derived``, which keeps the every-input-is-a-parameter
    property intact — the label source is itself a parameter, and a constructed
    pair absent from it simply falls back to the import wording.

    That default index is a full-tree AST scan (~2 s), so it is built LAZILY, on
    the first offender that needs a label: a green run has no offenders and pays
    nothing, while a run that produces one still labels off the real index, so
    the wording is exactly what it was when the build was eager.
    """
    resolve = _selection_for_module if select is None else select
    labels = consumer_edges
    offenders: list[str] = []
    for module in modules:
        if module in carveouts:
            continue
        importers = derived.get(module, set())
        selection = resolve(module)
        if importers:
            missing = sorted(importers - selection)
            if not missing:
                continue
            if labels is None:
                labels = _literal_path_consumer_index()
            literal_consumers = labels.get(module, set())
            offenders.extend(
                f"{module} -> {suite}: "
                + (
                    "literal-path consumer suite is not selected"
                    if suite in literal_consumers
                    else "derived non-gated importer suite is not selected"
                )
                for suite in missing
            )
        elif selection != {SELECTOR_META_GUARD_TEST}:
            offenders.append(
                f"{module}: derives no importer suites but selects {sorted(selection)} "
                f"instead of only {SELECTOR_META_GUARD_TEST}"
            )
    return offenders


def _database_filter_block(workflow: str) -> str:
    """ci.yml's `database:` paths-filter block, from its key to the next key.

    Block-scoped rather than a whole-file grep: `tests/conftest.py` appearing
    under some other filter says nothing about whether `real-db-integration`
    starts for it, and that job IS the factual predicate the carve-out cites.
    ``workflow`` is TEXT so the red path can feed a constructed workflow.
    """
    start = workflow.find(_DATABASE_FILTER_KEY)
    assert start != -1, f"{CI_WORKFLOW_PATH} no longer defines a `database:` paths-filter block"
    body_start = start + len(_DATABASE_FILTER_KEY)
    following = _NEXT_FILTER_KEY_LINE.search(workflow, body_start)
    return workflow[body_start : following.start() if following else len(workflow)]


def _carveout_filter_pin_offenders(
    *,
    workflow: str,
    carveouts: frozenset[str] = ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES,
) -> list[str]:
    block = _database_filter_block(workflow)
    return [
        f"{path}: carve-out is not listed in the ci.yml `database:` filter block"
        for path in sorted(carveouts)
        if f"- '{path}'" not in block
    ]


def test_tests_support_module_rules_cover_their_non_gated_importer_closure() -> None:
    # THE guard. A support module either routes to its derived importer suites,
    # or derives none and keeps the meta-guard collapse, or is a recorded
    # carve-out. Derived from the tracked tree on every run, so the rule table
    # cannot go stale in silence.
    modules = _tracked_tests_support_modules()
    assert modules, "expected tracked tests/ modules that are not test_*.py suites"

    derived = _derived_support_module_importers(modules)

    # Anti-vacuity, on the PRE-carve-out universe: the guard's whole content is
    # the importer relation, and a derivation that breaks into silence (bad
    # pathspec, AST regression, a marker filter gone wide) would otherwise pass
    # with every module looking like a 0-importer collapse.
    with_importers = sorted(module for module, importers in derived.items() if importers)
    assert with_importers, "derived no non-gated importer suites for any tests/ support module"
    assert len(with_importers) >= MIN_SUPPORT_MODULES_WITH_IMPORTERS, (
        f"expected at least {MIN_SUPPORT_MODULES_WITH_IMPORTERS} support modules with derived importers, "
        f"got {with_importers}"
    )
    assert {module for module, _ in SUPPORT_MODULE_ROUTING_ANCHORS} == {
        rule.pattern for rule in SUPPORT_MODULE_TEST_RULES
    }, "SUPPORT_MODULE_ROUTING_ANCHORS drifted from the routing table"
    for module, anchor in SUPPORT_MODULE_ROUTING_ANCHORS:
        assert anchor in derived.get(module, set()), (
            f"{module}: expected {anchor} among derived importers, got {sorted(derived.get(module, set()))}"
        )

    offenders = _support_module_closure_offenders(modules=modules, derived=derived)
    assert not offenders, "tests/ support-module importer closure incomplete:\n  " + "\n  ".join(offenders)


def test_support_module_closure_guard_reds_on_a_missing_importer_suite() -> None:
    # The rot this exists to catch: a new suite starts importing a routed fixture
    # at file level and nobody extends the rule. Modelled on a constructed derived
    # map — the tracked tree is untouched — and the failure must NAME both ends,
    # because "closure incomplete" alone tells the next reader nothing.
    module, _ = SUPPORT_MODULE_ROUTING_ANCHORS[0]
    newcomer = "tests/test_mapping_builder_probe.py"

    offenders = _support_module_closure_offenders(modules=[module], derived={module: {newcomer}})

    assert offenders == [f"{module} -> {newcomer}: derived non-gated importer suite is not selected"]


def test_support_module_closure_guard_reds_on_a_dropped_consumption_edge() -> None:
    # The #1498 rot direction: a subprocess consumer lands (or the mock's rule is
    # dropped) and the module re-collapses to the meta-guard while every
    # assertion depending on its output contract sits unrun. Constructed
    # selection, tracked tree untouched.
    #
    # The wording is the point of the consumer_edges label parameter: a reader
    # who greps the named suite for an import of the module finds none and
    # concludes the guard is broken. "literal-path consumer suite" sends them to
    # the right derivation.
    module = "tests/mock_shud_omp.py"
    derived = _derived_support_module_importers([module])
    assert derived[module], "mock_shud_omp derives no consumer suites — the union lost its literal-path half"

    offenders = _support_module_closure_offenders(
        modules=[module],
        derived=derived,
        select=lambda _module: {SELECTOR_META_GUARD_TEST},
    )

    assert offenders == [
        f"{module} -> {suite}: literal-path consumer suite is not selected" for suite in sorted(derived[module])
    ]


def test_support_module_closure_guard_reds_on_a_gratuitous_zero_importer_selection() -> None:
    # The other direction, and the reason branch (c) is an EQUALITY: a module
    # nothing reaches — by import OR by literal path — must keep the one-element
    # collapse that arms ci.yml's full-tree collect-only smoke (#1454). Widening
    # it to unrelated suites trades that smoke for whatever the rule happened to
    # name.
    #
    # The module is DERIVED rather than named: mock_shud_omp.py stood here until
    # #1498 gave the derivation its literal-path edge and moved it off this
    # route. Any zero-consumer module proves the branch; if the tree ever has
    # none (a legal terminal state) the branch is proven on a constructed path,
    # since select_tests maps any unrouted non-suite `tests/` path to the
    # meta-guard, which is the collapse under test.
    module = next(iter(_zero_consumer_support_modules()), "tests/fixtures/zero_consumer_probe.py")
    unrelated = "tests/test_gateway.py"

    def selects_something_extra(module_path: str) -> set[str]:
        return _selection_for_module(module_path) | {unrelated}

    offenders = _support_module_closure_offenders(
        modules=[module],
        derived={module: set()},
        select=selects_something_extra,
    )

    assert offenders == [
        f"{module}: derives no importer suites but selects {sorted([SELECTOR_META_GUARD_TEST, unrelated])} "
        f"instead of only {SELECTOR_META_GUARD_TEST}"
    ]


def test_support_module_carveout_is_load_bearing_not_decorative() -> None:
    # Honest labelling of the carve-out: with the allowlist emptied, both entries
    # red immediately — they have real derived importers that nothing selects.
    # The exemption is a recorded scope decision with PARTIAL external coverage
    # (see the allowlist comment), not a statement that the gap is closed.
    modules = sorted(ISSUE_1487_SCOPE_CARVEOUT_SUPPORT_MODULES)
    derived = _derived_support_module_importers(modules)

    assert not _support_module_closure_offenders(modules=modules, derived=derived)

    without_the_allowlist = _support_module_closure_offenders(
        modules=modules,
        derived=derived,
        carveouts=frozenset(),
    )
    assert {offender.split(" -> ", 1)[0] for offender in without_the_allowlist} == set(modules)


def test_carved_out_support_modules_are_pinned_in_the_ci_database_filter_block() -> None:
    # The carve-out cites one checkable fact — ci.yml's `database` filter lists
    # these paths, so `real-db-integration` starts for them. Pin it, or the
    # scope decision keeps reading fine long after its premise is gone.
    workflow = Path(CI_WORKFLOW_PATH).read_text(encoding="utf-8")

    assert not _carveout_filter_pin_offenders(workflow=workflow)


def test_carveout_filter_pin_reds_when_a_path_leaves_the_database_block() -> None:
    # Constructed workflow text, so the tracked ci.yml is untouched. Two shapes:
    # the entry deleted outright, and the entry moved under ANOTHER filter — the
    # second is why the pin slices the block instead of grepping the file, since
    # `tests/conftest.py` under `backend:` starts no database job.
    workflow = Path(CI_WORKFLOW_PATH).read_text(encoding="utf-8")
    entry = "              - 'tests/conftest.py'\n"
    assert entry in _database_filter_block(workflow)

    deleted = workflow.replace(entry, "")
    assert _carveout_filter_pin_offenders(workflow=deleted) == [
        "tests/conftest.py: carve-out is not listed in the ci.yml `database:` filter block"
    ]

    moved_to_backend = deleted.replace("            backend:\n", "            backend:\n" + entry)
    assert entry in moved_to_backend
    assert _carveout_filter_pin_offenders(workflow=moved_to_backend) == [
        "tests/conftest.py: carve-out is not listed in the ci.yml `database:` filter block"
    ]


def test_support_module_rule_patterns_are_distinct_exact_paths() -> None:
    # The routing table carries no `stop_on_match`, which is only safe while its
    # patterns cannot both match one path. Exact paths (no glob metacharacters)
    # plus uniqueness is that premise, asserted rather than assumed.
    patterns = [rule.pattern for rule in SUPPORT_MODULE_TEST_RULES]

    assert sorted(patterns) == sorted(set(patterns))
    assert not [pattern for pattern in patterns if set(pattern) & set("*?[")]
    assert all(not rule.stop_on_match and not rule.only_when_any_changed for rule in SUPPORT_MODULE_TEST_RULES)


@pytest.mark.parametrize(
    ("module_path", "required"),
    [(rule.pattern, rule.tests) for rule in SUPPORT_MODULE_TEST_RULES],
    ids=[rule.pattern for rule in SUPPORT_MODULE_TEST_RULES],
)
def test_routed_support_module_selects_its_importer_suites_and_the_meta_guard(
    module_path: str,
    required: tuple[str, ...],
) -> None:
    assert Path(module_path).is_file(), f"routing table names a module that is not in the tree: {module_path}"
    for suite in required:
        assert Path(suite).is_file(), f"routing table names a suite that is not in the tree: {suite}"
        # A file-level gated target would skip in the PR lane, buying constant
        # skips and zero assertions — the #1447 ruling, applied at authoring time
        # here and re-derived by the closure guard on every run.
        assert not _file_level_gating_markers(_parse_tracked(suite)), (
            f"routing table names a file-level gated suite (it would skip in the PR lane): {suite}"
        )

    selected = set(select_tests([module_path], repo_root=Path(".")))

    missing = sorted(set(required) - selected)
    assert not missing, f"{module_path}: routed importer suites not selected {missing}"
    # Decision 2: the meta-guards must run on the PR class that can invalidate
    # them, and a routed support-module PR is one — it can add the very importer
    # the closure guard above derives.
    assert SELECTOR_META_GUARD_TEST in selected
    assert selected != {SELECTOR_META_GUARD_TEST}


@pytest.mark.parametrize("module_path", _zero_consumer_collapse_params())
def test_zero_importer_support_modules_keep_the_meta_guard_collapse(module_path: str) -> None:
    # Issue #1487's acceptance named an example set for this route; the fixture
    # review corrected it once (`tests/__init__.py` derives three importer suites
    # under the repo's package-aliasing authority and is ROUTED), and #1498
    # corrected it again — mock_shud_omp.py left this sample the moment its
    # subprocess consumption became a derived edge. Hardcoding is what made both
    # corrections necessary, so the sample is now derived from the same union the
    # guard uses. Today it is exactly
    # tests/fixtures/mapping_builder/keliya/build.py: no suite imports it and no
    # scanned suite carries its path, matching its own docstring — "The test
    # suite reads the checked-in files directly and never invokes this script."
    assert Path(module_path).is_file()
    assert module_path not in {rule.pattern for rule in SUPPORT_MODULE_TEST_RULES}

    assert select_tests([module_path], repo_root=Path(".")) == [SELECTOR_META_GUARD_TEST]


def test_zero_consumer_collapse_sample_stays_visible_when_the_domain_empties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every support module having a consumer is a legal terminal state, and the
    # sample above is derived, so that state silently collects ZERO cases —
    # the collapse-route guard would disappear from the run with nothing saying
    # it had. The fallback is dead code until that day, which is precisely why it
    # is exercised here rather than trusted.
    monkeypatch.setattr(f"{__name__}._zero_consumer_support_modules", list)

    params = _zero_consumer_collapse_params()

    assert len(params) == 1
    marks = params[0].marks
    assert [mark.name for mark in marks] == ["skip"]
    assert "needs re-decision" in marks[0].kwargs["reason"]


def test_literal_path_consumption_edges_route_the_subprocess_only_mock() -> None:
    # #1498's acceptance. The gap it closes, asserted from both sides: the import
    # authority sees NOTHING here (nothing imports a mock CLI), and the module is
    # nevertheless consumed by three suites that hand its path to
    # `[sys.executable, <path>]`. Derived, not read off the rule table — a rule
    # extension that stops matching the derivation reds in the closure guard.
    module = "tests/mock_shud_omp.py"

    consumers = _literal_path_consumer_index().get(module, set())

    assert _non_gated_top_level_importer_tests(_dotted_module_name(module)) == set()
    assert consumers == {
        "tests/test_shud_runtime.py",
        "tests/test_direct_grid_e2e.py",
        "tests/test_e2e.py",
    }

    selected = set(select_tests([module], repo_root=Path(".")))
    assert consumers <= selected
    assert SELECTOR_META_GUARD_TEST in selected


def test_literal_path_scan_excludes_the_meta_guard_suite_that_lists_paths_as_data(tmp_path: Path) -> None:
    # The exclusion is a correctness requirement, not tidiness. This suite spells
    # support-module paths as DATA — routing anchors, the carve-out allowlist,
    # guard fixtures — and a scanner cannot tell "path as subject" from "path as
    # datum". Scanning it would make it a consumer of nearly every support
    # module, which empties the zero-consumer domain the collapse route is
    # asserted on and leaves the anti-vacuity floor counting phantoms.
    excluded = _literal_path_consumer_index()
    assert not [module for module, suites in excluded.items() if SELECTOR_META_GUARD_TEST in suites]

    unexcluded = _literal_path_consumer_index(suites=_tracked_top_level_test_files())
    phantom = sorted(module for module, suites in unexcluded.items() if SELECTOR_META_GUARD_TEST in suites)
    fabricated = sorted(set(phantom) - set(excluded))
    assert fabricated, (
        "this suite no longer spells support-module paths as data, which is the "
        f"only reason the exclusion exists (phantom edges: {phantom})"
    )

    # The mechanism itself, on a constructed file so the claim does not rest on
    # which paths this suite happens to spell today: a module whose source lists
    # the paths becomes a "consumer" of every one of them.
    support_modules = _tracked_tests_support_modules()
    probe = tmp_path / "test_paths_as_data.py"
    probe.write_text(f"SAMPLE = {support_modules!r}\n", encoding="utf-8")

    assert set(_literal_path_consumer_index(suites=[str(probe)])) == set(support_modules)
