# Tasks

## 0. Evidence Floor

- [x] `uv run pytest -q tests/ -k file_orchestration_journal` green (local)
- [x] `uv run ruff check .` green (local)
- [x] **Equivalence property test**: over a journal populated through the
      production writers across multiple cycles and both sources, for every key
      reachable by each narrowed entrypoint, the cycle-scoped result is
      list-equal to the full-scan result filtered by that key — same rows, same
      merge resolution, same `_db_compatible_pipeline_job_order_key` ordering,
      same blocked-row error shape.
- [x] **Read-path containment test**: with `_read_optional_json` / `_read_jsonl`
      instrumented to record touched paths, a narrowed single-key lookup touches
      only paths under that cycle's `latest/<source>/<cycle>/`,
      `journal/<source>/<cycle>*`, and the direct partition — zero files from
      any other cycle.
- [x] **Fall-open negative pins** (these cannot be reddened by reverting the
      source; they pin the direction the fix moves toward):
      - a key whose source spelling differs in case between the run-id and the
        on-disk directory still resolves to the row;
      - a key that cannot be parsed into `(source, cycle)` returns the
        full-scan answer, not `None`;
      - a row that exists only in a cycle *other* than the one derivable from
        the key is still returned by the entrypoints that must return it (or,
        where it must not be, the exclusion is asserted explicitly).
- [x] **`include_direct=False` parity**: the `_pipeline_job_for_id_unlocked`
      fallback keeps excluding direct records **in its narrowed path** (not only
      in the shared iterator), with a test that would show duplication if the
      flag were dropped.
- [x] **Flat-surface containment**: a narrowed lookup opens no flat
      `pipeline-jobs/*.json` file belonging to another cycle, and DOES open one
      whose name is unparseable.
- [x] **Re-pointed whole-tree probes**: each existing discovery-hardening test
      that used a now-narrowed entrypoint as a whole-tree vehicle still asserts
      its property, through a path that still full-scans — preferably an
      underivable key through the same public entrypoint (fall-open reaches the
      full scan). Per-class justification recorded in the PR's 偏离记录.
