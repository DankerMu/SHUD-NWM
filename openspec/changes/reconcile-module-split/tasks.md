# Tasks — reconcile-module-split (#3155, PR 2 of 2)

- [ ] 1.0 Work in the `design.md` stage order: A partition, B retire, C selector. Run the E2 move proof at the end of
  stage A, before the retire, and save its output; after B, re-run it with the two retired parsers as the only
  removals. The orchestrator commits the finished work; the end state satisfies the large-file guard.
- [ ] 1.1 Re-run the patch-target audit on the current tree (string targets, `setattr(module, ...)`, aliases,
  direct assignment, lazy facade reads in production). Reconcile it with the `design.md` table before moving anything.
- [ ] 1.2 Retire `_parse_comment_sacct_rows` and `_parse_comment_sacct_matches`. Retarget the LOW-2 cases in
  `tests/test_reconcile_sacct_parse.py` and `tests/test_gateway_reconcile_comment_accounting.py:520-577` at
  `_index_comment_sacct_matches`, using `nhms_idem:` comments. Keep their intent: a malformed JobID is skipped and
  scanning continues, and an array row normalizes to its master id. `test_reconcile_sacct_parse.py` imports from
  `reconcile_comment_sacct`.
- [ ] 1.3 Create the leaf modules per `design.md`, with byte-identical bodies and comments, and `LOGGER` named
  `services.orchestrator.reconcile`. Reduce `reconcile.py` to the facade with redundant-alias re-exports and
  `import subprocess`.
- [ ] 1.4 Retarget every moved-caller patch in tests to the owning leaf. Leave the facade-resolved patches on the facade.
- [ ] 1.5 Path-keyed consumers:
  - the `pyproject.toml` B010 per-file ignore;
  - `tests/status_literal_allowlist.json`;
  - `tests/test_operator_action_listing_held_reservations.py`, which scans every `services/orchestrator/reconcile*.py`;
  - `docs/governance/ROLE_BOUNDARY.md:67`;
  - the `reconcile.py:<n>` references listed in proposal What 6, rewritten as symbol references with no new
    `file:line` key. Remove the two baseline keys that no longer occur (`line_reference_baseline.json:83,212`).
- [ ] 1.6 Selector:
  - every new leaf gets the routes `reconcile.py` has (directory rule, positive selection floor). Add
    `runtime-budget` / importer-gap rows only for gaps the guard actually derives; a pre-emptive row trips the
    stale-exclusion check at `tests/test_select_ci_tests.py:12243`;
  - `test_reconcile_sacct_parse.py` stays in the `real_backend` one-hop set;
  - update the frozen lists and importer-gap dispositions;
  - add one assertion that a leaf-only change selects `tests/test_reconcile_sacct_parse.py` and the
    `reconcile.py`-equivalent suites.
- [ ] 1.7 Add `services/orchestrator/reconcile_reserved.py` to `.large-file-guard.json`. No other reconcile entry.
- [ ] 1.8 Write the AST move proof (E2) and the patch-target re-derivation with a mutation check (E3) as scratchpad
  scripts, and record their outputs.
- [ ] 1.9 File the follow-up issue to decompose `reconcile_reserved_unbound_jobs` and drop its guard exclusion
  (orchestrator).
