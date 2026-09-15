# Design — journal-root seam adoption and the full-tree budget contract

## Risk triage

- **Fixture level: high.** Two issues in one PR; the change touches a
  write-lane lock derivation, the vocabulary of the row that gates duplicate
  submission, and 8+ pinned tests. Round-1 seats: 4 —
  `correctness`, `invariant-state`, `test-evidence+spec-compliance`,
  `security-perf+integration`.
- **Selected risk packs**: `invariant-state` (the blocked row is a state-machine
  participant: present / non-terminal / not reusable), `security-perf`
  (path authority, symlink following, lock placement, no path or traceback
  leakage on stderr), `integration` (two CLI entrypoint families per lane plus
  two operator scripts), `test-evidence` (live node-22 measurement is the
  premise of the whole #1953 half), `spec-compliance` (both modified
  requirements are load-bearing contracts with existing scenarios).
- **Not selected**: `data-migration` (no schema, no durable payload shape
  change: the blocked row is synthesised per call and never written),
  `concurrency` beyond the lock-path question (no new locking, no new threads).

## Seams under test

- `services/orchestrator/journal_root_authority.py:56`
  `verify_journal_root_authority(root, *, setting)` — consumed as-is, not
  renegotiated. It expands `~`, refuses blank/relative/unexpandable, and walks
  every component with `O_NOFOLLOW`; it returns the tilde-expanded, **unresolved**
  path. It refuses a **missing** root (`FileNotFoundError` is an `OSError`).
- `packages/common/safe_fs.py:43` `verify_directory_no_follow` (verify only) vs
  `:73` `ensure_directory_no_follow` (creates missing components with
  `os.mkdir(..., dir_fd=...)`).
- `services/orchestrator/file_orchestration_journal.py:1024` `_RecordBudget`
  (constructed at `:2074` `rollback_scope_records`, `:6662` whole-tree, `:6906`
  cycle-scoped inside `_replay_pipeline_job_records_for_cycle` `:6867`),
  `:6660` `_replay_all_pipeline_job_records`, `:13046` `_blocked_query_job`.

## Decisions

**D1 — one seam, seven lanes, verified at the lane's first statement.**
All seven sites call `verify_journal_root_authority(root, setting="--journal-root")`
and use the returned path for every subsequent filesystem decision the lane
makes. Verification happens at the **entry** of each lane, not immediately
before the repository constructor: the three rollback lanes (`prepare_file_journal_rollback:67`,
`launch_file_journal_rollback_writer:210`, `complete_file_journal_rollforward:986`)
take `_rollback_execution_lock` at `:87`, `:231` and `:997` — *before* the
repository is built at `:96`, `:202` and `:1006` — so verifying at the
constructor would leave the lock itself on an unverified root.
`launch_file_journal_rollback_writer` additionally builds
`ProductionSchedulerConfig(scheduler_journal_root=...)` at `:224` from the raw
value; that too takes the verified root. Both operator scripts are
included: excluding `node22_repair_placeholder_hydro_uris.py` (which the issue
permits) would buy nothing and cost a recorded deviation. Note that this script
also globs the raw root at `:57` even without `--apply`; that read moves behind
the verified root too, otherwise the dry-run half still reads the wrong tree.

**D2 — the create-capable lane creates through the no-follow creator, then verifies.**
`import_historical_scheduler_state` is used against a not-yet-existing root
today (`tests/test_file_orchestration_migration.py:220` passes `tmp_path/"journal"`),
and the plain seam refuses a missing root. Therefore that lane alone: refuse
blank/relative/unexpandable first (seam semantics), then
`ensure_directory_no_follow` (which refuses a symlinked component while creating
missing ones), then verify. Implementation shape: attempt the seam; only when it
refuses with `error_type == "FileNotFoundError"` may the lane create and re-verify.
Any other refusal shape stays a refusal. The six remaining lanes never create.

**D3 — the rollback execution lock is derived, never resolved.**
`_rollback_execution_lock` takes the already-verified root (tilde-expanded,
unresolved) and derives `lock_path = root / ROLLBACK_EXECUTION_LOCK_NAME`. The
`.expanduser().resolve()` call is removed: `resolve()` follows symlinks, which
both defeats the subsequent no-follow check and splits the lock's tree from the
repository's tree. Keeping `.resolve()` while only verifying at the constructor
would still leave a blank root creating `.reconcile-inventory-rollback-execution.lock`
in the working directory, because `Path("") / NAME` resolves under `cwd` — which
is why D1 verifies at lane entry. The existing `ensure_directory_no_follow(root)`
call is kept as a cheap defence in depth on an already-verified path (no lane
that takes this lock is create-capable; the create-capable lane is the import
lane of D2, which takes no lock).

