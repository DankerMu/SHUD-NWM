# Tasks

## 1. Repository

- [x] 1.1 `infra/env/compute.scheduler-dbfree.env.example`: `NHMS_SCHEDULER_CYCLE_LAG_HOURS=10`, with a comment giving
      the rationale (measured completion, deferral of incomplete cycles, #3225) and the `LOOKBACK + LAG <= 48 h` bound.
- [x] 1.2 `services/orchestrator/operator_action_listing.py`: the production window values are quoted at three sites —
      `LIST_OPERATOR_ACTIONS_HELP` at `:310` ("lookback_hours=32 with cycle_lag_hours=16 ... the 16h lag") and `:312`
      ("production 32, code default 24"), and the docstring at `:733` ("production 32, code default 24"). All three
      state the window boundary without a production value. The code default (24) may stay: it is a code fact.
- [x] 1.3 Runbooks that quote the production lag. Sentences are rewritten, not number-swapped:
      - `docs/runbooks/node22-control-plane-manual-recovery.md:208-209` (boundary (1) of exit 0);
      - `docs/runbooks/production-ops/recalibration-and-archive.md:311-313`: the release cutoff `t*` is `C + 10h`;
      - `docs/runbooks/tier-node27-timeseries-storage.md:4894-4898`: the dual constraint is `32 + 10 = 42 h <= 48 h`,
        no longer "aligned with" the compression lag; the advice for an outage longer than the window stays;
      - `docs/runbooks/production-ops/service-bringup.md:868`: same, `LOOKBACK=32` + `LAG=10` = 42 h <= 48 h;
      - `docs/runbooks/canonical-precip-mirror-backfill.md:139` ("默认 16").
      - `docs/runbooks/production-ops/known-issues-state-index.md:797` ("node-22 lag 16：下限 400 h"; the file is
        excluded from E3 for its worked-example table, so this line is listed here; found in review).
      Receipts under `docs/runbooks/receipts/` are history and stay as written.
- [x] 1.4 Tests:
      - help text: parse `NHMS_SCHEDULER_LOOKBACK_HOURS` and `NHMS_SCHEDULER_CYCLE_LAG_HOURS` from the template with
        `tests.test_env_templates.parse_env_template`; assert `LIST_OPERATOR_ACTIONS_HELP` names
        `cycle_window.start_time_utc`, `cycle_window.end_time_utc`, `lookback_hours` and `cycle_lag_hours`, contains
        no match of `(lookback_hours|cycle_lag_hours)\s*=\s*\d` and no match of `production \d+`, and still contains
        `--lookback-hours 0` (the degenerate window the existing test at `tests/test_operator_action_listing.py:2474ff`
        asserts);
      - newest cycle not ready (the steady state a smaller lag makes reachable; the existing test at
        `tests/test_production_scheduler.py:32356` covers the reverse shape): backfill enabled, one source, the only
        gap is the newest cycle with `available=False`, reason `nfs_raw_manifest_raw_files_missing`, status `invalid`.
        Assert pass status `planned`, `candidate_count == 0`, `blocked_candidate_count == 0`, no orchestrator call,
        exactly one source-cycle entry for that cycle with `selection_status == "not_selected"` and
        `selection_reason == "source_cycle_unavailable_does_not_consume_source_budget"`, and the audit row
        `gap_count == 1`, `unavailable_gap_count == 1`, `selected_count == 0`. A second case adds an older complete
        cycle is NOT required (completion needs a journal fixture); the one-cycle window is the claim under test.
- [x] 1.5 Receipt `docs/runbooks/receipts/2026-11-19-scheduler-cycle-lag-10h.md` with the measurement table, the
      node-22 apply log and the first passes under the new lag.

## 2. Production (orchestrator, node-22)

- [x] 2.1 Apply at a pass boundary with the `gap_count = 0` pre-check; backup outside the env dir.
- [x] 2.2 First pass under the new lag is `planned`/`ready`; the first eligible cycle is selected and submitted.

## Evidence Floor

- E1 `uv run ruff check . --no-cache`; `uv run pytest -q tests/test_operator_action_listing.py
     tests/test_env_templates.py tests/test_node22_scheduler_env_drift_table.py` plus the file holding the new
     newest-cycle test pass locally.
- E2 `openspec validate shorten-scheduler-cycle-lag --strict --no-interactive`.
- E3 this command prints nothing:
     `git grep -n -E 'cycle_lag_hours=16|LAG(_HOURS)?=16|32 \+ 16|C \+ 16h|now − 16h|16h lag|16 小时|默认 16|cycle_lag=16h|lookback=96h'
     -- ':!tests' ':!docs/runbooks/receipts' ':!openspec/changes' ':!docs/review-loop-log.jsonl'
     ':!docs/runbooks/production-ops/known-issues-state-index.md' ':!openspec/specs/file-state-snapshot-index'`.
     The exclusions are history or worked examples, not statements of the production value: the review log, the
     state-index runbook's `16`/`119` examples (`:563`), the state-index spec's scenario values (`:943`), tests
     (fixture values), receipts, and this change. `openspec/specs/production-scheduler-orchestration/spec.md` is
     covered once the change is archived.
- E4 node-22: apply log; `cycle_window.cycle_lag_hours = 10` in pass evidence; the first newly eligible cycle selected
     for both sources and its pass completes.
- E5 node-27: display frontier landing time before (T+16.4 h .. T+18.9 h) and after, recorded on the issue.
- E6 (issue body, two-day observation) no cycle stuck `unavailable`: recorded on the issue after two days, not a
     merge gate.