- [x] **Concurrency**: any new per-cycle cache is exercised by the existing
      shared-instance concurrency test shape (spec
      `pipeline-job-persistence` "Journal read caches are safe under concurrent
      orchestration threads sharing one repository instance"); single lock
      order preserved, no cache-mutex -> write-mutex nesting.
- [x] **node-22 pre-change oracle** (captured, `.workplans/1734/baseline-node22.md`):
      pass `scheduler_2026082219_f048457a8e0d` at `e9970a1b`, PID 3830783, last
      live sample before exit (elapsed 01:56:46 / CPU 01:22:14):
      `rchar` 44,453,875,940 · `read_bytes` 1,043,906,560 · `syscr` 1,403,447.
      = 380 MB/min and **3.18 GB per candidate** (14 candidates); working set
      566.1 MiB. systemd at exit: `Consumed 1h 22min 53.556s CPU, 1.5G peak`.
- [x] **node-22 post-change re-measurement, same口径**, on a pass that genuinely
      executes backfill, **with the PID pinned for the whole sample** (the timer
      relaunches immediately, so a `pgrep` sampler silently follows the next
      pass — this happened during baseline capture):
      primary criterion `rchar / candidate_count <= 0.32 GB` (>=90% below the
      3.18 GB baseline).
      Secondary criteria, both stated in the **same口径 as the baseline sample
      above** (elapsed 116.77 min): `rchar` rate not above 38 MB/min (10% of the
      baseline's 380 MB/min) and `read_bytes` rate not above **0.89 MB/min**
      (10% of the baseline's `1,043,906,560 B / 116.77 min` = 8.94 MB/min).
      **Correction:** an earlier draft set the `read_bytes` bound at 48.5
      MB/min. That number is `proposal.md`'s raw, already-regressed rate from a
      different ~20.8-minute sample — not a reduction of anything. It would have
      been satisfied by post-change IO running ~5.4x *worse* than the baseline
      sample, on the very metric the proposal calls the real cost. It is retained
      nowhere as a target; if quoted at all it is an absolute non-regression
      ceiling, never evidence of improvement.
      `read_bytes` is page-cache-sensitive and noisier than `rchar`, so the
      primary criterion remains the decider: a `read_bytes` miss with the primary
      met does not void D1 by itself, but SHALL be recorded with its cause rather
      than waved through.
      **The receipt MUST record `syscr` alongside `rchar`.** The D2a
      filename prefilter cuts bytes read, but `_iter_discovered_files`
      still `lstat`s every directory entry *before* the filename filter
      runs, so metadata-RPC count does not fall with it. Recording only
      `rchar` would let the measurement declare victory on bytes while
      `lstat` count keeps growing with retained history. (Residual
      reported, not fixed — routed to #1758.)
- [x] Retention ruling recorded in `design.md` with its working-set bound
      rationale, and a follow-up issue filed for its implementation.

## 1. Evidence: identify the dominant caller before narrowing

The `/proc` measurement cannot attribute read volume to an entrypoint, and
production cannot be instrumented before the change. Discharge is therefore
three-part, all three required:

- [x] (a) Static call-graph ranking — recorded in design.md D1. Explicitly an
      estimate chained across two hops of indirection; it settles the binary
      narrow/leave ruling and nothing more.
- [x] (b) Local call-count instrumentation: wrap `_iter_pipeline_job_records`
      with a counter and drive an existing end-to-end scheduler test, recording
      which entrypoints fire and how often. This converts (a)'s estimate into a
      measured per-entrypoint ranking on a real code path.
- [x] (c) The node-22 post-change measurement is the empirical decider.
      **Pre-declared fallback ruling**: if `rchar` does not drop >=90%, the D1
      "leave" decisions for `_pipeline_job_for_id_unlocked` and
      `query_pipeline_job_by_slurm_id` are void and are revisited **inside this
      change**, not shipped around and deferred.

      **MEASURED 2026-08-23 — PRIMARY CRITERION NOT MET. Receipt:
      `.workplans/1734/receipt-node22-postchange.md`.** Post-change pass
      `scheduler_2026082306_f02713c4c7ec`, PID 4077969, node-22 HEAD `e056c33b`
      (contains `e5d25c80`/PR #1759, verified by `git merge-base --is-ancestor`).
      Shape-identical to the baseline pass (`candidate_count 48 / submitted 14 /
      skipped 34 / blocked 0`), so the denominator 14 is the baseline's own.

      |metric|baseline|post-change|criterion|verdict|
      |---|---|---|---|---|
      |`rchar`/candidate (GB)|3.175|0.911|<= 0.32|**FAIL** (71.3% drop, needed >= 90%)|
      |`rchar` rate (MB/min)|380.7|320.5|<= 38|**FAIL**|
      |`read_bytes` rate (MB/min)|8.94|46.66|<= 0.89|**FAIL**, 5.2x baseline|
      |`syscr` total|1,403,447|768,170|record|-45%|
      |`syscr` rate (/min)|12,019|19,309|record|+61%|

      Elapsed fell 65.9% (116.77 -> 39.78 min) alongside the 71.3% byte drop,
      which is why the *rate* criteria barely move: they normalise by wall time,
      and this change removed work rather than slowing it down. The primary
      criterion is per-candidate total bytes and is the pre-declared decider; it
      fails.

      **THE PRE-DECLARED FALLBACK RULING ABOVE IS STALE AND CANNOT DISCHARGE
      THIS MISS.** It voids "the D1 'leave' decisions for
      `_pipeline_job_for_id_unlocked` and `query_pipeline_job_by_slurm_id`", but
      design.md D1a already REVERSED `_pipeline_job_for_id_unlocked` to **narrow**
      (implemented), and design.md D1 records `query_pipeline_job_by_slurm_id` as
      having **zero production callers** — narrowing it cannot change any
      production byte. Executing the ruling literally would close nothing. The
      ruling was written against the pre-D1a table and was never updated when
      D1a landed.

      **Where the residual actually points, on this receipt's own numbers:**
      `rchar` fell 71.3% while `syscr` fell only 45% and `read_bytes` rose 5.2x.
      That is the exact asymmetry this task block predicted in its `syscr`
      clause — `_iter_discovered_files` `lstat`s every directory entry BEFORE the
      filename prefilter, so metadata cost does not fall with bytes (routed to
      #1758). The residual is therefore attributed to the directory-walk surface
      rather than to either D1 call site, but that attribution is an inference
      from three aggregate counters, NOT a traced measurement, and it is recorded
      as such. Closing the primary criterion requires attributing the remaining
      11.88 GB before any further narrowing is chosen — the same discipline
      Task 1 imposed on the first round.

## 2. Implementation

- [x] Cycle-scoped record iteration replaying the same sources through the same
      merge path (NOT a route to `_direct_pipeline_job_records_for_cycle_cached`
      alone — see design.md "Forbidden implementation").
- [x] Key -> (source, cycle) derivation reusing the existing run-id/path helpers
      and `normalize_source_id`; no fresh parser.
- [x] Wire the derivable entrypoints, **including `_pipeline_job_for_id_unlocked`
      per D1a** (derive via `_CANDIDATE_JOB_ID_RE` / the `job_cycle_` shape,
      fall open otherwise, `include_direct=False` preserved). Leave
      `query_pipeline_job_by_slurm_id` on the full scan.
- [x] Filter the flat `pipeline-jobs/` direct surface by filename per D2a:
      skip only names parsing to a different `(source, cycle)`; read unparseable
      names.
- [x] Fall-open fallback on any derivation failure.

## 3. Spec + docs

- [x] Spec delta under `pipeline-job-persistence`.
- [x] Retention ruling + follow-up issue. (ruling = D8; follow-up = #1757)

## 4. Follow-ups filed

- [x] #1757 — `latest/`/`journal/` disk-side archive口径 (D8's deferred implementation).
- [x] #1758 — `_iter_direct_pipeline_job_records_for_cycle` still whole-scans the
      flat directory and filters by record content; same growth law, a read path
      D2a's clause does not name. Reported, not fixed, because content is
      identity-authoritative there and a filename prefilter would change
      behaviour for a name that contradicts its content.
- [x] #1760 — write-boundary invariant: nothing enforces that a row's `job_id`
      agrees with its own `source_id`/`cycle_time`, yet D2a makes the file NAME
      authoritative for cycle scoping. Verified not producible by any existing
      writer (run ids are content-pinned; `normalize_source_id` is a closed
      allowlist with no `_`; 0 divergent rows in 4,309 production files), and a
      hand-planted divergent row is still recovered via the content-derived
      journal partition. Declared as a residual in the delta rather than closed;
      the fail-closed `job_id` decomposition check is tracked there.
- [x] #1761 — 大小写别名双读残留：`_merge_cycle_source_discovery` 与
      `_cycle_read_source_segments` 的 `source_segment_overrides` 分支仍按字符串
      去重，二者互锁（前者产出的混合拼法 `("IFS","ifs")` 原样喂给后者，后者收不掉）。
      与本 change 已修的 primary 分支同缺陷类，但在冻结的分支范围之外。属**开发环境
      完整性**面：本地 macOS 上预算/containment 类断言仍可能「以错误的理由」变绿。

## 5. Round 2 (2026-08-23): parity fix + fired memo contingency + traced attribution

Opened because the round-1 receipt missed the primary criterion (71.3% vs
`>=90%`) and the pre-declared fallback ruling was found stale. Design: D9, D10,
D11, D12.

**Pre-declared outcome (D12): the primary criterion is expected to MISS again in
this round.** 8.27 GB must go; B is `syscr`-capped at ~2.3 GB and C is estimated
at 1-4 GB, so at most ~6.3 GB is reachable here. The round's deliverable is the
two pre-declared fixes **plus a traced A/B/C split that sizes A**. A receipt
showing the criterion still missed alongside that split is this round succeeding.

### Implementation

- [x] **D9 parity**: `_iter_direct_pipeline_job_records_for_cycle` (`:4857`)
      delegates its flat `pipeline-jobs/` leg to
      `_iter_flat_direct_pipeline_job_records_for_cycle` (`:4801`).
      **Delegate, do not copy** — a third filter definition recreates the very
      parity class this fixes. By-cycle leg untouched (already partitioned).
- [x] **D10 memo**: memoize `_iter_pipeline_job_records_for_cycle` (`:4943`) on
      `(source_id, cycle)`. Invalidation signature scoped to **this cycle's own
      files** — `latest/<segment>/<cycle>/` dir stat is already cycle-scoped;
      `journal/` and flat `pipeline-jobs/` legs stat the **matched file set**,
      never the shared directory. Any leg that cannot be scoped is recorded as a
      stated memo limitation, not hidden behind a directory stat.
- [x] **D11 counter**: always-on per-entrypoint `(tag, calls, bytes)` counter
      over the read primitives, merged into pass evidence. Ships in the repo
      (node-22 pulls from GitHub; no local-patch path). Thread-safe under
      spec `pipeline-job-persistence:550`.

### Evidence floor

- [x] **Discriminating memo test** (D10, required): a write to a **different**
      cycle MUST NOT evict this cycle's memo entry. Without this assertion the
      new memo is indistinguishable from `_direct_jobs_cycle_cache`'s
      correct-but-thrashing pattern. Must be shown to bite: revert the scoping
      to a shared-directory stat and watch it go red.
- [x] **D9 parity test**: a file in flat `pipeline-jobs/` belonging to another
      cycle is not opened by `_iter_direct_pipeline_job_records_for_cycle`.
      Must be shown to bite against the pre-change function.
- [x] **Fail-open preserved**: an unparseable flat file name is still read
      (both readers), pinned on the delegating path.
- [x] **Concurrency unchanged**: existing shared-instance concurrency test
      (spec `pipeline-job-persistence:550`) green; single lock order, no
      cache-mutex -> write-mutex nesting.
- [x] **Local suite**: `uv run pytest` over the journal/scheduler suites the
      round-1 change already covered, plus the new tests.
- [ ] **node-22 receipt, same口径 as round 1 verbatim**: denominator 14,
      decimal GB/MB, `syscr` recorded alongside `rchar`, PID pinned for the whole
      sample on a pass that genuinely submits (a plan-only pass performs no
      writes, triggers no invalidation, and would understate B).
      Report `rchar`/candidate, `rchar` total, `syscr`, `read_bytes`, elapsed —
      **and the D11 tag split**, which is the round's actual deliverable.
      Record `wchar` this time: round 1 could not attribute the `read_bytes`
      rise because the baseline sample never recorded it.

### Round 2 implementation record (2026-08-23)

- D9 landed as a shared PATH helper,
  `FileOrchestrationJournalRepository._flat_direct_pipeline_job_paths_for_cycle`
  — one filter definition, used by both flat readers. Record-level delegation
  was rejected because `_iter_direct_pipeline_job_records_for_cycle` merges its
  flat and by-cycle legs in ONE sort, so yielding records from the other reader
  would have changed its yield order.
- D9's prefilter now normalises the source token; the pre-existing filter
  compared raw strings, which would have skipped every file of a source passed
  as `ifs` rather than `IFS`. Pinned by
  `test_direct_cycle_records_prefilter_normalises_the_source_case`.
- D10's memo key carries `include_direct` in addition to `(source_id, cycle)`,
  plus the resolved source segments. The flag is not decorative —
  `_pipeline_job_for_id_unlocked` replays with it false while every other
  narrowed entrypoint replays with it true — so a key without it would serve
  one variant's rows to the other.
- #1758 is superseded by D9 and should be closed against this round rather
  than left open: its stated reason for "reported, not fixed" (content is
  identity-authoritative there) is the ruling D9 reverses.

### Round 2 verification actually run (local)

- Bite proof 1 (memo): red against pre-change source on `assert reads == []`
  ("a warm memo must not re-read any file"); red again with the invalidation
  scoping reverted to `_stat_signature(self.root / "pipeline-jobs")` on
  `"a write to another cycle must not evict this cycle's memo entry"`; green
  after restore. `__pycache__` cleared and mtime perturbed around the mutation.
- Bite proof 2 (D9 parity): red against pre-change source on
  `assert "job_cycle_gfs_2026062812_convert.json" not in opened`.
- `uv run pytest` over the journal, scheduler, chain, reconcile, retention and
  timing suites — see the PR body for the run.
- `uv run ruff check .` and `openspec validate ... --strict` clean.

### Round 2 pins that MOVED, and why

- `test_file_orchestration_journal_scoped_direct_snapshot_discovery_fails_closed_on_malformed_present_evidence`
  asserted that a malformed flat file naming IFS/2026062812 fails a
  GFS/2026062800 `has_active_pipeline` closed. That is precisely the parity
  defect D9 removes, and the test's own round-1 comment said so ("the scoped
  reader above still fails closed on it, unchanged"). It now asserts the
  foreign-cycle file no longer blocks, AND that a malformed file naming THIS
  cycle still does.
- `test_narrowed_journal_lookup_touches_no_foreign_cycle_file` asserted every
  lookup "must still read something"; the memo makes a warm lookup read
  nothing. Containment is a cold-path property — a memo can only shrink the
  opened set — so the test now clears the memo before each measured lookup.

### Round-1 cross-review fixes (5 verified findings, all CONFIRMED by an independent verifier)

Spec/design corrections are already applied by the orchestrator (D11/D13 entrypoint
claim, D13 "no unscoped leg", I7 matrix row, and three new spec scenarios). The
items below are the code and test work.

- [x] **P1 — the counter's concurrency oracle is tautological.**
      `tests/test_file_orchestration_journal.py:13383-13385` asserts
      `totals == sum(tags)`, but `journal_read_attribution()` (`:275-282`) builds
      `totals` from the same `rows` it returns as `tags`, so the equality is an
      identity for any counter content. **Measured**: with the counter replaced by
      a non-atomic read-modify-write, `TRUE_CALLS=40 COUNTED=37 LOST=3` and all
      three assertions still passed. Replace with an assertion against an
      independently known expected count. New spec scenario: "The counter is
      proven accurate, not merely self-consistent". Must be shown to bite by the
      same racy-counter substitution.
- [x] **P2 — attribution does not cover the read surface.** Measured on a real
      308-test fixture: **80.6% of bytes carried no entrypoint**. Only six
      wrappers existed (`:1202, 1212, 1255, 1310, 1324, 1337`);
      `query_inflight_jobs`, `query_reserved_unbound_jobs`,
      `query_rollback_unsettled_jobs`, `get_pipeline_job`, the three cycle-status
      predicates (`:721`, `:729`, `:764`) and the write-path methods that read
      before writing were all untagged. Spec `:180` requires **every** read to be
      attributed. Prefer a boundary mechanism over enumerating methods — a list
      is what drifted in the first place. Acceptance is measured, not argued:
      re-run the fixture probe and report the residual share.
- [x] **P3 — `direct_flat_scan` conflates two legs.** `:4527` wraps the whole of
      `_iter_direct_pipeline_job_records_for_cycle`, which merges the flat leg and
      the already-partitioned `pipeline-jobs/by-cycle/` leg into one sorted list
      (`:5077-5091`). **Measured**: by-cycle contributed 33.5% of the lane's bytes
      in a scratch fixture; on node-22's tree sizes (26 MB by-cycle vs 13 MB flat)
      it would be roughly two thirds. Split into `direct_flat_scan` and
      `direct_by_cycle_scan`. Record D13's counter-argument with the fix: the tag
      sits on the cache-**miss** path, so by-cycle re-reads on thrash-induced
      misses are arguably part of B's real cost — the defect is that D11 grades
      this lane against flat-sized expectations.
- [x] **P3 — the discriminating memo test does not discriminate.**
      `:13066-13122` uses only parseable job ids, so it cannot see either
      fall-open arm. Add an **unparseable-name** arm (real legacy shape
      `cycle_gfs_..._retry_active`, no `job_` prefix) and a **source-unnormalisable**
      arm. No production code change: broadening invalidation is semantically
      required here (see D13).

      **Correction to this item's own wording, recorded rather than quietly
      dropped.** It first said "Both must fail against today's code, then pass"
      while simultaneously forbidding a production change and requiring the
      tests to pin *current* behaviour. Those are contradictory: a behaviour pin
      cannot go red against the source it pins. The fix pass flagged the conflict
      instead of silently choosing, which is the correct handling. Resolved as
      **red against the tempting wrong fix** — narrow the prefilter so fall-open
      files are skipped (exactly what D13 forbids) and both arms go red:
      `assert 'cycle_gfs_2026062800_retry_active' in {...}` for arm 1 and
      `assert set() > {'gfs_2026062800'}` for arm 2 — then restore and both pass.
      That is a stronger receipt than the original wording asked for: it proves
      the pins guard the specific wrong turn a future reader is most likely to
      take.
- [x] **P3 — the new memo has no concurrent-stress coverage.** The named vehicle
      for I7 drives only `_cycle_rows` and `_read_bytes_limited_cached`. The
      8-thread counter test does incidentally populate `_cycle_job_records_cache`
      (12 entries measured), but capacity is 512 so the eviction branch
      (`:5319-5320`) never runs, and it has no writer thread. Minimal closure:
      squeeze `MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES` in a memo-driving
      concurrent test that also runs a writer.
- [x] Re-run the CI-selected set (`scripts/select_ci_tests.py --base master`) as
      the CI substitute. Pre-fix baseline at `f329eab4`: **4355 passed, 1 skipped,
      exit 0**.

**Round-1 fix pass evidence (`b7aff05d`).**

|item|receipt|
|---|---|
|P1 oracle|bite proof RED `counter recorded 8 reads, threads performed 40: per-thread [5,5,5,5,5,5,5,5]` -> restored GREEN|
|P2 attribution|no-entrypoint bytes **80.8% -> 0.01%** (114 B), no-lane **78% -> 0.14%**, same 308-test command as the finding's receipt|
|P3 lane split|`direct_flat_scan` / `direct_by_cycle_scan` both carry bytes in a relocation test|
|P3 fall-open pins|two arms, each RED against the tempting wrong fix, then GREEN|
|P3 memo concurrency|cap squeezed to 2, eviction branch instrumented as executing **94 times**|
|suites|journal+scheduler+gateway **2896 passed, 1 skipped**; journal pair **474 passed, 1 skipped** (was 470 -> exactly +4 new tests, zero deletions, orchestrator-verified)|
|CI substitute|**4359 passed, 1 skipped**, exit 0 (baseline 4355 + exactly the 4 new tests)|
|ruff|clean|

Orchestrator-side follow-ups from the fix pass, applied in design.md: **D11a**
(the lane set grew past A/B/C; adjudicated accept, and the receipt vocabulary is
now split into candidate lanes vs baseline lanes) and **D11b** (entrypoint
attribution moved to the class boundary, outermost-wins; round-2 and round-3
receipts are not tag-comparable). The 114 B / ~2.4 KB residuals are recorded
there as known limits rather than rounded away.
