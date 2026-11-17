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
        if not job.idempotency_key: continue
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
- **`outcome_floor`.** In the original, every append is immediately followed by `continue`, and a raise inside an
  append's argument means no append. So `len(outcomes) == outcome_floor` is always true in the `except`. After the
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
| lane classification and pre-query exits (bind_only non-forcing, legacy unversioned, file cohort not accepted, forcing bind_only no outcome), and the `submit_outcome is None` transitions | :167-247 | 4 |
| accounting query with the per-row transient `except ReconcileQueryUnavailable` (wraps only :248-287) | :248-330 | 1 |
| proof-kind dispatch (coverage incomplete ×2, forcing non-match kinds, accepted-submit ambiguous / foreign / unparsable / no-match / fallback-ambiguous) | :331-518 | 8 |
| record selection and identity gates (calls `_reserved_record_identity_matches`; stays in `reconcile_reserved.py`) | :519-602 | 3 |
| forcing absence branch | :603-683 | 3 |
| fallback-unique no-record and confirmed-absent demotion | :684-823 | 3 |
| final fallback comment guard | :825-855 | 1 |
| commit / bind (accepted-submit commit with refusal mapping, forcing bind, legacy bind) and the bound outcome | :857-1046 | 3 |

The exit counts are the implementer's to verify and record. E3 requires 27 before and after.

## Hazards (each paired with a mutation in E4)

1. **`now` is a callable.** It is called at `:630`, `:741` and inside `_identity_blocked_outcome`. Snapshotting it
   into the context changes the deferral decisions of later rows in a long pass.
2. **Capability probe.** It must stay lazy (never at context construction, never for an empty selection or for rows
   that exit before lane selection) and must run at most once per invocation.
3. **`fallback_basis`.** It is written to row state at `:496`, before the `_record_file_reconciliation` that can
   raise `FileOrchestrationJournalError`, and at `:530`, before the commit-time scan (#2655). The quarantine outcome
   reads it from row state.
4. **Exception scope.** The inner `except ReconcileQueryUnavailable` wraps only the accounting query. The raises at
   `:860` and `:977` are outside it and outside the journal `except`, so they abort the pass. A helper boundary must
   not widen either `except`.
5. **Patch seam.** `_reserved_record_identity_matches` is looked up in `reconcile_reserved`'s globals at call time.
   Its call site stays in that module.
6. **Write order.** Phases run in original order. No write moves across a phase boundary.

## Module layout

- `reconcile_reserved.py` keeps:
  - `reconcile_reserved_unbound_jobs`;
  - the per-row function and the identity-gate phase (the seam caller);
  - `_reserved_record_identity_matches`.
- Seam-free phases go to `reconcile_reserved_support.py` if they fit under 1000 lines there. Otherwise they go to a
  new `services/orchestrator/reconcile_reserved_phases.py`, which matches the `reconcile*.py` glob pins and uses the
  reconcile `LOGGER` if it logs.
- New modules must import only from leaves, never from the facade, to avoid an import cycle. Check
  `tests/test_import_layering_guard.py`, the `Any`-ratchet baseline (`tests/test_any_ratchet.py`), the status-literal
  scans (`tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`), and the `pyproject.toml`
  per-file-ignores, which key on file paths.
- A new module is added to the selector wherever `reconcile_reserved.py` / `reconcile_reserved_support.py` are named
  (`tests/test_select_ci_tests.py:12463-12464`, `scripts/select_ci_tests_rules.py`), with a red-before-green pin.

## Readers and sibling surfaces

- Callers: `services/orchestrator/reconcile.py:144-147` (facade), the scheduler runtime, and the operator recovery
  scripts through the facade.
- Tests that read source text:
  - `tests/test_operator_action_listing_held_reservations.py` (vocabulary closure over `reconcile*.py`);
  - `tests/test_reconcile_partition_logger.py`;
  - any line-reference baseline (`line_reference_baseline.json`) or doc citing `reconcile_reserved.py:NNN`. Grep it,
    and repoint every reference.
- Spec: `orchestrator-reconcile-module-partition` (MODIFIED).
