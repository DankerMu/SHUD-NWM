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

Evidence: node-27 at `d29045eb` (first runner head): 15 DB-free + 8 real-DB passed. The six
mutants give 6/1/2/1/1/1 failures, then the source is restored clean; later runner changes only add the read-only plan artifact check. The row-lock aged-out assertion was added
afterwards, together with the plan artifact check: 23 passed at `18b80827`. The production import check ran the
runner with `PYTHONPATH=/home/nwm/NWM` at `415cbd1e` (`import_ok`, parser from the live tree). Read-only production
`plan` at `18b80827`: 3770 candidates, 0 artifacts missing, 3.56 B estimated rows (design Context).

- [x] 1.3 Pilot 1 fix: serial chunk seeding and transient requeue (design D5a), with DB-free seed-cover, cause-chain and
      dispatch-cap tests plus real-DB seeded-after-`drop_chunks` and held-`SHARE ROW EXCLUSIVE` requeue cases.
      Evidence: node-27 at `d17039a2`, 28 passed. Mutants (no seeds / no transient classification / no serial seed
      cap / transient counted failed) give 2/2/2/1 failures, then the source is restored clean.

## 2. Documentation and gate

- [x] 2.1 Amend the parent `timeseries-narrow-store-expand-contract` contract requirement, rollout gate, design and
      tasks §6 to "zero legacy-routed runs inside the retention window".
- [x] 2.2 Runbook `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.6: plan, pilot, full run, verify, post-run
      compression and governance check.

## 3. Production (separate GO)

- [x] 3.1 Publish exact runner bytes; `plan` receipt; pilot `run --limit 20 --concurrency 4` with measured throughput.
      Pilot 1 (`7b5c959e`, runner sha256 `5a145389…`): `failure_budget` stop; 1 reparsed (1 284 192 rows, 104 s),
      12 rolled back by lock collisions (D5a), all verified still `legacy`/`published` with their prior `parsed_at`
      and no narrow rows. Pilot 2 follows with the D5a runner.

      Pilot 2 (D5a runner, `tool_version` `node27-river-narrow-reparse-backfill/2`, `tool_sha256`
      `beb752eaaec8e5643188f7c270685531fcd2188a077ce1055f8c3d79ceaf4ea6`, `parser_sha256`
      `59b8d93ddd75f657881c523f1517a336d1319f602c3dea100ea90b78503123da`, `window_days` 21):
      `run --limit 20 --concurrency 4`, 2026-09-15T03:54:22Z→04:02:04Z (462 s), `result` `complete`,
      20/20 reparsed, 21 218 232 rows → **45 927 rows/s**, `transient_retries` 0, no rollback.
      `verify` immediately after: 20/20 `pass`, 0 mismatched rows.
      Receipts: `evidence/receipts/pilot1-summary-20260915T033711Z.json`,
      `pilot2-summary-20260915T035422Z.json`, `pilot2-verify-20260915T040231Z.json`.

      Concurrency/WAL tuning steps before the full run, same runner bytes, both `complete` with
      `transient_retries` 0: `--concurrency 8` with the raised WAL settings, 64/64 reparsed, 65 256 744 rows in
      975 s (**66 930 rows/s**); `--concurrency 16`, 128/128 reparsed, 125 725 992 rows in 1 676 s
      (**75 016 rows/s**). Receipts `tuning-c8wal-summary-20260915T051543Z.json`,
      `tuning-c16-summary-20260915T062335Z.json`. 16 was chosen for the full run.

      No standalone `plan` receipt file is kept: the runner embeds its plan output in every summary
      (`candidates`, `estimated_rows`, `window_days`, `legacy_route_counts_before`), so the archived summaries
      carry it without a second artifact that could drift from the run it describes.

- [x] 3.2 Full run(s) until the in-window legacy route count is 0; `verify --sample 50` pass; compression tick and
      governance receipt after release. Archive the receipts under this change.

      Two sessions, `--concurrency 16`, identical runner and parser hashes to 3.1:

      | session | window | result | candidates | reparsed | rows | throughput |
      |---|---|---|---|---|---|---|
      | 1 | 2026-09-15T07:10:17Z→15:14:15Z (28 438 s) | `partial`, `stop_reason` `deadline` | 3493 | 2039 | 2 031 695 400 | 71 442 rows/s |
      | 2 | 2026-09-15T15:14:16Z→19:56:15Z (16 919 s) | `complete`, `stop_reason` `exhausted` | 1409 | 1409 | 1 229 253 312 | 72 655 rows/s |

      Totals: **3448 runs, 3 260 948 712 rows, 12 h 46 min wall, 70 955 rows/s, 0 failures,
      `transient_retries` 0 in both sessions.** Session 1 seeded three missing chunk days serially
      (`seed_runs`, D5a) and left `not_dispatched` 1454; session 2 planned 1409 of those, the 45-run difference
      being candidates that aged out of the 21-day window — consistent with out-of-window legacy runs rising
      2874 → 2919 over the same interval.

      Gate: `legacy_route_counts_after` in the session-2 receipt contains **no `in_window: true` row** — the
      in-window legacy route count is 0. Only out-of-window rows remain (2685 `published`, 234 `superseded`),
      which the 21-day retention window removes on its own schedule and which the amended #1988 gate excludes.

      `verify --sample 50` at 2026-09-15T20:01:24Z: **50/50 `pass`, 0 mismatched rows** across all sampled runs.

      First post-release compression tick, 2026-09-16T04:25:32Z→04:31:42Z (6 min 10 s) against a 3900 s wrapper
      wall: `outcome` `clean`, 4 chunks committed, 7.42 GB → 2.0 GB (3.7×). The narrow hypertable is 35 chunks,
      4 compressed as of 2026-09-16; the daily lane's `per_tick_bound` of 4 clears the rest on its own cadence.
      Disk at that point: `/data/GHDC` 2.0 T used of 15 T.

      Receipts: `evidence/receipts/full-summary-20260915T071017Z.json`,
      `full-summary-20260915T151416Z.json`, `full-verify-20260915T200124Z.json`.
