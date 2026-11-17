# Design — decompose-reserved-reconcile (#3176)

## Governing invariant

For every input (store state, accounting answers, `now()` values, pins), the decomposed function gives the same ordered
outcome list, the same ordered sequence of store and journal calls, and the same escaping exceptions as the original
body. The decomposition only moves text. It does not re-derive logic.

## Shape

```
reconcile_reserved_unbound_jobs(store, *, …)          # signature unchanged
    reserved_jobs = …                                  # selection unchanged (:132-144)
    context = _ReservedPassContext(…, comment_capability=<lazy provider>)
    for job in reserved_jobs:
        idempotency_key = job.idempotency_key         # kept: the quarantine log and outcome use it
        if not idempotency_key: continue
        row = _ReservedRowState()                     # fallback_basis: str | None = None
        try:
            outcomes.append(_reconcile_one_reserved_unbound_job(job, context=context, row=row))
        except FileOrchestrationJournalError as error:
            LOGGER.warning(…)                          # same message and args
            outcomes.append(<journal_quarantined outcome, fallback_match_basis=row.fallback_basis>)
    return outcomes
```

- **Lazy provider.** The `nonlocal` closure becomes a small object, or a closure kept in the outer function, that
  calls `_comment_query_capability(comment_query)` at most once, on first use. It is never called at construction.
- **`outcome_floor`.** In the original, every append except the last (`:1009`, the end of the loop body) is
  immediately followed by `continue`, and a raise inside an append's argument means no append. So `len(outcomes) == outcome_floor` is always true in the `except`. After the
  rewrite every exit returns, so exactly one outcome per keyed row is structural. This is stated in a comment. The
  write-order and quarantine tests assert exactly one outcome per keyed row; the property is not silently dropped.
- **Exit control.** The per-row function returns the outcome where the original appended and continued. The moved
  text is otherwise unchanged. Phase helpers return `ReservationReconcileOutcome | None`, where `None` means "fall
  through to the next phase". Values computed in one phase and used later (`accepted_submit_reconcile`,
  `forcing_submit_reconcile`, `trusted_submit_reconcile`, `attempt_anchor`, `proof`, `record`, `fallback_unique`,
  `forcing_reconciliation_source`) travel in an explicit per-row facts object, or are returned as a tuple, whichever
  keeps the moved text closer to verbatim.

Phases, in original order (line ranges on master):

| Phase | Lines | Exits |
|---|---|---|
| lane classification and pre-query exits (bind_only non-forcing, legacy unversioned, file cohort not accepted, forcing bind_only with `submit_outcome is None` → `query_unavailable` / `slurm_exact_comment` at :230-242), and the `submit_outcome is None` transitions | :167-247 | 4 |
| accounting query with the per-row transient `except ReconcileQueryUnavailable` (wraps only :248-287) | :248-330 | 1 |
| proof-kind dispatch (coverage incomplete ×2, forcing non-match kinds, accepted-submit ambiguous / foreign / unparsable / no-match / fallback-ambiguous) | :331-518 | 8 |
| record selection and identity gates (calls `_reserved_record_identity_matches`; stays in `reconcile_reserved.py`) | :519-602 | 3 |
| forcing absence branch | :603-683 | 3 |
| fallback-unique no-record and confirmed-absent demotion | :684-823 | 3 |
| final fallback comment guard | :825-855 | 1 |
| commit / bind (accepted-submit commit with refusal mapping, forcing bind, legacy bind) and the bound outcome | :857-1046 | 3 |
| journal quarantine (outer loop `except`) | :1047-1066 | 1 |

Total 27 outcome producers (24 `ReservationReconcileOutcome(` + 3 `_identity_blocked_outcome(`). E3 counts them
before and after.

## Hazards (each paired with a mutation in E4)

1. **`now` is a callable.** It is called at `:630`, `:741` and inside `_identity_blocked_outcome`. Snapshotting it
   into the context changes the deferral decisions of later rows in a long pass.
