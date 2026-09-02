# Tasks

Fixture level: expanded · repair intensity: medium · issues: #1745 #1733 #1642 #1800 #1613 #1649
(one PR, one implementer, serial, order #1745 → #1733 → #1642 → #1800 → #1649 → #1613).
Line cites are against `origin/master` `9785e52d`; symbol names are authoritative.
Upstream `Suggested fixture level` / `Minimal mergeable slice`: absent on all six (hand-written issues).

Change surface (all under `tests/`): `test_scheduler_lineage.py`,
`test_scheduler_file_provider_refresh.py`,
`test_timescale_write_guard_wire_site_invariant.py`, `test_production_scheduler.py`,
`test_shud_runtime.py`, `test_warm_start_chaining.py`, and `test_orchestration_chain.py`
only if D6 routes the #1613 fix through its `_orchestrator` helper.

Must preserve: every existing test in the six files and their current assertions
(none deleted, none weakened); the production modules named in the red proofs
byte-identical at commit time (`git diff -- packages services workers` empty);
every timeout-path test still receives a budget that expires; the `_orchestrator`
helper's consumers outside the six files keep passing —
`tests/test_file_orchestration_journal.py` (`:78, :16300, :16364, :17367`; CI-selected
via its module-level `from tests.test_production_scheduler import` at `:49`),
`tests/test_forced_resubmit_veto.py` (`:657, :704`; imports inside function bodies,
which `scripts/select_ci_tests.py:66-73` deliberately excludes from CI's importer
closure — the one consumer invisible to the PR CI lane) and
`tests/test_production_scheduler.py` (in the change surface, self-selected); all
three are run locally here (0.1/0.2).

Seams under test (declared per issue, consumed not renegotiated):
`repo.clone_lineage_signal` / `resolve_lineage_cutover` over the file-index fixture
(#1745); `read_provider_snapshot` (#1733); the wire-site AST scan helpers and
`_wire_site_hits()` (#1642); `scheduler_module._bounded_evidence_payload` over
`_incident_scheduler_evidence_payload` (#1800); `_module_level_constant_consumers`
plus the imported module namespace (#1649); `SHUDRuntime.execute` and
`ForecastOrchestrator.orchestrate_cycle` through the existing test helpers (#1613).

Risk packs (core):
- Public API / CLI / script entry: not selected — no entrypoint changes.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: not selected — #1733 covers the metadata disjunct of an existing guard only (a `tmp_path` chmod inside one test); no production IO change, so the pack's closure matrix (symlink leaf/ancestor, traversal, non-regular) is outside its stated scope.
- Schema / columns / units / field names: not selected — no format change.
- Auth / permissions / secrets: not selected.
- Concurrency / shared state / ordering: **selected** — #1613 is a wall-clock-budget flake; evidence is the two deterministic mechanism harnesses in 6.5 (`DIAG1613_SPAWN_DELAY=14.5` for A, `DIAG1613_STALL_STAGE=state_save_qc:6` for B) red before / green after; load runs are NOT the oracle (design D6).
- Resource limits / large input / discovery: not selected — the AST scan roots are unchanged.
- Legacy compatibility / examples: not selected — no consumer-facing change.
- Error handling / rollback / partial outputs: not selected — no production error path changes.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: **selected** — four spec deltas record the new coverage obligations and declared bounds.
Domain packs (NHMS profile): all not selected — no geospatial, forcing, SHUD-numerical, PostGIS/Timescale runtime, Slurm, provider, manifest/QC or display behaviour changes; #1642 touches only a test over SQL literals.

Non-goals: production behaviour changes; #1800 fixture↔producer sync;
#1649 residuals 3/4/5 (declared, not closed); #1642 `tests/integration_helpers.py`
teardown DELETE (#1640) and non-guarded DELETE literals; #1101 test-file split;
an injectable clock in `services/orchestrator` / `workers/shud_runtime`.

## 0. Evidence Floor

Oracle is local + CI pytest for all six (all `db-free`); node-27 receipts are
required only where the issue demands both platforms (#1733) or where the
failure needs the oracle machine's contention profile (#1613). No node-22.

- [x] 0.1 `uv run pytest -q tests/test_scheduler_lineage.py tests/test_scheduler_file_provider_refresh.py tests/test_timescale_write_guard_wire_site_invariant.py tests/test_shud_runtime.py tests/test_warm_start_chaining.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_forced_resubmit_veto.py` green locally (the last two are `_orchestrator` consumers; only `test_forced_resubmit_veto.py` is invisible to the PR CI lane)
- [x] 0.2 `uv run pytest -q tests/test_production_scheduler.py -k "bounded or scheduler_state_failure or module_level_constant"` green locally, then the whole file once
- [x] 0.3 `uv run ruff check .` clean
- [x] 0.4 Red proofs (sections 1-6), each shown red before / green after; NO `git stash` (the stash stack is shared with other live sessions) — mutate, run, `git checkout -- <file>`, paste output
- [x] 0.5 `git diff --stat -- packages services workers` empty at commit time
- [x] 0.6 node-27 (detached worktree at the frozen SHA, never the live tree): #1733 file green + mutation receipt (section 2); #1613 three files green + the two post-fix mechanism harness commands passing (section 6)
- [x] 0.7 `openspec validate harden-test-oracles-followups --strict --no-interactive`

## 1. #1745 — fixture wiring becomes an observed quantity (D1)

- [x] 1.1 In `test_unusable_earliest_clone_row_still_resolves_lineage_on_the_db_free_plane` (`tests/test_scheduler_lineage.py:175-208`) bind the `_entry(..., usable_flag=False)` result to a name, `assert entry["usable_flag"] is False` before `_repository(...)` publishes it, then pass `[entry]`
- [x] 1.2 Red proof: hard-code `"usable_flag": True` at `tests/lineage_state_index_fixtures.py:100` → this test red (paste), restore → `uv run pytest -q tests/test_scheduler_lineage.py` green

## 2. #1733 — isolate the metadata disjunct (D2)

- [x] 2.1 Add `test_provider_snapshot_rejects_metadata_only_divergence` adjacent to the two provider-snapshot tests (`tests/test_scheduler_file_provider_refresh.py:687-756`): destination `generation-a`; monkeypatch `provider_atomic_module.read_bytes_limited_no_follow`; on call 2 read the bytes, then `os.chmod` the destination to a different mode (bytes untouched); assert `reason == "provider_preimage_changed"`, `calls == 3`, bytes still `generation-a`; restore the original mode in `finally`
- [x] 2.2 Red proof (macOS): delete the `before != after` disjunct at `packages/common/provider_atomic.py:139` → `-k provider_snapshot_rejects` shows the new test red AND the existing two still passing (`1 failed, 2 passed`); restore; file green
- [x] 2.3 Same mutation receipt on node-27 in a detached worktree (`1 failed, 2 passed` under mutation; whole file green unmutated); the live `/home/nwm/NWM` tree is not mutated

## 3. #1642 — DELETE window predicate (D3)

- [x] 3.1 Add `_delete_literal_is_valid_time_bounded(text: str) -> bool` (or equivalently named helper) to `tests/test_timescale_write_guard_wire_site_invariant.py`: true iff the literal contains both `valid_time >=` and `valid_time <=`; document the admitted-spelling set and that `valid_time <` is refused
- [x] 3.2 Parametrized synthetic pin (style of `_UNADMITTED_MODULE_LEVEL_FORMS`): bounded one-line, bounded multi-line/indented, bounded with `%(name)s` params → True; no predicate, `>=` only, `<=` only, `>=` + `<` → False
- [x] 3.3 Repository-scan test: for every `(schema, table)` in `HYPERTABLES_GUARDED` and every module in `_wire_site_hits()`, every call-argument string containing `DELETE FROM {schema}.{table}` passes the predicate; failure message names module, enclosing function and missing bound(s)
- [x] 3.4 Red proof: strip `AND valid_time <= %s` from `workers/output_parser/parser.py:992` → scan test red (paste), restore; the synthetic negatives (3.2) are the predicate's own red-capable pins and are pasted green. Note `packages/common/compressed_chunk_cold_probe/shell.py:417` carries the half-open form but is on `_INTENTIONALLY_UNWIRED_MODULES` and never reaches the scan — do NOT widen the allowlist for it
- [x] 3.5 The four existing tests in the file unchanged and green

## 4. #1800 — payload-driven lane presence (D4)

- [x] 4.1 Add a property test near the bounded `restart_reconcile` consumers in `tests/test_production_scheduler.py`: take `_incident_scheduler_evidence_payload(...)`, run `scheduler_module._bounded_evidence_payload(payload, reason="evidence_size_limit_exceeded", max_evidence_bytes=8_000)`, and for every source `restart_reconcile` key whose value is a `Mapping` with an `outcomes` value that is a non-`str`/`bytes` `Sequence`, assert the key is in the bounded block with an `outcomes` list of equal length; assert at least two such lanes were exercised (so the property is not vacuous)
- [x] 4.2 The guard neither imports nor references `_BOUNDED_RESTART_RECONCILE_LANES` (grep-verifiable); docstring states why the nine `== _expected_bounded_restart_reconcile()` assertions stay (extra-key detection) and the declared residual (producer-only lanes invisible until mirrored into the fixture)
- [x] 4.3 Extend the docstring of `test_bounded_evidence_retains_inflight_error_when_only_the_inflight_segment_failed` (`:20502`) to say its exact-shape assertion also pins lane retention, not only the error key
- [x] 4.4 Count check: exactly nine `== _expected_bounded_restart_reconcile()` assertions before and after (`grep -c`)
- [x] 4.5 Red proof, discriminating double mutation (the #1797 shape — compactor AND helper both missing the lane): drop `inflight` from the `for lane in _BOUNDED_RESTART_RECONCILE_LANES` loop in `services/orchestrator/scheduler_evidence_payload.py:398` (`continue` on `inflight`) AND remove the `"inflight"` entry from `_expected_bounded_restart_reconcile()` (`tests/test_production_scheduler.py:35147`) → the nine exact-equality assertions stay green while the new property reds (paste both counts); expected collateral reds from inline-dict tests that also name the `inflight` lane: `test_bounded_evidence_retains_inflight_error_when_only_the_inflight_segment_failed` (`:20502`), `test_bounded_evidence_keeps_inflight_identity_mismatch_outcomes` (`:20534`), `test_compact_restart_reconcile_passes_an_empty_outcomes_list_through_on_both_lanes` (`:20622`) — name them in the receipt, they are not a false alarm. RESTORE CAREFULLY: `tests/test_production_scheduler.py` carries your uncommitted 4.1-4.3 edits, so revert the `:35147` helper line BY HAND (never `git checkout --` that file); `git checkout -- services/orchestrator/scheduler_evidence_payload.py` is fine. `-k bounded` green afterwards. A single-sided compactor mutation reds the nine as well and proves nothing new

## 5. #1649 — residuals 1 and 2 closed, 3/4/5 declared (D5)

- [x] 5.1 In `test_scheduler_state_failure_holds_no_second_permanent_code_refusal_list` (`:27641`): for every inventoried name with declared kind `function` or `class`, assert `getattr(value, "__module__", None) == scheduler_state_failure_module.__name__` and `getattr(value, "__qualname__", None) == name`
- [x] 5.2 Add value pins for the thirteen unpinned constants with literal expected values (`_COPYBACK_REQUIRED_RESTART_STAGES`, `_ARTIFACT_PROBE_ERROR_REASON`, `_ARTIFACT_TARGET_NOT_A_FILE_REASON`, `_NON_REGULAR_OBJECT_KINDS` as literal strings, `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CLASSIFIERS`, `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CODES`, `_RECORDED_FAILURE_CODE_KEYS`, `_HYDRO_RUN_CODE_CLEARING_STATUSES`, `_DOWNSTREAM_FORECAST_OUTPUT_DEPENDENT_STAGES`, `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES`, `_FORCING_SIDECAR_FILENAME`, `_FORCING_PACKAGE_MANIFEST_FILENAME`, `_FORCING_SIDECAR_MAX_BYTES`); assert the pinned constant name set equals `set(_SCHEDULER_STATE_FAILURE_CONSTANT_CONSUMERS)` so a nineteenth constant cannot arrive unpinned
- [x] 5.3 Update the test docstring: the "five of eighteen" residual is closed; identity pins close the function/class same-kind leg up to identity forgery (`functools.wraps` / `lru_cache` / `staticmethod` copy identity — declared bound); residuals 3/4/5 remain declared scale limits
- [x] 5.4 No-false-red proof: a scratchpad harness that imports the module, decorates an existing helper with `functools.lru_cache` via `setattr`, and re-runs the identity clause → green (paste)
- [x] 5.5 Red proof (scratchpad harness, not committed): reflectively rebind `_remedy_permits_permanent_failure` to a plain wrapper `def` defined outside the module → identity pin red; replace `_ForcingSidecarProvenance` with a subclass → red; `functools.partial` → red; `staticmethod`/`functools.wraps` wrapper → report the measured result (expected: passes; that is the declared bound)
- [x] 5.6 Red proof: temporary duplicate assignment of one newly pinned constant with a different value at the end of `services/orchestrator/scheduler_state_failure.py` → that value pin red (paste), restore
- [x] 5.7 `uv run pytest -q tests/test_production_scheduler.py -k "scheduler_state_failure or module_level_constant or downstream_failure_restartable"` green

## 6. #1613 — wall-clock robustness of the two victims (D6; diagnosis done)

Diagnosis persisted at `.workplans/pr-1949/diag-1613/DIAGNOSIS.txt` (gitignored,
local-only evidence) with the harness plugin `diag1613.py` (env knobs
`DIAG1613_SPAWN_DELAY`, `DIAG1613_JOB_TIMEOUT`, `DIAG1613_STALL_STAGE`, `DIAG1613_TRACE`);
the issue's cross-test shared-state reading is superseded and not reproduced under
the diagnosis's conditions (design D6).

- [x] 6.1 Victim A: `_runtime(tmp_path, repository, shud_executable=stub, timeout_seconds=300)` at `tests/test_shud_runtime.py:6034` and at the sibling watcher-held-stub test (`:6092`); a one-line comment cites #1613 (budget is spawn-latency headroom, not a timeout oracle); each site pins `runtime.config.timeout_seconds >= 60` so a revert to the helper's 30 s default is red
- [x] 6.2 Victim B: `_orchestrator` (`tests/test_orchestration_chain.py:9209`) takes `job_timeout_seconds: float = 120.0` and passes it into `OrchestratorConfig`; raise the eleven direct `job_timeout_seconds=5` sites in `tests/test_warm_start_chaining.py` (`:1172, 1514, 1624, 1745, 1795, 1838, 1884, 1926, 1967, 2028, 2446`) and the four inline `OrchestratorConfig` builds in `tests/test_orchestration_chain.py` (`:4465, 4492, 4527, 4553`) to `120`
- [x] 6.3 Pin: a small test in `tests/test_orchestration_chain.py` asserting `_orchestrator(...).config.job_timeout_seconds >= 60` and one in `tests/test_warm_start_chaining.py` asserting the same for `_cohort_orchestrator(...)`, each with a docstring citing #1613 (red against the previous hard-coded 5); the ten remaining inline `120` literals in `test_warm_start_chaining.py` stay unpinned (declared limit D6 iii)
- [x] 6.4 Timeout-path oracles intact: `tests/test_orchestration_chain.py:4318,4356,4395,12841` and `tests/test_shud_runtime.py:5888` unchanged; `--durations=10` on both files before/after shows no test slowed by more than noise; the out-of-file `_orchestrator` consumers (`tests/test_file_orchestration_journal.py`, `tests/test_forced_resubmit_veto.py`, `tests/test_production_scheduler.py`) pass with the new default (0.1/0.2)
- [x] 6.5 Mechanism red → green (local): (a) A pre-fix red with `PYTHONPATH=.workplans/pr-1949/diag-1613 DIAG1613_SPAWN_DELAY=14.5 uv run pytest -q -p no:cacheprovider -p diag1613 <victim A nodeid>` (`f012=budget_exhausted`), post-fix same command passes; (b) B: extend `diag1613.py` with `DIAG1613_STALL_STAGE=state_save_qc:6` (sleep in `FakeCycleSlurmClient.get_job_status` or the poll wrapper for that stage) — pre-fix `'failed' == 'complete'` red, post-fix passes. Paste both. The harness is gitignored local-only evidence; the committed, repo-resident oracle is the `>= 60` pins of 6.1/6.3.
- [x] 6.6 node-27 (detached worktree at the frozen SHA, live tree untouched): `uv run pytest -q tests/test_shud_runtime.py tests/test_warm_start_chaining.py tests/test_orchestration_chain.py` green, plus the two post-fix harness commands from 6.5 passing there
- [x] 6.7 Recorded deviation in the PR `偏离记录`: acceptance re-framed from "name the shared state" to "remove the wall-clock dependence" (issue author's second comment + diagnosis verdict; shared state recorded as superseded / not reproduced, not refuted; criterion 2 discharged by the mechanism harnesses, not a full-suite green); declared limits `:5888`, the other-file `job_timeout_seconds=5` sites and the ten unpinned inline `120` literals listed in design D6 with the one-line reason
