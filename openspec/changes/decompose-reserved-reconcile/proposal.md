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
   - Coverage tool: the repo does not depend on coverage, and `uv.lock` must not change. Use
     `uv run --with pytest-cov pytest --cov=services.orchestrator.reconcile_reserved --cov-branch --cov-context=test …`
     locally or on node-27, never on node-22.
   - Measure statement and branch coverage of `:155-1066` under the issue's verification set on master. Record the
     exit → test map for all 27 outcome producers (24 `ReservationReconcileOutcome(` + 3 `_identity_blocked_outcome(`)
     and both escaping raises (`:860`, `:977`). List every unhit branch (for example `:994`, the False side of `:782`,
     an accepted-submit row reaching `:785`). Each gets a characterization test that is green on the old code.
   - Every E4 mutation needs a test that is green on the old code and red under the mutation. The tests that do not
     exist today are added here:
     - a non-caching fake querier with a counting `capability()` over at least two eligible rows (the default querier
       caches, `reconcile_comment_sacct.py:332-345`, so it cannot catch a per-row re-probe);
     - a clock that advances per row across the grace boundary (no current test varies the reconcile `now`);
     - assertions on the `:860` and `:977` escaping messages;
     - the quarantine `fallback_match_basis` on the `:496`-path journal fault (only the commit path is covered today,
       `tests/test_gateway_reconcile_submitline_exact.py:403`).
   - A recording-store write-order test asserts the ordered call trace of multi-write rows. The trace includes the
     `submit_outcome is None` transition (`:225`/`:243`), the querier's `capability()` and query calls,
     `forecast_cohort_runtime_identity_matches`, and the record / commit / permit / bind writes. Constraint: recording
     must not change which branches run.
     - `_callable_accepts_keyword` inspects signatures (`reconcile_records.py:363-370`).
     - `getattr` probes optional APIs (`:185`, `:643`, `:761`, `:858`, `:975`).
     - So use subclasses with exact signatures, or `functools.wraps`; never a `**kwargs` or `__getattr__` proxy.
   - The new tests go in a new `tests/test_gateway_reconcile_<topic>.py` file under 1000 lines, so the issue's glob
     covers it, with selector routing.
2. **Move commit (behaviour-preserving decomposition).**
   - Extract the per-row body into `_reconcile_one_reserved_unbound_job(job, *, context, row) -> ReservationReconcileOutcome`.
   - The pass context carries `store`, `comment_query`, `grace`, `accepted_submit_grace`,
     `identity_blocked_streak_limit`, `now` (still a callable), `bind_only`, the three `target_*` pins, and the lazy
     at-most-once capability provider. The provider object itself is passed to `_accepted_submit_reconcile_job`; it is
     never evaluated first.
   - The row state carries `fallback_basis` out of the `try`. The outer loop keeps the `idempotency_key` local that the
     quarantine `LOGGER.warning` and the quarantine outcome use (`:1048-1060`).
   - Phase helpers are verbatim slices of the original body, in original order. Each helper unpacks the context into
     the original local names, so the moved text is identical up to indentation.
   - The allowed line changes:
     - `outcomes.append(X)` followed by `continue` → `return X`. This also covers `:1009`, the final bound append,
       which is not followed by `continue`.
     - `fallback_basis = …` → `fallback_basis = row.fallback_basis = …`. The reads at `:515`, `:565`, `:852`, `:897`,
       `:953`, `:968` and `:1044` stay unchanged.
     - helper `def` lines, `return None` fall-throughs, packing and unpacking of the context and per-row facts, and the
       per-row dispatch lines.
   - The vocabulary closure (`tests/test_operator_action_listing_held_reservations.py:108-157`) resolves a local
     `action` only through a plain `ast.Assign` in the same `FunctionDef`. So the `action = …` assignments at `:384`
     and `:928` stay in the same helper as the `ReservationReconcileOutcome(action=action)` that uses them, without an
     annotation. The constructor keeps its plain name.
   - Shared blocks keep their guards verbatim. The confirmed-absent block (`:708-823`) is shared, and its
     `update_job_status` `else` (`:785`) is reachable by an accepted-submit row whose store lacks
     `permit_pipeline_job_retry`.
   - No outcome producer is added, removed or merged. No defensive branch is added.
