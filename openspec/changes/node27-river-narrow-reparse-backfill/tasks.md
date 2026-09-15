## 1. Runner and oracle

- [x] 1.1 Implement `scripts/node27_river_narrow_reparse_backfill.py` (`plan`, `run`, `verify`; design D1–D7).
- [x] 1.2 DB-free contract tests `tests/test_node27_river_narrow_reparse_backfill.py` and disposable real-DB oracle
      `tests/test_node27_river_narrow_reparse_backfill_integration.py`.

Scope: the runner, its two test files, this change, the parent contract-gate amendment and runbook §4.10.6. No
migration, parser, reader or lifecycle code change.

Minimal mergeable slice: atomic. The runner without the gate amendment would leave the contract gated on chunk aging,
and the amendment without the runner has nothing to satisfy it.

Evidence Floor:

- Real PG15 + Timescale disposable database (node-27 production image): in-window `published` and `superseded` runs
  reparsed with status preserved, `parsed_at` advanced, legacy facts untouched, `verify` pass; aged-out and `parsed`
  runs stay `legacy`; a rerun finds zero candidates.
- A missing artifact and an injected post-parse mismatch each roll back to the exact prior row state.
- Failure budget, deadline, `--limit` newest-first, `--end-time-after`, and compressed-overlap decompress (and its
  budget refusal) behave as specified; a held lifecycle mutex refuses with no change.
- Mutants (route flip removed, row-lock window check removed, `parsed` made eligible, commit despite mismatch,
  decompress skipped, candidate window removed) each turn the suite red; restored source is clean.
- Source audit: no `DELETE FROM`, `DROP`, `TRUNCATE`, legacy demotion or `reset-failed` in the runner.

Verification: on node-27, a worktree at the PR head, with the `415cbd1e` staged interpreter against disposable
container `nwm-i8-1987-window-volume-6fb30b552`:
`NHMS_RUN_INTEGRATION=1 python -m pytest tests/test_node27_river_narrow_reparse_backfill.py tests/test_node27_river_narrow_reparse_backfill_integration.py`,
plus the mutant driver.

Hygiene: `uv run ruff check scripts/node27_river_narrow_reparse_backfill.py tests/test_node27_river_narrow_reparse_backfill*.py`;
`openspec validate node27-river-narrow-reparse-backfill --strict --no-interactive`.

Evidence: node-27 at `d29045eb` (pre-docs head; runner bytes unchanged since): 15 DB-free + 8 real-DB passed. The six
mutants give 6/1/2/1/1/1 failures, then the source is restored clean. The row-lock aged-out assertion was added
afterwards and is re-run at the PR head (see PR body).

## 2. Documentation and gate

- [x] 2.1 Amend the parent `timeseries-narrow-store-expand-contract` contract requirement, rollout gate, design and
      tasks §6 to "zero legacy-routed runs inside the retention window".
- [x] 2.2 Runbook `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.6: plan, pilot, full run, verify, post-run
      compression and governance check.

## 3. Production (separate GO)

- [ ] 3.1 Publish exact runner bytes; `plan` receipt; pilot `run --limit 20 --concurrency 4` with measured throughput.
- [ ] 3.2 Full run(s) until the in-window legacy route count is 0; `verify --sample 50` pass; compression tick and
      governance receipt after release. Archive the receipts under this change.