**D4 — widened `except` arms, because a traceback is not an improvement.**
`OrchestratorError` and `FileOrchestrationJournalError` are siblings under
`RuntimeError`, so the recovery lane (`operator_released_reservation_recovery.py:250,284`)
and the three rollback commands (`cli.py:600,637,666` click; `:894,911,926`
argparse) add `OrchestratorError` to their arms. The two scripts have no
wrapping handler and get a typed non-zero return inside `main()`.

**D4a — `migrate-scheduler-state` converges instead of staying the exception
(revised after round 1).** D4 originally left it alone, on the ground that its
existing `(RuntimeError, ValueError)` arm already catches the refusal
(`OrchestratorError` is a `RuntimeError`). Review showed what that costs: the
arm prints `str(error)`, which is the message *alone*, so the one thing an
operator greps for — the `FILE_JOURNAL_INVALID_ROOT:` prefix — never appears,
while this change's own spec delta promises the typed line on all four
migration commands. Three surfaces then disagreed: spec said typed line, code
said message-only, runbook documented the divergence as deliberate. The cheapest
honest fix is to make the code match the spec: the same `except OrchestratorError`
arm on both entrypoints, the runbook's exception paragraph deleted, and a
db-free test (fake exporter forwarding into the real
`import_historical_scheduler_state`) pinning the typed line on both — which also
closes the "documented exception nobody pins" gap review found.

**D5 — a new census code, not a reused one.**
`CENSUS_OUTPUT_UNEXPANDABLE` is new. `CENSUS_OUTPUT_UNWRITABLE` is documented
(`journal_scope_census.py:98-101`) as a failure *after* the receipt is emitted;
reusing it would make exit 1 ambiguous about whether stdout already carries a
receipt.

**D6 — the budget error names its lane; the budget itself is untouched.**
`_RecordBudget` gains a `lane` field that rides in `evidence`. The reason token
`file_journal_record_limit_exceeded` and the field `pipeline_job_records` are
unchanged, so runbook strings and pinned tests keep matching, and
`_blocked_query_job` surfaces the lane for free through the `_evidence_safe`
path it already pipes. `MAX_FILE_JOURNAL_RECORDS` stays 100,000: #1810's
docstring (`:1913-1915`) records that raising it only moves the cliff, and the
measurement below shows the cliff is now inside the production tree — a larger
default would have to be re-raised at the next tree growth.

**D7 — the blocked row changes its status vocabulary and nothing else.**
The row must stay non-`None` and non-terminal: it is load-bearing.
`_pipeline_job_conflicts_unlocked` (`:9336`) keys on non-`None` and gates
`reserve_pipeline_job` / `_write_pipeline_job_unlocked`; `_active_orchestration_conflicts`
(`chain_runtime_utils.py:103,123`) keys on `status not in TERMINAL_JOB_STATUSES`.
Returning `None`/`[]` would re-enable duplicate reservation and duplicate cycle
scheduling. Verified safe for the literal change: both `TERMINAL_JOB_STATUSES`
definitions (`chain_runtime_utils.py:32`, `chain_types.py:15`) are identical and
contain no blocked token; `_file_auto_retry_job_can_be_reused` (`:12516`) is an
allowlist of `{"pending","submission_failed"}`; `_manual_retry_source_for_run`
(`:11762`) filters by `job_id`, not status; `reservation_is_active` has zero
callers and `ReservationResult.already_inflight` keys on `created=False`.

The API and frontend are safe by **reachability**, not by permissiveness:
`apps/api/routes/pipeline.py:49` `PIPELINE_JOB_STATUS_VALUES` *is* a closed
status enum published into the OpenAPI document
(`apps/api/openapi_patching.py:1484,1531`), with a second allowlist
`_ACTIVE_JOB_STATUSES` (`pipeline.py:64`) gating `cancel_run` (`:598`) and
`_stage_display_status` (`:2248`); and
`apps/frontend/src/components/monitoring/JobsTable.tsx:41` `activeStatuses`
gates the cancel button at `:336` (the purely cosmetic union is
`apps/frontend/src/lib/constants.ts:33`). The blocked row never reaches either:
the API's production wiring reads the DB store, and the file-lane retry service
override exists only in tests. The new literal therefore must never be written
into a durable row or an API payload — it is synthesised per call — and it stays
outside `schemas/pipeline_job.schema.json`'s closed enum. `job_id` defaults
are unchanged (`"file_journal_read_blocked"` on the list lanes, the real id on
`get_pipeline_job`), because the `:11762` filter and
`tests/test_retry.py::test_retry_api_file_lane_blocked_job_row_keeps_503` depend
on exactly that.