3. **Module layout.**
   - `reconcile_reserved.py` comes down to ≤1000 lines, and its `.large-file-guard.json` entry is removed in the commit
     where it first fits.
   - The call site of the patched seam `_reserved_record_identity_matches` stays in `reconcile_reserved.py`
     (`tests/test_gateway_reconcile_comment_accounting.py:1563` patches it there). That means the identity-gate phase
     stays in that module.
   - The seam-free phases go to a new leaf `services/orchestrator/reconcile_reserved_phases.py`. Adding them to
     `reconcile_reserved_support.py` (382 lines) would take it past 1000 lines. The new leaf goes into the sorted
     `RECONCILE_PARTITION_LEAVES` (`tests/test_select_ci_tests.py:12457-12466`, with the drift guard at `:12549` and the
     superset rule at `:12554-12558`) and into every selector rule that names the reserved leaves, with a
     red-before-green pin.
   - Any module that ends up holding `ReservationReconcileOutcome(...)` calls or reconcile logging must match
     `reconcile*.py` (the held-reservation vocabulary pin globs it, `tests/test_operator_action_listing_held_reservations.py:36-41`)
     and use the `services.orchestrator.reconcile` logger (`tests/test_reconcile_partition_logger.py`).
   - The facade `services/orchestrator/reconcile.py:144-147` keeps re-exporting both names. Leaves never import the
     facade.
4. **Spec.** In `orchestrator-reconcile-module-partition`, REMOVED + ADDED, because a MODIFIED block cannot replace
   the exemption scenario. The restated requirement allows no partition module to be excluded from the guard.

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
- `ReconcileQueryUnavailable` raised inside the accounting query (`:249-287`) is handled per row
  (`query_unavailable`). Any `ReconcileQueryUnavailable` raised outside that span still escapes the whole function:
  `:860`, `:977`, and any raised by a store committer or binder.
- The capability probe is lazy, runs at most once per invocation, and never runs for an empty selection or for rows
  that exit before lane selection.
- `now()` is called at the same points (deferral checks, `_identity_blocked_outcome`), never snapshotted.
- Import surface: `services.orchestrator.reconcile` and `services.orchestrator.reconcile_reserved` still expose
  `reconcile_reserved_unbound_jobs` and `_reserved_record_identity_matches`. Patching the latter on
  `reconcile_reserved` still takes effect.

## Fixture level

expanded. Seats: correctness, test evidence + spec compliance, invariant/state machine.

## Evidence Floor

- [x] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`;
  `wc -l services/orchestrator/reconcile*.py` all ≤1000; no `services/orchestrator/reconcile*.py` entry in
  `.large-file-guard.json` (the existing `tests/test_gateway_reconcile_*.py` entries stay).
- [x] E2 Pin commit, on unchanged code:
  - the coverage report (command in What 1) shows all 27 outcome producers and both escaping raises hit, and the
    exit → test map is recorded;
  - every remaining unhit branch of `:155-1066` is listed with a reason;
  - the new characterization, write-order and mutation-target tests are green.
- [x] E3 Move commit:
  - a normalized body diff (dedent, then compare the original body with the concatenated phase helpers) lists every
    changed line, and each is one of the allowed changes in What 2;
  - the outcome producers number 27 before and after (24 `ReservationReconcileOutcome(` + 3
    `_identity_blocked_outcome(`); after the move, 26 are reached by `return` and the quarantine one by the outer
    `except`.
- [x] E4 Mutation reds (each restored afterwards), from design.md "Hazards":
  - `now` snapshotted once;
  - capability probe eager, or uncached per row;
  - `fallback_basis` dropped from the quarantine outcome;
  - the `:860` raise caught per row;
  - the `_reserved_record_identity_matches` call site moved off `reconcile_reserved`. This is caught by the pin test
    `test_a_fallback_unique_master_with_an_invalid_slurm_id_past_the_identity_seam_is_held_as_no_match`, not by the
    `:1563` patch test. That test stays green under the mutation, because the real identity gate rejects the same row
    with the same visible outcome, so it cannot tell whether the patch took effect;
  - two phase helpers swapped in order.

  Each mutation is mapped to the test that catches it, and that test is green on the unchanged code.
- [x] E5 The issue's verification set passes locally and in a node-27 temporary worktree:
  `tests/test_gateway_reconcile_*.py`, `tests/test_orchestrator_bind_reserved_job_*.py`,
  `tests/test_orchestrator_demote_*.py`, `tests/test_forcing_submit_ambiguity.py`, `tests/test_production_scheduler.py`,
  `tests/test_reconcile_partition_logger.py`, `tests/test_operator_action_listing_held_reservations.py`, the new pin
  file, and the selector set for the diff.
- [x] E6 `openspec validate decompose-reserved-reconcile --strict --no-interactive`.