2. **Capability probe.** It must stay lazy (never at context construction, never for an empty selection or for rows
   that exit before lane selection) and must run at most once per invocation.
3. **`fallback_basis`.** It is written to row state at `:496`, before the `_record_file_reconciliation` that can
   raise `FileOrchestrationJournalError`, and at `:530`, before the commit-time scan (#2655). The quarantine outcome
   reads it from row state.
4. **Exception scope.** The inner `except ReconcileQueryUnavailable` wraps only the accounting query (`:249-287`).
   The raises at `:860` and `:977`, and any `ReconcileQueryUnavailable` from a store committer or binder, are outside
   it and outside the journal `except`, so they abort the pass. A helper boundary must not widen either `except`.
5. **Patch seam.** `_reserved_record_identity_matches` is looked up in `reconcile_reserved`'s globals at call time.
   Its call site stays in that module.
6. **Write order.** Phases run in original order. No write moves across a phase boundary.
7. **`fallback_basis` reads.** The local stays bound in every helper that reads it (`:515`, `:565`, `:852`, `:897`,
   `:953`, `:968`, `:1044`). It is assigned as `fallback_basis = row.fallback_basis = …`, or unpacked from row state
   at the top of a later helper. `:897` decides whether `fallback_submitline_key` is passed to the commit.
8. **Vocabulary closure.** `action = …` (`:384`, `:928`) stays in the same function as the
   `ReservationReconcileOutcome(action=action)` that uses it, as a plain unannotated assignment. The constructor name
   is not aliased (`tests/test_operator_action_listing_held_reservations.py:108-157`).
9. **Recording without perturbation.** The write-order test records through exact-signature subclasses or
   `functools.wraps`, because `_callable_accepts_keyword` (`reconcile_records.py:363-370`) and the `getattr` probes
   choose branches from the store's real surface.

## Module layout

- `reconcile_reserved.py` keeps:
  - `reconcile_reserved_unbound_jobs`;
  - the per-row function and the identity-gate phase (the seam caller);
  - `_reserved_record_identity_matches`.
- The seam-free phases go to a new `services/orchestrator/reconcile_reserved_phases.py`, because
  `reconcile_reserved_support.py` (382 lines) cannot take them and stay under 1000. The name matches the
  `reconcile*.py` glob pins, and the module uses the reconcile `LOGGER` if it logs.
- New modules must import only from leaves, never from the facade, to avoid an import cycle. Check
  `tests/test_import_layering_guard.py`, the `Any`-ratchet baseline (`tests/test_any_ratchet.py`), the status-literal
  scans (`tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`), and the `pyproject.toml`
  per-file-ignores, which key on file paths.
- A new module is added to the selector wherever `reconcile_reserved.py` / `reconcile_reserved_support.py` are named:
  the sorted `RECONCILE_PARTITION_LEAVES` (`tests/test_select_ci_tests.py:12457-12466`), whose drift guard is at
  `:12549` and whose leaf ⊇ facade selection rule is at `:12554-12558`, and `scripts/select_ci_tests_rules.py`. Each
  gets a red-before-green pin.
- If the new pin file imports `tests/gateway_reconcile_helpers.py` at module scope, add it to the exact-set assertion
  at `tests/test_select_ci_tests.py:17979-18008`.
- `Any` ratchet: the global ceiling is `tests/any_ratchet_ceiling.json`. Type the context and facts precisely, and
  avoid adding `Any`.

## Readers and sibling surfaces

- Callers: `services/orchestrator/reconcile.py:144-147` (facade), the scheduler runtime, and the operator recovery
  scripts through the facade.
- Tests that read source text:
  - `tests/test_operator_action_listing_held_reservations.py` (vocabulary closure over `reconcile*.py`);
  - `tests/test_reconcile_partition_logger.py`;
  - line references: none exist. No file cites `reconcile_reserved.py:NNN`, `line_reference_baseline.json` has no
    reconcile entry, and the `pyproject.toml` per-file-ignores and the status-literal allowlist do not name these files.
- Spec: `orchestrator-reconcile-module-partition` (REMOVED + ADDED).