**D7a — the other three blocked sentinels keep `"running"`, deliberately.**
Four surfaces synthesise a blocked row saying `"running"`. Only
`_blocked_query_job` (`:13046`) changes. `active_slurm_jobs`' inline sentinel
(`:1497-1500`), `_file_journal_blocked_candidate_state` (`:12799`) and
`_blocked_stage_status` (`:12844`) are untouched, and the spec delta is scoped
to the five query entrypoints so it does not bind them. The sharpest reason is
`_file_journal_blocked_candidate_state`: its `pipeline_status` feeds the
**allowlist** `ACTIVE_PIPELINE_STATUSES` (`scheduler_state_types.py:28`,
consumed at `scheduler_state_decision.py:137,194` and
`scheduler_state_rows.py:829,857`), so a new literal there would flip the
db-free scheduler from fail-closed to fail-open. Their tests (including the
whole-dict assertion at `tests/test_file_orchestration_journal.py:5225-5232`)
must stay unchanged.

**D7b — reach point 3 raises, and that is the correct shape.**
`query_released_identity_blocked_jobs`' `unscoped` branch
(`file_orchestration_journal.py:1971-1976`) wraps no handler, so a budget
refusal propagates as a typed `FileOrchestrationJournalError` to its only
consumer, `operator_released_reservation_recovery.py:132` — where D4's widened
arms turn it into a typed single line. No synthetic row is produced, so no
vocabulary change applies to it. #1953's three reach points are therefore
adjudicated as: `:7001`-style fall-open → refuses, converted to a blocked row;
`query_pipeline_job_by_slurm_id` → refuses, converted to a blocked row, no
production caller; `unscoped` → refuses by raising, handled at the CLI.

**D8 — `query_pipeline_job_by_slurm_id` is kept.**
It has zero production callers on both lanes (Protocol `chain.py:467`, DB
`chain_repository.py:836`, file `:1877`, parity lists `chain_compat_static.py:278,308`).
`openspec/changes/archive/2026-08-23-narrow-file-journal-single-row-lookups/design.md:19`
already adjudicated this as **leave**. Deleting it across the Protocol, the DB
lane and both parity lists re-litigates an archived decision for a p3 issue, so
the answer is: keep, let it inherit the lane-tagged refusal, and add a static
test pinning "no production caller" so the fact cannot rot.

## Must-preserve behaviour

1. A blocked read never returns `None` or an empty list from the five query
   lanes, and never a terminal status.
2. Reason token, field and `file_journal.status == "blocked"` marker unchanged;
   only `evidence.lane` is added and the `status` literal changes.
3. `verify_journal_root_authority`'s returned path stays unresolved, so a
   repository still reads the exact configured location.
4. `migrate-scheduler-state` behaviour, the db-free preflight lane and the
   retention/restore authority are untouched.
5. Census `--max-records` remains the escape valve, and the existing
   `CENSUS_OUTPUT_*` codes keep their meanings.

## Live measurement (node-22, read-only, 2026-09-14)

