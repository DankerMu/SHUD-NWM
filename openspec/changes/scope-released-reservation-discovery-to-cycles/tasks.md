# Tasks

## 1. Red-first oracle (must be written and RED before any production edit)

- [x] 1.1 Regression pin: the discovery listing SHALL survive a record budget
      that the whole-tree replay exceeds but no single cycle does.
      **Oracle shape matters.** The cycle-scoped replay consumes the SAME
      `_RecordBudget` per call (`file_orchestration_journal.py:5770`), so a naive
      `max_records=1` reddens the fixed code too and proves nothing. Build
      **N cycles x M records each with M < max_records < N*M** — e.g. 3 cycles x
      4 records with `max_records=6`: whole-tree consumes 12 (red today),
      per-cycle consumes at most 4 (green after the fix). Assert the released row
      is FOUND, not merely that no exception escapes.
- [x] 1.2 Prove 1.1 is red at base: run it on `origin/master` and record the exact
      failure (`file_journal_record_limit_exceeded`, `field=pipeline_job_records`).
- [x] 1.3 (adapted — design D13) Residue pin: a flat `pipeline-jobs/` row whose job_id does NOT match
      `_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE` but whose CONTENT is a current-contract
      master SHALL still be discovered. Production holds zero such rows today
      (74 unparsable names, all `(row_kind=None, contract_is_current=False)`), so
      only a synthetic row can exercise this path — which is exactly why it needs
      a pin rather than an assumption.
- [x] 1.4 (adapted — design D13) Fall-open pin: a row that yields no cycle scope from name OR content
      SHALL fall open to the full scan, preserving the #1734 D4 contract that an
      underivable key costs the old full scan and never a false "not found".
- [x] 1.5 Unweakened-pin check: the existing single-row listing test
      (`tests/test_file_orchestration_journal.py:14291`) SHALL pass with zero
      edited assertions.

## 2. Implementation (shape per design D10 — the per-cycle-replay shape of D9 was measured and rejected)

- [x] 2.1 Discovery reads the flat `pipeline-jobs/` directory once via
      `_iter_direct_pipeline_job_records` (`file_orchestration_journal.py:5336`),
      which is guarded by `max_files`, NOT by `_RecordBudget`. Filter on the
      record's `payload` using the EXISTING six-clause predicate, byte-identical.
- [x] 2.2 Each surviving candidate gets an authoritative re-read through the
      cycle-scoped path (the same one `--job-id` already uses and which verifies
      on a real production row). The flat scan is a CANDIDATE filter; the
      cycle-scoped read is what the returned row is built from.
- [x] 2.3 (adapted — design D13) Residue: a flat entry whose job_id does not parse still yields a scope
      from row content (`_source_id_from_job` `:9983` / `_cycle_time_from_job`
      `:9991`); only when content also yields no scope does that ONE candidate
      fall open to the unscoped read.
- [x] 2.4 Preserve the return contract: same `_public_scheduler_row` shaping, same
      `job_id` sort, same list type. `cli.py` untouched.
- [x] 2.5 Rewrite the docstring's cost paragraph. The current text defends the
      whole-tree replay on wall-time grounds; what actually fires is a fail-closed
      budget. Name the constraint that binds.

## 3. Correctness argument (must be written down, not assumed)

- [x] 3.1 **Closed-list write inventory.** Enumerate EVERY site that appends a
      `("pipeline_job", row, ...)` payload or otherwise persists a pipeline-job
      row, and show each either pairs a `_write_pipeline_job_direct_unlocked`
      call or provably cannot carry a current-contract master. Known pairs:
      `:2959`/`:3006`, `:3084`/`:3111`, `:3311`/`:3336`, plus
      `_write_pipeline_job_unlocked` which pairs both writes itself at `:7382`;
      `:3931` writes a candidate, routed to `by-cycle/` by design. **Four
      eyeballed pairs are not the proof** — the deliverable is the exhaustive
      list with a verdict per site. If any site can persist a master row without
      a flat write, the flat scan is fail-open and the design changes.
