## Why

node-22 production stalled for >30h (issue #2655): on this explicitly comment-less cluster (`AccountingStoreFlags=(null)`) the #1565 name-window fallback can only count `nhms_forecast` masters inside `[attempt anchor, now]`. The scheduler submits the gfs and IFS forecast cohorts seconds apart, so each reserved-unbound master sees both arrays (56823/56839) and is refused as `ambiguous_fallback_match` forever — even though both arrays are `COMPLETED` and `sacct SubmitLine` literally carries `--comment=nhms_idem:<idempotency_key>` for every array-task row. The held `reserved` master keeps its 47 members "active" (`active_duplicate_pipeline`), backfill waits on the prior cycle, and both sources freeze. The same shape has held two older masters (ifs 2026091100, gfs 2026091212) for two weeks, and the widening `now`-ended window guarantees recurrence whenever the two sources submit in one pass.

## What Changes

- The comment-less fallback query additionally requests `SubmitLine` (appended last; parsed as the remainder of the `|`-split row). An eligible forecast-family row whose SubmitLine carries exactly one distinct `--comment=` value yields a per-master **submit-line key**.
- Classification uses the key before the two-master cap: a master whose key differs from the reservation's `nhms_idem:<idempotency_key>` comment is excluded (it is provably another job); when every remaining master carries a key, exactly one master equal to the reservation's comment is a unique candidate, and two or more equal masters stay `ambiguous_fallback_match` (a genuine double submission). Any remaining master without a parsable key keeps today's pure count semantics (fail-closed).
- Durable claimant exclusivity: a candidate proven by an exact submit-line key is claimed only by the reservation whose own idempotency comment equals that key; overlapping-window siblings with a different key are not claimants for it. Candidates without a proven key keep today's window-overlap rule unchanged. Active-owner and same-accounting-incarnation occupancy gates are unchanged.
- The successful bind reuses the existing `slurm_binding_source=slurm_name_window_unique` / `matched_bound` durable tuple (no new durable token — rollback-safe for the node-22 checkout); pass evidence gains an additive `fallback_match_basis` (`submitline_exact` | `name_window_count`) on reserved-unbound fallback outcomes.
- The forecast ambiguous-submit branch persists `error_code` / `error_message` on the durable transition, as the forcing branch already does.
- Runbooks route the stall symptom (`active_duplicate_pipeline` + reserved-held forecast master + `ambiguous_fallback_match`) to the fallback section and document the completed-but-unbound case.

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (issue says implementation-ready; expanded because it changes parser/persisted-state/concurrency contracts on the production scheduler)
Blast radius: a wrong bind attaches a reservation to another cohort's Slurm job (silent wrong terminal projection) or double-binds one master; a wrong exclusion re-opens the #1116 double-submission hazard.
Selected risk packs: Public API/CLI/script entry; Schema/field names; Concurrency/shared state; Resource limits; Legacy compatibility; Error handling; Documentation; Slurm production lifecycle; Run manifest/QC provenance
Evidence floor: focused gateway-reconcile + chain suites, full `uv run pytest -q`, `uv run ruff check .`, strict OpenSpec, node-22 scratch-journal rehearsal + first live pass receipt
```

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `pipeline-job-persistence`: amend the comment-less fallback classification and durable claimant rule in "Comment-based absence proof requires proven comment accounting capability".

## Impact

- Code: `services/orchestrator/reconcile.py`, `services/orchestrator/file_orchestration_journal.py`, `services/orchestrator/chain_stage_execution.py` (and scheduler evidence serialization if it filters outcome keys).
- Tests: `tests/test_gateway_reconcile_claimant_exclusivity.py`, `tests/test_gateway_reconcile_comment_accounting.py`, `tests/test_gateway_reconcile_comment_sacct_bounds.py`, chain ambiguous-submit tests.
- Operations: `docs/runbooks/failed-basin-retry.md`, `docs/runbooks/node22-control-plane-manual-recovery.md`, `docs/runbooks/scheduler-dbfree-typed-reasons.md`; node-22 deploy is `git pull --ff-only` (the timer picks it up; no dependency change, no `uv sync`).
- No DB schema, display API, frontend, gateway, or sbatch-template change.
