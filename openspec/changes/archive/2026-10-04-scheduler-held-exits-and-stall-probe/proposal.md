# Scheduler held-row exits and stall probe: four node-22 correctness gaps

## Triage

```text
Issue type: bugfix (batch S: #2662, #2670, #2682, #2660)
Fixture level: expanded
Upstream suggested level: absent (orchestrator selects expanded: persisted scheduler state, CAS write,
  new operator exit, alert-predicate parity, rollback fence)
Blast radius: a production stall reported healthy; an operator retry that re-stages the same broken
  forcing package; a second writer over shared forcing artifacts (double-write); a rollback quiescence
  check that misses an unsettled job.
Selected risk packs: Public API / CLI / script entry; Concurrency / shared state / ordering;
  Error handling / rollback / partial outputs; Auth / permissions / secrets (operator attestation);
  Legacy compatibility / examples; Documentation / migration notes
Evidence floor: uv run ruff check .; the targeted pytest files named in tasks.md; CI green.
  No node-22 deployment and no node-27 run in this batch; deferred receipts are listed in tasks.md.
```

## Why

- **#2662**: the node-22 stall probe reported `ok` for >30 h of the #2655 freeze. Two independent
  defects: the default suppression rule silences `comment_accounting_unproven` entries on the CURRENT
  frontier, and a zero-submission pass whose candidates were all skipped as `active_duplicate_pipeline`
  is classified idle and resets the streak, although the scheduler itself counts those as in-flight.
- **#2670**: on the strict warm-start lane a stage-less (full-chain) manual retry after a
  forcing-input failure is rewritten into a `forecast` restart, re-staging the same broken forcing
  package. A missing direct-grid `.tsd.forc` member is reported as `DIRECT_GRID_TSD_FORC_TOO_LARGE`.
- **#2682**: a held forcing master (`reserved`, unbound, `submit_result_ambiguous`) whose job is
  confirmed dead has no supported exit; it freezes the source's forward lane.
- **#2660**: rollback quiescence compares a wall-clock `prepared_at` with filesystem `st_mtime`. On a
  coarse-mtime filesystem a file written within one tick after the fence looks older than the fence
  and is skipped.

## What Changes

- `scripts/node22_scheduler_stall_health.py`: a new pass shape for "zero submitted, zero blocked, but
  non-terminal skips present" that does not break the streak; a time gate from the last progress pass
  that turns a sustained blocked/in-flight-held run into a non-`ok` verdict; suppression is bypassed
  for tracker entries whose cycle is on the newest pass's frontier; the receipt carries both.
- `services/orchestrator/scheduler_candidates.py` (+ the evidence marker written where manual-retry
  state evidence is built): a stage-less manual retry carrying a forcing-input failure marker keeps
  its full-chain decision on the strict lane. `workers/shud_runtime/runtime.py`: a missing/unreadable
  limited-checksum object is no longer reported as `*_TOO_LARGE`; the forcing-input matcher recognises
  the resulting code.
- `services/orchestrator/file_orchestration_journal.py` (+ CLI, listing, runbook): an
  operator-verified absence exit for held forcing rows. See design.md "#2682 design" — the design is
  published in the PR description before the code lands.
- `services/orchestrator/file_orchestration_journal.py`: the rollback fence is the `st_mtime_ns` of a
  sidecar file created once at prepare, so both sides of the comparison come from the filesystem clock.

## Non-goals

- #2655 root cause; manual demotion of the old 2026091100 / 2026091212 masters; the scheduler-side
  no-progress tracker criteria.
- Changing the strict upgrader's 2-argument signature, the #2439 pre-forecast boundary, or the
  runtime manifest write order.
- Any node-22 production action. The #2682 scratch-journal drill and the #2662 live probe re-run are
  operator-gated and deferred.
- Real-evidence replay for #2662: the frozen-period pass artifacts (2026-09-25T16:45Z..09-27) and the
  2026-09-23 baseline were deleted by evidence retention (`retention-20260925..29` receipts list them
  in `deleted_paths`; the runner unlinks, there is no archive). The stall fixture is a RECONSTRUCTION
  from the values measured in #2662, not a replay.
