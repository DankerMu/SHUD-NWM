# Tasks

## 0. Evidence Floor

- [ ] `uv run pytest -q tests/ -k file_orchestration_journal` green (local)
- [ ] `uv run ruff check .` green (local)
- [ ] **Equivalence property test**: over a journal populated through the
      production writers across multiple cycles and both sources, for every key
      reachable by each narrowed entrypoint, the cycle-scoped result is
      list-equal to the full-scan result filtered by that key — same rows, same
      merge resolution, same `_db_compatible_pipeline_job_order_key` ordering,
      same blocked-row error shape.
- [ ] **Read-path containment test**: with `_read_optional_json` / `_read_jsonl`
      instrumented to record touched paths, a narrowed single-key lookup touches
      only paths under that cycle's `latest/<source>/<cycle>/`,
      `journal/<source>/<cycle>*`, and the direct partition — zero files from
      any other cycle.
- [ ] **Fall-open negative pins** (these cannot be reddened by reverting the
      source; they pin the direction the fix moves toward):
      - a key whose source spelling differs in case between the run-id and the
        on-disk directory still resolves to the row;
      - a key that cannot be parsed into `(source, cycle)` returns the
        full-scan answer, not `None`;
      - a row that exists only in a cycle *other* than the one derivable from
        the key is still returned by the entrypoints that must return it (or,
        where it must not be, the exclusion is asserted explicitly).
- [ ] **`include_direct=False` parity**: the `_pipeline_job_for_id_unlocked`
      fallback keeps excluding direct records in the narrowed path, with a test
      that would show duplication if the flag were dropped.
- [ ] **Concurrency**: any new per-cycle cache is exercised by the existing
      shared-instance concurrency test shape (spec
      `pipeline-job-persistence` "Journal read caches are safe under concurrent
      orchestration threads sharing one repository instance"); single lock
      order preserved, no cache-mutex -> write-mutex nesting.
- [ ] **node-22 pre-change oracle** (already captured, `.workplans/1734/baseline-node22.md`):
      pass `scheduler_2026082219_f048457a8e0d` at `e9970a1b`, `rchar`
      539.5 MB/min, `read_bytes` 48.5 MB/min, working set 566.1 MiB.
- [ ] **node-22 post-change re-measurement, same口径**, on a pass that genuinely
      executes backfill: `rchar` <= 54.0 MB/min (>=90% reduction) and
      `read_bytes` not above 48.5 MB/min.
- [ ] Retention ruling recorded in `design.md` with its working-set bound
      rationale, and a follow-up issue filed for its implementation.

## 1. Evidence: identify the dominant caller before narrowing

- [ ] Establish, with static call-graph evidence, which entrypoint(s) dominate
      the read volume (the `/proc` measurement cannot attribute it). If the
      dominant caller turns out to be `query_pipeline_job_by_slurm_id`, the
      "leave it on the full scan" decision is void and must be revisited before
      implementation — not after the node-22 receipt fails the >=90% criterion.

## 2. Implementation

- [ ] Cycle-scoped record iteration replaying the same sources through the same
      merge path (NOT a route to `_direct_pipeline_job_records_for_cycle_cached`
      alone — see design.md "Forbidden implementation").
- [ ] Key -> (source, cycle) derivation reusing the existing run-id/path helpers
      and `normalize_source_id`; no fresh parser.
- [ ] Wire the derivable entrypoints; leave `query_pipeline_job_by_slurm_id`
      on the full scan.
- [ ] Fall-open fallback on any derivation failure.

## 3. Spec + docs

- [ ] Spec delta under `pipeline-job-persistence`.
- [ ] Retention ruling + follow-up issue.
