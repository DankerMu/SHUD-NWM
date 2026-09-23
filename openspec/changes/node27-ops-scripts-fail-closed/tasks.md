# Tasks — node-27 ops scripts fail-closed (#2355 #2309 #2284 #2283 #2529 #2504 #2464)

## Risk Packs

- [ ] Public API / CLI / script entry — **selected**: retention/raw-retention/refresh/alert CLIs, two shell wrappers, display route `source` params. Covered by 1–7.
- [ ] Config / project setup — **selected**: retention env toggle precedence, shared retention-window resolver, new unit/timer/env example. Covered by 1.x, 5.x, 6.x.
- [ ] File IO / path safety / overwrite — **selected**: summary overwrite (#2284), observer state file atomic replace. Covered by 3.x, 5.x.
- [ ] Error handling / rollback / partial outputs — **selected**: traversal failure localisation + summary always written (#2309), typed exit 2 in the observer. Covered by 2.x, 5.x.
- [ ] Schema / columns / field names — **selected**: retention receipt `mode/outcome`, raw-retention `skipped[]` fields, alert `reason=unsupported-source`, audit JSON, observer report. Covered by 1.2, 2.2, 5.x, 6.4, 7.x.
- [ ] Legacy compatibility — **selected**: #1446 in-window guard, alert exit codes, `--enforce`/env semantics, OpenAPI unchanged by the alias. Covered by 1.3, 6.3, 7.3.
- [ ] Concurrency / shared state / ordering — **selected**: same-second concurrent summaries; retention × parse overlap evidence (receipt). Covered by 3.x, 5.6.
- [ ] Documentation / migration notes — **selected**: env examples, runbook pages, #2529 receipt, archived coverage-alert design pointer. Covered by 1.4, 2.4, 5.7, 6.6, 7.4.
- [ ] Auth / permissions — not selected beyond: the observer uses a read-only DSN (documented in 5.5).
- [ ] Resource limits — **selected**: the audit is bounded (valid-time-bounded EXISTS, per-probe transaction, statement/lock timeouts); the expired `--skip-fresh` selection is rescan-bounded (`expired_rescan_interval`); the observer report is line-capped. Covered by 5.2, 6.2, 6.3, 6.4.
- [ ] Release / packaging — not selected.

## Execution plan

Two serial implementer passes on this branch, each committed before the next: **pass A** = #2355, #2309, #2284, #2283, #2464 (sections 1–4, 7); **pass B** = #2529, #2504 (sections 5–6). Implementers never ssh to node-27; 5.6 is composed from the orchestrator-captured raw file; 5.7, 6.5, 8.3 are orchestrator-run (design §Receipt ownership).

## 0. Setup

- [x] 0.1 Branch `feat/issue-2355-2309-2284-2283-2529-2504-2464-node27-fail-closed` from `origin/master` (contains batch F). Locate sites by symbol (issue line numbers are stale).
- [ ] 0.2 Large-file guard: add `packages/common/display_coverage.py`, `tests/test_node27_mvt_cache_retention.py` and any other staged >1000-line file absent from `.large-file-guard.json` `exclude`. New test module routed in `scripts/select_ci_tests.py` with a routing pin; update `tests/test_select_ci_tests.py` exact-set pins deliberately.
  - Pass A: `tests/test_node27_mvt_cache_retention.py` (1927 lines) added to `exclude`; every other pass-A file >1000 lines (`scripts/node27_timeseries_retention.py`, `tests/test_node27_{timeseries,raw}_retention.py`, `tests/test_node27_autopipeline_preflight.py`, `docs/runbooks/tier-node27-timeseries-storage.md`) was already excluded. No new pass-A test module. Routing change: `tests/test_node27_coverage_freshness_alert.py` added to the `apps/api/routes/hydro_display*.py` rule (the #2464 pin behind a function-local import is no importer edge), pinned by `test_display_route_diff_selects_the_coverage_alert_source_allowlist_pin`. RED with the rule entry removed: `AssertionError: apps/api/routes/hydro_display.py` / `assert 'tests/test_node27_coverage_freshness_alert.py' in [...]`; GREEN restored; `uv run pytest -q tests/test_select_ci_tests.py` → `785 passed`. `packages/common/display_coverage.py` is pass B.
- [ ] 0.3 Every new-behaviour test is shown red on pre-change source then green; record short excerpts inline per section. (Pass A done: excerpts under 1, 2, 3, 4, 7.)

## 1. #2355 — `--dry-run` wins (D1)
- [x] 1.1 Resolver precedence in `config_from_args`.
- [x] 1.2 Regression with a real eligible chunk + drop spy: `--dry-run` + `ENFORCE=1` → receipt dry-run, 0 drops, no enforce-only measurement.
- [x] 1.3 Existing semantics kept: env-only enforce; `--enforce`; env empty/0/false/no dry-run.
- [x] 1.4 `--help` text and `infra/env/node27-timeseries-retention.example` (and any runbook copying the old warning) corrected.
  - Tests (`tests/test_node27_timeseries_retention.py`): `test_dry_run_with_enforce_env_drops_nothing_through_main` (through `main(["--dry-run"])`, env `ENFORCE=1`, one eligible chunk: `mode`/`outcome` `dry-run`, `candidate_chunks == [chk-a]`, stub calls `== ["fetch"]` — no `measure`, no `drop`); `test_dry_run_flag_overrides_every_truthy_enforce_env_spelling` (7 spellings incl. `TRUE`, ` yes `, `on`, `2`); `test_enforce_flag_still_wins_over_a_falsy_or_absent_env` (absent/empty/blank/`0`/`false`/`FALSE`/`no`/` No `); `test_parser_keeps_dry_run_and_enforce_mutually_exclusive`; `test_help_text_states_the_env_default_and_that_dry_run_wins`; `test_env_example_no_longer_claims_dry_run_is_ignored`.
  - RED (pre-change source): `10 failed, 12 passed` on the new selection; `test_dry_run_with_enforce_env_drops_nothing_through_main`: `AssertionError: assert 'enforce' == 'dry-run'`; the env-example pin: `'does NOT override this variable' is contained here`.
  - GREEN: `uv run pytest -q tests/test_node27_timeseries_retention.py` → `221 passed, 1 skipped`.
  - Docs: env example precedence block rewritten (old-checkout caveat kept); `docs/runbooks/tier-node27-timeseries-storage.md` §8.4 step 2 comment, the `outcome=dry-run` bullet and the bracket-procedure warning updated. Historical receipts under `docs/runbooks/receipts/` left as history. Wrapper `scripts/node27_timeseries_retention_once.sh` untouched. Sibling: `scripts/node27_timeseries_compression.py` has no `*_ENFORCE` env fallback (`grep -n ENFORCE` → no match).

## 2. #2309 — traversal failure retires only its target (D2)
- [x] 2.1 Whole traversal wrapped; generator `OSError` → `_unavailable_skip`, not planned.
  - `_dir_size` now returns `(bytes, OSError | None)` (the `_iter_dirs` contract); both call sites record `_unavailable_skip(key=<target>, reason="<lane>_target_unsafe", ...)` — `raw_target_unsafe`, `canonical_target_unsafe`, `precip_cache_target_unsafe` (the existing `<prefix>_{root,source}_unsafe` vocabulary one level down; ends in `_unsafe`, so the documented operator `jq` check goes RED on it). Per-file `stat` guard kept. `SCHEMA_VERSION`, exit codes, `services/orchestrator/retention.py` untouched.
- [x] 2.2 Tests: ESTALE on the raw-lane target and on a mapped-lane target (inject via `os.scandir`) → exit 0, `status=completed`, target skipped with `error`/`error_type`, other targets/lanes deleted, summary written.
  - `test_a_stale_raw_cycle_while_sizing_retires_only_that_target`, `test_a_stale_mapped_lane_cycle_while_sizing_retires_only_that_target[canonical|precip-cache]` (fake `os.scandir` keyed on the resolved cycle path; anti-vacuity `hits` assertion).
  - RED (pre-change): `3 failed`; each `scripts/node27_raw_retention.py:909: in main` → `run_retention` → `collect_targets` → `_collect_{raw,mapped}_lane` → `:209: in _dir_size` → `OSError: [Errno 70] Stale file handle: '.../store/raw/gfs/2026060100'` (tick dead, no summary).
- [x] 2.3 `uv run pytest -q tests/test_node27_raw_retention.py` green. → with `tests/test_node27_raw_retention_copyback_mutex.py`: `80 passed`.
- [x] 2.4 `infra/env/node27-raw-retention.example` "closed the one crash path" wording corrected.

## 3. #2284 — one summary per run (D3)
- [x] 3.1 Unique default summary path with exclusive create; explicit override untouched.
  - `scripts/node27_mvt_cache_retention_once.sh`: override still checked for absoluteness before the lock and used verbatim; the default is built and reserved (`( set -C; : > "$path" )`, `-2`, `-3`, … on collision; `SUMMARY_PATH_UNWRITABLE` blocked when the create fails for a non-collision reason) only after `flock -n 9` and after the last `blocked` exit (`cd "$REPO"`); the `start summary=` line moved after the reservation. An EXIT trap removes a still-empty reservation. Runner `_write_summary` unchanged. `infra/env/node27-mvt-cache-retention.example` documents the naming.
- [x] 3.2 Test: two runs with `date` stubbed to the same second (the stub also answers `+%s`) → two files, both valid JSON; flock-skip and blocked paths leave no empty `*.json`.
  - `test_two_same_second_runs_leave_two_summary_files` (`...165207Z.json` + `...165207Z-2.json`, distinct pids, log lines name each file), `test_a_skipped_tick_leaves_no_summary_file`, `test_a_blocked_run_leaves_no_summary_file`, `test_a_runner_that_dies_before_writing_leaves_no_empty_summary`, `test_an_explicit_summary_override_is_used_verbatim`.
  - RED (pre-change): `test_two_same_second_runs_leave_two_summary_files` — `At index 0 diff: 'mvt-cache-retention-20260912T165207Z.json' != 'mvt-cache-retention-20260912T165207Z-2.json'` / `Right contains one more item` (second run overwrote the first). The skip/blocked/die/override tests pass on pre-change source by construction (no reservation existed); they guard the new reservation.
  - GREEN: `uv run pytest -q tests/test_node27_mvt_cache_retention.py` → `84 passed, 1 skipped` (the skip is the real-`flock(1)` case, absent on macOS).
- [ ] 3.3 (oracle-blocked, post-deployment) node-27 live: plan-only then production tick back-to-back → both JSONs in `node27-mvt-cache-retention-logs/`; exact commands recorded.

## 4. #2283 — cron rc capture (D4)
- [x] 4.1 Both `(non-fatal)` sites log the real rc; no other `$?`-after-expansion left in the file (grep evidence).
  - `|| { rc=$?; echo "[$(ts)] ... rc=$rc (non-fatal)" >> "$LOG"; }` at the coverage-backstop and MVT-prewarm sites; log text otherwise unchanged; explanatory comment placed after both blocks so line numbers cited elsewhere do not shift. `grep -n '\$?' scripts/node27_autopipe_cron.sh` → only `207:RC=$?` (read directly after the ingest command) plus the comment.
- [ ] 4.3 (oracle-blocked, post-deployment) node-27 live: first tick with a failing prewarm logs an rc agreeing with `tick-prewarm.json` `failed_count>0`; exact check recorded.
- [x] 4.2 Test executing the fixed idiom under `bash` with a stub exiting 3 → log `rc=3`, plus a static assertion that the anti-pattern is absent from the script.
  - `tests/test_node27_autopipeline_preflight.py::test_wrapper_logs_the_real_rc_of_each_failing_non_fatal_step` runs the real wrapper under `bash` (backstop stub exits 3, prewarm stub exits 4, tick still rc 0, all three phases timed); `test_wrapper_never_reads_the_exit_status_after_an_expansion` is the static half.
  - RED (pre-change): the log carried no `coverage backstop rc=3 (non-fatal)` line (`assert False` on the `any(...)`), and the static scan reported `(231, '    || echo "[$(ts)] autopipe: coverage backstop rc=$? (non-fatal)" >> "$LOG"')` plus the prewarm line. One-liner: `bash -c 'ts(){ date; }; false || echo "[$(ts)] rc=$?"'` → `rc=0`; fixed idiom → `rc=1`.
  - GREEN: `uv run pytest -q tests/test_node27_autopipeline_preflight.py tests/test_node27_mvt_prewarm.py` → `112 passed`.

## 5. #2529 — parse-failure residency observer (D5, D5r)
- [ ] 5.1 `scripts/node27_parse_failure_residency_alert.py` per D5 (query, state, lock, verdicts, exits 0/1/2, report to stdout).
- [ ] 5.2 Tests (injected observations + clock): liveness bound excludes a failed row not touched within `retry_liveness`; report fits the 30-line journal budget with 50 residents; resident → exit 1 naming the run; 09-19 self-healing shape → exit 0; alerted within re-alert interval → 0; after → 1; run disappears → dropped from state; corrupt state / DB error → exit 2 typed, no traceback.
- [ ] 5.3 Unit + timer + env example under `infra/systemd/` / `infra/env/`: journal stdio, `OnFailure=nhms-node27-unit-failure-alert@%n.service`, 30-min timer, read-only DSN. Unit-file tests in the style of the other lanes' unit pins; the `StandardError=append:` set pin handled deliberately (set unchanged since the new unit uses journal stdio; refresh its stale count docstring).
- [ ] 5.4 `tests/test_node27_autopipeline_handoff.py` docstrings no longer claim an autopipe `OnFailure=`; they point at the residency observer.
- [ ] 5.5 Runbook: new lane's purpose, threshold/re-alert/retry-liveness, disposition when it fires, the residual that a tick hung >6 h ages its failures out of the watched set (covered by the 4 h frontier-stall lane), clearing action for abandoned failures, how to enable (deployment step, not done here), which read-only role.
- [ ] 5.6 Root-cause receipt (D5r) committed, composed from the orchestrator-captured raw outputs (`2529-rootcause-raw.txt`, `2529-backlog-probe.txt`; commands + outputs verbatim), verdict exactly as D5r states (H1 tick #17 only, not sole cause; H2/H3 open; confounder), AC1 replace-chain duration marked oracle-blocked with the capture method.
- [ ] 5.7 (orchestrator) node-27 live receipt: scratch DB + transient unit with the real `OnFailure=` handler → mail sent with non-empty body naming the seeded run → clear → recover (unit succeeds). Record commands/outputs in tasks.md; scratch DB and transient units removed afterwards.

## 6. #2504 — frozen populated rows converge outside the window (D6)
- [ ] 6.1 `configured_retention_window_days(env) -> int | None` in `packages/common/storage.py`, used by the retention runner (its own default + typed validation kept) and the coverage refresh (fail-closed: `None` → no relaxation, empty expired selection). Add the variable to `infra/env/node27-ingest.example` (the env `scripts/node27_autopipe_cron.sh` sources) with a MUST-equal-retention-env comment; test that the cron's strict-source filter accepts it.
- [ ] 6.2 Guard relaxation only for `river_valid_time_end < expired_cutoff` with the cutoff anchored on the display watermark (fail-closed on watermark error); `--skip-fresh` also selects expired populated rows not refreshed within `expired_rescan_interval`.
- [ ] 6.3 Real-PostgreSQL tests (integration): out-of-window empty → lowered to 0 and selected by `--skip-fresh`; in-window empty → refused (existing #1446 legacy-cohort tests still green); out-of-window with facts → count kept; NULL end → never relaxed; watermark lagging now → the band `[watermark-window, now-window)` stays protected; watermark error → no relaxation; expired row with surviving facts is not reselected on the next tick; window variable absent → behaviour identical to today (no relaxation, no expired selection); parallel `--all` path covered.
- [ ] 6.4 `--audit-populated-empty` read-only JSON report (in_window / out_of_window / null_end buckets + watermark/cutoff), bounded (valid-time-bounded EXISTS, one short transaction per probe, statement_timeout + lock_timeout); tests on a seeded DB.
- [ ] 6.5 (orchestrator) node-27 read-only audit receipt against production (bounded, run outside the 06:36Z retention and 04:25Z compression windows) recorded; the production convergence ("the 48 runs no longer listed") is **oracle-blocked** on deployment authorization — record the exact command an operator runs after deployment and the expected audit delta.
- [ ] 6.6 Guard header, `_REFUSAL_ADVICE`, runbook updated to the decided semantics with the 000060 rationale.

## 7. #2464 — alert source allowlist (D7)
- [x] 7.1 Shared `DisplaySourceId` / `DISPLAY_SOURCE_IDS` in `packages/common/source_identity.py`; three route sites use the alias.
- [x] 7.2 `evaluate` branch → `not-evaluated` / `unsupported-source`; test with `{"gfs": (T0, T0), "era5": (T0, None)}` → exit 0 and the era5 row visible.
  - The alert resolves the set lazily in the config stage (`display_source_ids()`, same shape as `lookback_days()`, so a missing repo import path stays a typed exit 2) into `CoverageAlertConfig.display_sources`; `evaluate` checks it right after the null-source branch; `default_observe` no longer asks the catalog about a non-display key. `main(..., display_sources=...)` is a test seam used only by the two journal-budget/truncation pins, whose synthetic `src*`/`ok*`/`bad*` keys are not display sources.
  - Tests: `test_a_non_display_source_is_visible_but_never_alerts` (rc 0, `era5` row `status=not-evaluated reason=unsupported-source`, header `sources=2 evaluated=1 breaching=0`), `test_an_unsupported_source_alone_is_not_a_zero_source_failure`, evidence 20 extended with an `era5` row (catalog called for `gfs` only).
- [x] 7.3 Pin test (`typing.get_type_hints` / `inspect.signature(eval_str=True)` — the module has `from __future__ import annotations`; unwrap `| None`): allowlist equals each route's `source` annotation args; OpenAPI drift/contract tests green (no `$ref`).
  - `test_display_source_allowlist_equals_each_route_source_annotation[list_discharge_cycles|list_layer_valid_times|hydro_national_source_cycle_mvt_tile]` + `test_display_sources_are_the_shared_display_route_enum`. Mutation check: widening `list_layer_valid_times` to an inline `Literal["gfs", "ifs", "era5"] | None` reds it (`Extra items in the left set: 'era5'`); restored.
  - RED (pre-change): `9 failed, 32 passed` — `ImportError: cannot import name 'DISPLAY_SOURCE_IDS' from 'packages.common.source_identity'`, `TypeError: main() got an unexpected keyword argument 'display_sources'`, era5 case `assert 1 == 0`, evidence 20 `assert 2 == 1` (catalog asked about `era5`).
  - GREEN: `tests/test_node27_coverage_freshness_alert.py tests/test_source_identity.py` → `54 passed`; `tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_api_contract.py tests/test_api.py` + the hydro-display MVT route suites → `206 passed`. `app.openapi()` generated from HEAD and from the working tree diffed byte-identical (10 068-line JSON), so `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts` are unaffected (`check:api-types` not run: it diffs the untouched YAML).
- [x] 7.4 Main spec + runbook reason table updated; archived `node27-coverage-freshness-alert/design.md` exclusion rationale gets a pointer line (history not rewritten).
  - Spec: the requirement ships as this change's `specs/display-coverage-freshness/spec.md` ADDED delta and folds into `openspec/specs/` on archive (the main spec is not hand-edited, which would duplicate the header at archive). Runbook `docs/runbooks/production-ops/coverage-freshness-alert.md` §11.1: new bullet + `not-evaluated` reason table (`null-source`, `unsupported-source`, `outside-window`, `no-ready-frontier`). Archived design: one pointer sentence after the `__null_source__` rationale.

## 8. Verification
- [ ] 8.1 `uv run ruff check .` clean; `openspec validate node27-ops-scripts-fail-closed --strict --no-interactive` valid.
- [ ] 8.2 Focused suites green locally: retention, raw retention, mvt cache retention, autopipeline preflight/handoff, refresh coverage (+ CLI, parallel), coverage-freshness alert, hydro display/API/OpenAPI tests touched by the alias, new observer module, `tests/test_select_ci_tests.py`.
- [ ] 8.3 (orchestrator) node-27 isolated oracle at the PR head (scratch PG, `NHMS_RUN_INTEGRATION=1`) for the same suites incl. the integration-marked coverage tests.
- [ ] 8.5 PR body carries the design §PR-body checklist items.
- [ ] 8.4 CI green on the PR head (or red with the cause recorded).
