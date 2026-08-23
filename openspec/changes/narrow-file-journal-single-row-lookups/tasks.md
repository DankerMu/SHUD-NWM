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