`/scratch/frd_muziyao/NWM/.venv/bin/python` (no `uv run`, per CLAUDE.md's
node-22 exception) against
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`:

| surface | files | raw consumes |
|---|---|---|
| `latest/**` | 7,898 | 54,258 |
| `journal/**.jsonl` | 325 | 94,123 |
| flat direct | — | 5,328 |

Raw total 148,381 (`include_direct=False`) / 153,709 (`True`) against the
100,000 default; unique jobs 17,025 either way; an unbudgeted replay takes 133 s.
(#1810's "raising it only moves the cliff" is at `:1913-1915`.)
Default-budget probes: both full-tree lanes raise
`file_journal_record_limit_exceeded: pipeline_job_records` after ~92 s;
`query_pipeline_job_by_slurm_id` and `query_candidate_state` with an underivable
key each return the synthetic `status: "running"` row after ~91 s with empty
evidence. `services/orchestrator/file_orchestration_journal.py` is byte-identical
between node-22's head `7b38bcb8` and `origin/master`, so the receipt speaks for
master.

## Recorded, not fixed (out of scope findings)

- `_manual_retry_source_for_run` (`:11762`) drops the sentinel by `job_id` and
  then proceeds as if the run had no jobs. Filed as **#2385**, whose read-only
  probe corrected the shape twice: the outcome is `RetryNotFoundError` →
  HTTP 404 `RETRY_NOT_FOUND` with zero writes, i.e. a **misclassified refusal**
  ("no retryable job" standing in for "the journal could not be read"), not a
  fail-open write; and it is unreachable from #1953's full-tree lane, because
  both call sites derive the cycle from the run id first, so only the
  cycle-scoped replay's own budget (or any other cycle-scoped read fault) can
  produce it. It becomes a genuine fail-open only if someone later adds a
  "no job found, create one" branch. Pre-existing, not one of #1953's three
  reach points.
- #1953's "无兄弟副本" premise is wrong: three sibling blocked sentinels exist
  (`:1497-1500`, `:12799`, `:12844`). They are deliberately left alone — see
  D7a, which also records why `_file_journal_blocked_candidate_state` must not
  be "cleaned up" for consistency later.
- Invariant Matrix row 4 of the archived journal-root change detects seam
  adoption by grepping `verify_directory_no_follow` + `FILE_JOURNAL_INVALID_ROOT`,
  which cannot see the second journal-root authority in
  `scheduler_journal_retention.py:120-136` / `scheduler_journal_restore.py:93`
  (`ensure_` + `RetentionFailure("journal_root_*")`). The archived change is not
  edited; the updated detection is recorded here — grep `verify_directory_no_follow`,
  `ensure_directory_no_follow` **and** `RetentionFailure("journal_root_` — and
  convergence of the two authorities is a separate adjudication, filed as
  **#2384**. One refinement from that filing: the literal
  `RetentionFailure("journal_root_` leg hits only the two non-f-string raises
  (`retention.py:190,193`), because the symlink/unsafe reasons are built as
  `RetentionFailure(f"{field}_symlink")`; the sharper discriminator for the
  second authority is `_safe_existing_directory(` together with
  `field="journal_root"`. The triple above still surfaces both files (through
  its `ensure_directory_no_follow` leg), so the recorded means is sound, just
  blunter than the pair now named here.

  **Re-censused (task 1.12), post-change.** `verify_directory_no_follow` inside
  `services/orchestrator/` now appears at exactly one journal-root site,
  `journal_root_authority.py:103` (the seam itself); the other hits
  (`source_cycle_raw_manifest.py:118,302,373`,
  `chain_forecast_execution.py:1151`) verify object-store and workspace roots,
  not journal roots. The seam's own consumers are now ten:
  `scheduler_core.py:67`, `operator_reserved_demotion.py:74`,
  `journal_scope_census.py:488,610`,
  `operator_released_reservation_recovery.py:132`,
  `file_orchestration_migration.py:91,237,1027` plus the create-capable
  `_verified_or_created_journal_root` (`:1286,1293`), and the two scripts
  (`scripts/node22_manual_retry_failed_runs.py:91`,
  `scripts/ops/node22_repair_placeholder_hydro_uris.py:67`). `FILE_JOURNAL_INVALID_ROOT`
  still has exactly one raise site (`journal_root_authority.py:87`).
  The second authority is unchanged and still invisible to the old grep pair:
  `scheduler_journal_retention.py:133` (`_safe_existing_directory`, reached for
  `journal_root` at `:191`) and `scheduler_journal_restore.py:93`, both reporting
  `RetentionFailure("journal_root_*")` (`retention.py:190,193`). So the detection
  means above is confirmed as the one that sees both authorities. Convergence is
  filed as a follow-up, not done here (#1955 acceptance item 6).
- A full-tree replay on an underivable `job_id` burns ~91 s inside
  `_write_lock` before refusing. Latent (D15b measured 0 live full-tree calls);
  recorded as a known limit.

## Non-goals

`MAX_FILE_JOURNAL_RECORDS`; `safe_fs` semantics; the db-free preflight;
retention/restore convergence; census read path, surface adjudication or record
budget; deleting `query_pipeline_job_by_slurm_id`; any node-27 or DB work.
