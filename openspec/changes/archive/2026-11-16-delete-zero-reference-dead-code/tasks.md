# Tasks: delete-zero-reference-dead-code (#3147)

## Risk packs

- [x] Legacy compatibility — selected: the 9 scheduler forwarders are governed compatibility facades. → tasks 1.2,
  2.2, 2.3, 2.7.
- [x] Public API / CLI / script entry — selected: an orphan script is deleted, and dynamic or string entry points must
  be ruled out. → tasks 1.1, 1.3, 2.6.
- [x] Documentation — selected: living docs that name a deleted symbol. → tasks 1.4, 2.6.
- [ ] Not selected — File IO, Schema, Auth, Concurrency, Resource limits, Error handling, Release, Config: deleting
  unreferenced code changes none of these.

## 1. Implementation

- [x] 1.1 For each symbol in #3147, re-verify that nothing references it, across the whole repo including `tests/`,
  `infra/`, `apps/frontend` and `.github/`. Check:
  - a word grep;
  - string, `getattr` and `importlib` dispatch;
  - owner-module callbacks into the facade (`_scheduler.<name>`, `scheduler.<name>`);
  - `monkeypatch.setattr` targets;
  - `pyproject` `[project.scripts]`;
  - sbatch, systemd and CI.

  Then delete the symbol, and delete whatever becomes unused as a result. Iterate until nothing more falls out.

  The PR records a table with these columns: symbol, grep command, hit count, verdict. A symbol that turns out to be
  referenced is kept, with the reason given.
- [x] 1.2 Delete only those of the 9 `scheduler.py` forwarders that have no reference, including callbacks. Keep the
  forwarders listed in `design.md` (Must preserve).

  In `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`, remove only the deleted names from rows 131 and 160. The
  `scheduler-model-discovery-forwarders` group row stays.

  No guard test is changed.
- [x] 1.3 Delete `scripts/m24_grib_preflight_proof.py`, and update any selector table that names it.
- [x] 1.4 Update the living docs that name a deleted symbol. Historical records stay as they are: archived openspec,
  receipts and dated inventories.

## 2. Tests / verification (each expected to pass, or to give the stated result)

- [x] 2.1 `uv run ruff check .`: clean.
- [x] 2.2 These pass:
  - `uv run pytest -q tests/test_production_scheduler.py -k "coerce_registered_model or discover_models or active_model or model_limit or duplicate_active_model or filter"`;
  - the governed-group test near `tests/test_production_scheduler.py:849`.
- [x] 2.3 These pass, and must be run explicitly, because a `docs/**`-only change does not open the backend lane:
  - `uv run pytest -q "tests/test_entropy_audit_report_contract.py::test_compatibility_facade_guard_current_repo_passes_with_inventories"`;
  - `uv run pytest -q "tests/test_entropy_audit_baseline_writer_summary.py::test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo"`.
- [x] 2.4 The selector-chosen set for the diff (`scripts/select_ci_tests.py`) plus `tests/test_select_ci_tests.py` pass,
  locally and in a node-27 temp worktree.
  - Evidence: local 15669 passed / 88 skipped; node-27 temp worktree (origin/master...HEAD selection, 331 files +
    selector tests) 15665 passed / 92 skipped / 0 failed (55 min). CI full sharded dispatch 43816876267: only
    `test_shared_preflight_rejects_realpath_stub_symlink` fails, identical to master since `b2fc70547` (#3153).
- [x] 2.5 An import smoke of every touched module succeeds (27 modules: the 26 from #3147 plus
  `services.orchestrator.scheduler`; add any further module the iteration touches):
  `uv run python -c "import importlib,sys; [importlib.import_module(m) for m in sys.argv[1:]]" packages.common.request_auth packages.common.slurm_env packages.common.node27_pgdata_workload_io packages.common.node27_pgdata_workload_plan packages.common.node27_pgdata_workload_query packages.common.node27_timeseries_compression_budget packages.common.model_registry_lifecycle_support packages.common.state_manager apps.api.openapi_patching_parameters scripts.node27_timeseries_compression scripts.node27_timeseries_compression_supervisor services.orchestrator.file_orchestration_journal services.orchestrator.pipeline_job_provenance services.orchestrator.reconcile services.orchestrator.scheduler_core services.orchestrator.scheduler_timing services.production_closure.met_validation services.production_closure.slurm_validation services.production_closure.two_node_e2e_evidence services.tile_publisher.publisher workers.canonical_converter.converter workers.forcing_producer.producer workers.model_registry.basins_calibration_overrides workers.model_registry.basins_geometry workers.model_registry.basins_geometry_shapes workers.shud_runtime.runtime services.orchestrator.scheduler`.
- [x] 2.6 Grep each deleted symbol and `m24_grib_preflight_proof` across these targets, excluding archived openspec:
  - the living docs;
  - `scripts/select_ci_tests*.py`;
  - `pyproject.toml`;
  - `infra/`;
  - `.github/`;
  - `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` (the deleted forwarder names).

  Expected: zero hits.
- [x] 2.7 Hard gate, read the way `.github/workflows/governance.yml:128-147` reads it:
  `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` exits 0, with
  `metadata.hard_gate_status == "pass"` and `metadata.hard_gate_failing_count == 0`.
  Structural budget: `uv run python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref origin/master`;
  in `metadata.structural_file_budget`, `ownership_growth_signal_count` is 0 and no file is newly oversized compared
  with the same command on `origin/master`.
- [x] 2.8 Vulture before and after on the touched dirs (`--min-confidence 60`): every deleted name is gone. The PR
  gives the before and after counts.
- [x] 2.9 No existing test is modified. Any unavoidable change is listed under deviations.
