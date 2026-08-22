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
- [ ] **node-22 post-change re-measurement, same口径**, on a pass that genuinely
      executes backfill, **with the PID pinned for the whole sample** (the timer
      relaunches immediately, so a `pgrep` sampler silently follows the next
      pass — this happened during baseline capture):
      primary criterion `rchar / candidate_count <= 0.32 GB` (>=90% below the
      3.18 GB baseline); secondary `read_bytes` rate not above 48.5 MB/min and
      `rchar` rate not above 38 MB/min.
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
- [ ] (c) The node-22 post-change measurement is the empirical decider.
      **Pre-declared fallback ruling**: if `rchar` does not drop >=90%, the D1
      "leave" decisions for `_pipeline_job_for_id_unlocked` and
      `query_pipeline_job_by_slurm_id` are void and are revisited **inside this
      change**, not shipped around and deferred.

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
