from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import pytest

from scripts.select_ci_tests import (
    CHANGED_TEST_FILE_RULES,
    CORE_SMOKE_TESTS,
    DIRECT_GRID_CONTRACT_TESTS,
    DIRECT_GRID_E2E_TESTS,
    DIRECT_GRID_SURFACE_TESTS,
    FILE_JOURNAL_READ_STATE_TESTS,
    ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
    PATH_TEST_RULES,
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
    selected = select_tests(["workers/shud_runtime/runtime.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_runtime_ic_header.py",
        "tests/test_runtime_mode.py",
        "tests/test_shud_runtime.py",
    ]


def test_select_tests_maps_direct_grid_producer_surface_to_compact_e2e_fixture() -> None:
    selected = select_tests(["workers/forcing_producer/direct_grid_contract.py"], repo_root=Path("."))

    assert selected == sorted(DIRECT_GRID_SURFACE_TESTS)
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

    assert selected == sorted(DIRECT_GRID_SURFACE_TESTS)
    assert len(selected) == 1 + len(DIRECT_GRID_CONTRACT_TESTS)
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
    # itself is untouched — no whole legacy suite comes back.
    assert selected == sorted({*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_select_ci_tests.py"})
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
    selected = select_tests(["services/orchestrator/retry.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_orchestration_chain.py",
        "tests/test_orchestrator.py",
        "tests/test_production_scheduler.py",
        "tests/test_scheduler_backfill.py",
        "tests/test_warm_start_chaining.py",
    ]


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


def _parse_tracked(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


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
GATING_MARKER_NAMES = frozenset({"integration", "e2e"})

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


def _tracked_top_level_test_files() -> list[str]:
    return [path for path in _tracked_python_files("tests") if fnmatch.fnmatch(path, "tests/test_*.py")]


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


def test_guarded_module_rules_cover_their_non_gated_importer_closure() -> None:
    # Fourth recurrence of the same defect class (#1191 -> #1247 -> #1283 ->
    # #1447): a rule is written without a "who imports this module" scan, the PR
    # lane goes green on a partial selection, and the gap only shows up on the
    # post-merge master full run. The importer set is DERIVED from the tracked
    # tree here, never frozen, so a new importer suite reddens this test
    # (pointing at the rule to extend) instead of falling out of the PR lane.
    offenders: list[str] = []
    for source_path, module, known_member in GUARDED_MODULE_CLOSURES:
        assert Path(source_path).is_file(), f"guarded module source missing: {source_path}"
        importers = _non_gated_top_level_importer_tests(module)
        assert importers, f"{module}: derived no non-gated top-level importer suites"
        assert known_member in importers, (
            f"{module}: expected {known_member} among derived importers, got {sorted(importers)}"
        )

        selected = set(select_tests([source_path], repo_root=Path(".")))
        missing = sorted(importers - selected)
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
    # meta-guard accumulation is not. These two keep today's behavior exactly.
    assert Path(changed_path).is_file()

    assert select_tests([changed_path], repo_root=Path(".")) == [changed_path]


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
            for rule in (*PATH_TEST_RULES, *CHANGED_TEST_FILE_RULES)
            for target in rule.tests
            if "::" not in target
        }
        | {target for target in CORE_SMOKE_TESTS if "::" not in target}
    )
    assert file_targets, "expected file-level test targets in the selector rule set"
    stale_files = [target for target in file_targets if not Path(target).is_file()]
    assert not stale_files, f"selector rules point at test files that no longer exist: {stale_files}"
