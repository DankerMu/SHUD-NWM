# reconcile-module-split (#3155, PR 2 of 2)

Closes #3155. PR 1 (#3175) split `packages/common/safe_fs.py`.

## Why
`services/orchestrator/reconcile.py` is 3886 lines and is not excluded from the large-file guard
(`.large-file-guard.json`, `maxLines: 1000`). Because the guard counts staged line totals, even a deletion-only commit
to the file is blocked. That is why #3148 had to descope retiring `_parse_comment_sacct_rows` and
`_parse_comment_sacct_matches` (`reconcile.py:1283-1339`). Their only production caller, `_query_comment_accounting`,
was deleted in #3147, and the live parser is `_index_comment_sacct_matches` (`:1238`).

## What
1. **Partition** `reconcile.py` into the leaf modules in `design.md`, as a facade plus 8 leaves. This is a pure move:
   no logic edits except the logger-name change in What 3.
   - `reconcile_reserved_unbound_jobs` (`:2197-3203`) is a single 1007-line body. Its loop carries 27 `continue`
     exits and an `except` that reads state assigned inside the `try`, so it has no seam that can be extracted
     without a behavioural refactor.
   - It moves whole into `reconcile_reserved.py`, together with `_reserved_record_identity_matches`. That helper's
     only caller is this function, and a test patches it (`tests/test_gateway_reconcile_comment_accounting.py:1560`).
   - That module is the one new `.large-file-guard.json` exclusion. Decomposing the function is follow-up work
     under a new issue, filed with this PR.
2. **Facade** `reconcile.py`, about 150 lines:
   - the module docstring;
   - `import subprocess`, because tests use `reconcile_module.subprocess`;
   - one top-level `real_backend` import,
     `from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE as SLURM_JOB_ID_RE`. This keeps every suite that
     imports the facade at top level inside the derived `real_backend` one-hop closure, so the
     `ci-contract-baseline` fixed-point sentence, the selector comments at `scripts/select_ci_tests_rules.py:2347-2387`,
     and the pins at `tests/test_select_ci_tests.py:6384-6386,6444` all stay true;
   - `LOGGER`;
   - redundant-alias re-exports (`from services.orchestrator.reconcile_x import Y as Y`) of the **import surface**.
     That surface is every top-level name `reconcile.py` defines on `origin/master` (116), minus the two retired
     parsers, plus `subprocess`. Names `reconcile.py` only imported are not re-exported, apart from the
     `SLURM_JOB_ID_RE` import above.

   No leaf imports the facade. `facade_guard` / `COMPATIBILITY_FACADE_CONFIGS` govern only `scheduler.py` and `chain.py`
   (`scripts/governance/entropy_audit/schema.py:206-217`), so they do not apply here.
3. **Logger:** each leaf that logs uses `logging.getLogger("services.orchestrator.reconcile")`, not `__name__`. Tests
   filter records on that exact name (`tests/test_gateway_reconcile_comment_capability.py:261-330`,
   `tests/test_gateway_reconcile_comment_sacct_bounds.py:310,347`). With `__name__`, their "no warning" assertions
   would pass vacuously.
4. **Patch retargets:** this is the hazard of the split. A patch on the facade rebinds only the facade name, so any
   caller that moved to a leaf would keep the real object.
   - Every patch whose callers move is retargeted to the owning leaf, per the `design.md` table: `_bounded_sacct_stdout`,
     `_bounded_visibility_stdout`, `MAX_COMMENT_SACCT_*`, `COMMENT_SACCT_*`, `MAX_VISIBILITY_PROBE_*`,
     `_parse_fallback_sacct_rows`, `_reserved_record_identity_matches` and `forecast_cohort_identity_is_valid`.
   - Patches whose production readers resolve the name through the facade at call time stay on the facade:
     `default_comment_sacct_querier`, `default_sacct_querier` and `RESERVATION_ABSENCE_GRACE`. The call-time readers
     are `scheduler_runtime.py:1584-1588,1907,1915`, `chain_stage_execution.py:259` and
     `file_orchestration_journal.py:5289`. `RESERVATION_ABSENCE_GRACE` reaches `reconcile_reserved_unbound_jobs` only
     as a def-time default (`:2201`), so a facade patch never governed that path, and nothing narrows.
