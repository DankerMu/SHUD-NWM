# Proposal — decompose-reserved-reconcile (#3176)

Closes #3176.

## Why

`reconcile_reserved_unbound_jobs` (`services/orchestrator/reconcile_reserved.py:62-1068`) is a single 1007-line function:
- one `for job in reserved_jobs:` loop around a `try` / `except FileOrchestrationJournalError`;
- 27 `outcomes.append(...)` exits;
- a `nonlocal` closure that caches the per-pass comment capability;
- `fallback_basis`, assigned inside the `try` and read by the `except`.

#3155 moved it whole and gave its module the only `services/orchestrator/reconcile*.py` entry in `.large-file-guard.json`
(`openspec/specs/orchestrator-reconcile-module-partition/spec.md`, "Only the indivisible function is exempt"). While
the exclusion exists, the module can grow unnoticed. It holds the submit-crash-window state machine: reserved →
submitted / reservation_lost / absence_unconfirmed / identity-blocked release. That state machine is the core of
double-submit safety, and one unreviewable body is the riskiest place for it.

## What

1. **Pin commit (unchanged production code).**
   - Measure line coverage of `reconcile_reserved.py` under the issue's verification set on master. Every one of the 27
     `outcomes.append` exits, and both escaping raises (`:860` accepted-submit commit API unavailable, `:977` forcing
     bind API unavailable), must be hit.
   - Each exit that is not hit gets a characterization test that is green on the old code.
   - Add a recording-store write-order test: for rows that write more than once, it asserts the ordered call trace (the
     `submit_outcome is None` transition at `:225`/`:243`, then the accounting query, then the record / commit / permit /
     bind writes).
   - The new tests go in a new file under 1000 lines, with selector routing.
2. **Move commit (behaviour-preserving decomposition).**
   - Extract the per-row body into `_reconcile_one_reserved_unbound_job(job, *, context, row) -> ReservationReconcileOutcome`.
   - The pass context carries `store`, `comment_query`, `grace`, `accepted_submit_grace`,
     `identity_blocked_streak_limit`, `now` (still a callable), `bind_only`, the three `target_*` pins, and the lazy
     at-most-once capability provider.
   - The row state carries `fallback_basis` out of the `try`.
   - Phase helpers are verbatim slices of the original body, in original order. Each helper unpacks the context into
     the original local names, so the moved text is identical up to indentation.
   - Only two rewrites are allowed:
     - `outcomes.append(X); continue` → `return X`;
     - `fallback_basis = …` → `row.fallback_basis = …`.
   - Shared blocks keep their guards verbatim. The confirmed-absent block (`:708-823`) is shared, and its
     `update_job_status` `else` (`:785`) is reachable by an accepted-submit row whose store lacks
     `permit_pipeline_job_retry`.
   - No exit is added, removed or merged. No defensive branch is added.
3. **Module layout.**
   - `reconcile_reserved.py` comes down to ≤1000 lines, and its `.large-file-guard.json` entry is removed in the commit
     where it first fits.
   - The call site of the patched seam `_reserved_record_identity_matches` stays in `reconcile_reserved.py`
     (`tests/test_gateway_reconcile_comment_accounting.py:1563` patches it there). That means the identity-gate phase
     stays in that module.
   - Seam-free phases may move to the existing `reconcile_reserved_support.py` (382 lines), or to a new
     `services/orchestrator/reconcile_<topic>.py` leaf.
   - Any module that ends up holding `ReservationReconcileOutcome(...)` calls or reconcile logging must match
     `reconcile*.py` (the held-reservation vocabulary pin globs it, `tests/test_operator_action_listing_held_reservations.py:36-41`)
     and use the `services.orchestrator.reconcile` logger (`tests/test_reconcile_partition_logger.py`).
   - The facade `services/orchestrator/reconcile.py:144-147` keeps re-exporting both names. Leaves never import the
     facade.
4. **Spec.** MODIFY `orchestrator-reconcile-module-partition`: no partition module is excluded from the guard.

## Non-goals

- No behaviour change: outcome values, write order, exception scope and probe laziness all stay the same.
- No change to `reconcile_reserved_support.py` helpers' behaviour, the comment-accounting proof, or the stores.
- No renaming of public outcome vocabulary.

## Must preserve

- For every keyed row, the outcome sequence and each outcome's full field set: `action`, `status`,
  `reconciliation_source`, `reconciliation_decision`, `reconciliation_reason_class`, `match_count`,
  `matched_slurm_job_id`, `slurm_job_id`, `durable_write_kind`, `durable_write_count`, `fallback_match_basis`,
  `quarantine_*`.
- Rows with no `idempotency_key` produce no outcome.
- The order of store and journal writes within a row and across rows.
- Per-row `FileOrchestrationJournalError` quarantine, carrying `fallback_match_basis` when it was classified before
  the fault; one row's fault does not abort the others.
- `ReconcileQueryUnavailable` from the accounting query is handled per row (`query_unavailable`). The one raised
  from the commit or bind step (`:860`, `:977`) still escapes the whole function.
- The capability probe is lazy, runs at most once per invocation, and never runs for an empty selection or for rows
  that exit before lane selection.
- `now()` is called at the same points (deferral checks, `_identity_blocked_outcome`), never snapshotted.
- Import surface: `services.orchestrator.reconcile` and `services.orchestrator.reconcile_reserved` still expose
  `reconcile_reserved_unbound_jobs` and `_reserved_record_identity_matches`. Patching the latter on
  `reconcile_reserved` still takes effect.

## Fixture level

expanded. Seats: correctness, test evidence + spec compliance, invariant/state machine.

## Evidence Floor

- [ ] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`;
  `wc -l services/orchestrator/reconcile*.py` all ≤1000; no `reconcile*` entry in `.large-file-guard.json`.
- [ ] E2 Pin commit: the coverage report on unchanged code shows all 27 exits and both escaping raises hit (the
  exit → test map is recorded). The new characterization and write-order tests are green on the unchanged code.
- [ ] E3 Move commit: a normalized body diff (dedent, then compare the original body with the concatenated phase
  helpers) lists every changed line, and each changed line is one of the two allowed rewrites or a context unpack. The
  exit count is 27 before and after.
- [ ] E4 Mutation reds (each restored afterwards), from design.md "Hazards":
  - `now` snapshotted once;
  - capability probe eager, or uncached per row;
  - `fallback_basis` dropped from the quarantine outcome;
  - the `:860` raise caught per row;
  - the `_reserved_record_identity_matches` call site moved off `reconcile_reserved` (the `:1563` patch test goes red);
  - two phase helpers swapped in order.
- [ ] E5 The issue's verification set passes locally and in a node-27 temporary worktree:
  `tests/test_gateway_reconcile_*.py`, `tests/test_orchestrator_bind_reserved_job_*.py`,
  `tests/test_orchestrator_demote_*.py`, `tests/test_forcing_submit_ambiguity.py`, `tests/test_production_scheduler.py`,
  `tests/test_reconcile_partition_logger.py`, `tests/test_operator_action_listing_held_reservations.py`, the new pin
  file, and the selector set for the diff.
- [ ] E6 `openspec validate decompose-reserved-reconcile --strict --no-interactive`.