- [x] 3.2 Retention: show no pruning path removes a flat master file while the row
      is still retained. Candidate `unlink` sites are `:6646` (atomic residue) and
      `:6925` (scoped to `_RECONCILE_INVENTORY_DIRECTORY`); confirm these are all.
- [x] 3.3 State the snapshot property explicitly: the flat listing is
      point-in-time on a live journal (observed 4531 -> 4555 -> 4557 across three
      reads minutes apart). Not a regression — the whole-tree replay had it too.
- [x] 3.4 Record why the flat record is current for THIS shape:
      `release_identity_blocked_reservation` writes through
      `_write_pipeline_job_unlocked` (`:3425`), which writes the flat file
      unconditionally (`:7382`) — the releasing call is the one that rewrites it.

## 3b. Confirm-half growth law (cross-review round 2 — design D14)

- [x] 3b.1 Red-first pin: with M candidates over K distinct `(source_id, cycle)`
      scopes, the number of whole-flat-directory listings SHALL NOT grow with M.
      Counted directly by instrumenting `_iter_regular_json_files` on the
      `pipeline-jobs/` directory; red at base with 5 listings for M=2 and 9 for
      M=6, green at a constant 2 (candidate scan + one memo fill).
- [x] 3b.2 Memoize the raw flat listing for the duration of ONE read-only query
      only (`ContextVar`, never an instance cache, never on a mutating path), so
      `_flat_direct_pipeline_job_paths_for_cycle` stays the ONE definition of the
      filename filter.
- [x] 3b.3 Group candidates by `cycle_scope` so each distinct cycle is confirmed
      once. Fail-closed budget behaviour and the single whole-tree fall-open pass
      are unchanged.
- [x] 3b.4 Red-first pin for the GROUPING half specifically (round-2 P2: 3b.1
      counts listings, which the 3b.2 memo alone holds at 2 even with the
      grouping reverted, so it does not discriminate the two halves). Counts
      scoped REPLAYS, which the memo does not deduplicate: C=12 candidates over
      K=3 cycles SHALL cost exactly K replays, and the recorded scope set SHALL
      equal the candidates' own cycles (a count alone would also admit three
      replays of the wrong cycle; a `None` scope would mean the whole-tree
      fall-open, not grouping). Red at 12 with a per-candidate loop that KEEPS
      the memo; the 3b.1 pin stays green under that same revert, which is the
      finding's own proof.

## 4. Local verification

- [x] 4.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_production_scheduler.py`
- [x] 4.2 `uv run ruff check .`
- [x] 4.3 `openspec validate scope-released-reservation-discovery-to-cycles --strict --no-interactive`

## 5. Production receipt (node-22) — Evidence Floor

- [ ] 5.1 Equivalence diff on the real journal: run the OLD whole-tree replay with
      an injected huge budget (`max_records=10**9`, diagnostic script only, never
      production code, read-only) and diff its released-row set against the NEW
      flat-scan result. **Identical sets** proves coverage over the entire retained
      history rather than over four known rows. Prior art for the new side already
      measured: 4 557 flat files read in 1.44 s, FOUND 4, matching the independent
      ground truth from a full `journal/` scan for `identity_mismatch_released`.
- [ ] 5.2 Red/green on the same journal: default budget raises
      `file_journal_record_limit_exceeded` at base; the deployed fix returns the
      same four rows with `budget_errors == 0`.
- [ ] 5.3 CLI end-to-end on node-22 after deploy:
      `nhms-pipeline recover-released-identity-blocked-reservation --journal-root <prod>`
      returns `decision: "listed"` with `wedged_count == 4`, write-free.

## 6. Process (carried from PR #1802's ADR 0003 revisit)

- [ ] 6.1 Persist per-round lens lists to `.workplans/pr-<N>/review/round-<K>-lenses.txt`.
      PR #1802's line is excluded from the lens-rotation sample entirely because
      this was skipped (`loop_log_audit.py:124`); repeating it would be the same
      finding twice.