5. **Retire** `_parse_comment_sacct_rows` and `_parse_comment_sacct_matches`.
   - The LOW-2 malformed-JobID cases in `tests/test_reconcile_sacct_parse.py` and
     `tests/test_gateway_reconcile_comment_accounting.py:520-577` are **retargeted** at the live
     `_index_comment_sacct_matches`, not deleted.
   - `tests/test_reconcile_sacct_parse.py` then imports from `reconcile_comment_sacct`, a top-level importer of
     `real_backend`. It therefore stays a one-hop `real_backend` suite, as the MODIFIED `ci-contract-baseline`
     scenario requires.
6. **Path-keyed consumers** move to the new owning file:
   - `pyproject.toml:153` B010 per-file ignore → `reconcile_comment_sacct.py`;
   - `tests/status_literal_allowlist.json:45` → `reconcile_inflight.py`;
   - `tests/test_operator_action_listing_held_reservations.py:34,103-140` AST-scans `reconcile.py` for the action
     vocabulary → it scans every `services/orchestrator/reconcile*.py`;
   - selector rules, frozen lists, the `runtime-budget` disposition table and the importer-gap dispositions: each new
     leaf gets the treatment `reconcile.py` gets today;
   - `docs/governance/ROLE_BOUNDARY.md:67` names `reconcile.py`, and gains the partition.
   - `reconcile.py:<n>` line references: after the split the facade is about 150 lines, so every such reference names
     the wrong file. Each one is rewritten as a symbol reference (`services.orchestrator.reconcile_<leaf>.<symbol>`),
     with no new `file:line` key. The references:
     - `scripts/node22_scheduler_stall_health.py:271` (`:2564`, `reconcile_reserved_unbound_jobs`);
     - `services/orchestrator/scheduler_evidence_payload.py:109` (`:1076-1087`, `reconcile_inflight_jobs`);
     - `docs/runbooks/production-ops/stuck-detection.md:683,685`;
     - `docs/runbooks/failed-basin-retry.md:45`;
     - `tests/test_gateway_reconcile_identity_release.py:28`;
     - `tests/test_production_scheduler.py:21942,37647-37650`.

     The first two are scanned baseline keys (`line_reference_baseline.json:83,212`). Rewriting them only lowers the
     count, which the checker allows, and the two stale keys are removed.
   - Not applicable: `openspec/specs/gateway-reconcile-test-partitioning/spec.md:38,49` ("Runtime-budget exclusions
     remain narrow") stays literally true. The partitions import only the facade at top level, so a leaf-only change
     derives no `(leaf, partition)` gap, and no new `runtime-budget` row is needed.

**Non-goals:**
- No behavioural change.
- No decomposition of `reconcile_reserved_unbound_jobs`.
- `__module__` of the moved classes changes; nothing in the repo reads it (audit).
- Narrowing: `forecast_cohort_identity_is_valid` is imported from another package and has callers in three leaves:
  - `_terminal_file_cohort_identity_matches` (`reconcile_inflight`);
  - `_reserved_record_identity_matches` (`reconcile_reserved`);
  - `_accepted_submit_reconcile_job` (`reconcile_reserved_support`).

  The patch at `tests/test_gateway_reconcile_file_cohort_identity.py:944` drives only the terminal validator, so it
  targets `reconcile_inflight` and no longer reaches the other two. That test never exercised them. The spec's
  imported-name rule covers this, and it is recorded as a deviation.
- Deviation from the issue text, which asks for "each 1000 lines or fewer": `reconcile_reserved.py` is guard-excluded,
  because it holds the indivisible function. Follow-up: #3176.
- Deviation, found during implementation: four test suites were already over the guard on master and not excluded.
  Each carries a moved-caller patch that must be retargeted, and the guard counts staged totals, so the change
  cannot be committed without touching them. They are added to `.large-file-guard.json`; follow-up #3177 partitions
  them and removes the entries.

  | suite | lines on master |
  |---|---|
  | `tests/test_gateway_reconcile_claimant_exclusivity.py` | 3433 |
  | `tests/test_gateway_reconcile_binding_provenance.py` | 2298 |
  | `tests/test_forcing_submit_ambiguity.py` | 1209 |
  | `tests/test_gateway_reconcile_file_cohort_identity.py` | 1140 |

  The production-module rule (exactly one `services/orchestrator/reconcile*.py` exclusion) is unaffected. Three of the
  suites are gateway-reconcile partitions. `gateway-reconcile-test-partitioning` says "without adding a guard
  exemption", so that requirement gets a MODIFIED delta. It names the three temporary exemptions until #3177, the pre-existing
  `test_gateway_reconcile_comment_accounting.py` exemption from #2674, and the already-oversized but untouched
  `test_gateway_reconcile_inventory.py` (1083 lines), which #3177 also covers.

## Must preserve
- Every name reachable as `services.orchestrator.reconcile.X` on `origin/master` stays reachable, except the two
  retired parsers, and resolves to the leaf object.
- Every monkeypatch keeps governing the code it governed before.
- Restart-reconcile and reserved-unbound behaviour, sacct bounds, and the log record names.

## Fixture level
expanded. Risk packs: refactor-move, monkeypatch-target, selector-routing, test-only-retire.

## Evidence Floor
- [ ] E1 `uv run ruff check --no-cache .`
- [ ] E2 AST move proof against `origin/master`. The script and its output go in the scratchpad; the result goes in
  the PR body.
  - Every top-level def, class and assignment of the old `reconcile.py` appears with an identical `ast.dump`, and its
    source segment with the leading comment is byte-identical, in exactly one leaf. The exception is the two retired
    parsers.
  - The allowed differences are import lines and `LOGGER` assignments using the explicit name.
  - The import surface (What 2) is reachable as `reconcile.X`, and each name is the leaf object.
  - The import graph of the partition is acyclic, and no leaf imports the facade.
- [ ] E3 Patch-target table (`design.md`) re-derived after the change by an AST scan of `tests/`, `services/`,
  `packages/`, `scripts/`, `workers/` and `apps/`, covering string targets, `monkeypatch.setattr(module, ...)`, aliases
  and direct assignment. Every patched name resolves on the module its callers read, and the scan output is recorded.
  At least one moved-caller patch is mutation-checked: patching the facade instead of the leaf turns its test red.
- [ ] E4 Tests, all green locally:
  - the selector set for the diff;
  - every test file that references `services.orchestrator.reconcile` (34 today; `git grep -l` recorded);
  - every test file the change edits;
  - `tests/test_reconcile*.py`, `tests/test_gateway_reconcile_*.py` and `tests/test_orchestrator_bind_reserved_job_*.py`;
  - `tests/test_operator_action_listing_held_reservations.py`, `tests/test_production_scheduler.py`,
    `tests/test_orchestrator_demote_*.py`, `tests/test_forcing_submit_ambiguity.py`,
    `tests/test_orchestration_chain.py`, `tests/test_hydro_run_attempt_triage.py`,
    `tests/test_chain_stage_poll_isolation.py`, `tests/test_scheduler_held_reservation_block.py` and
    `tests/test_file_journal_legacy_unversioned_reserved_guard.py`;
  - the guards `tests/test_select_ci_tests.py`, `tests/test_orchestrator_first_import.py` (it auto-discovers the new
    leaves), `tests/test_status_literal_scan.py` and `tests/test_entropy_audit_line_references.py`.

  New files get `git add -N` before the local selector run.
- [ ] E5 The same set passes in a node-27 temporary worktree.
- [ ] E6 `wc -l services/orchestrator/reconcile*.py`: every module is at or under 1000 lines except `reconcile_reserved.py`,
  which is the only `services/orchestrator/reconcile*.py` entry in `.large-file-guard.json`. The other new entries
  are exactly the four pre-existing oversized test suites named under Non-goals. The entropy hard gate has `hard_gate_failing_count 0`.
- [ ] E7 Outside `openspec/changes/archive/` and this change, `_parse_comment_sacct_rows` and `_parse_comment_sacct_matches` have
  zero hits.
- [ ] E8 `openspec validate reconcile-module-split --strict --no-interactive`.
