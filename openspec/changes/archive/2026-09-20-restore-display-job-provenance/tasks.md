## 1. Fixture and contracts

- [x] 1.1 Keep the expanded fixture, three specs and design coherent; run
      `openspec validate restore-display-job-provenance --strict --no-interactive` before implementation.
- [x] 1.2 Preserve the current node-27 production rollback state for PR #2450; #2121-A and #2346 remain held until this
      issue closes.

## 2. Producer publication on node-22

- [x] 2.1 Implement the bounded source-owned provenance publisher that reads the DB-free journal without write locks,
      mkdir-on-read, or private path export, validates the selected run's manifest, and writes the per-run sidecar plus
      any verified canonical log artifacts.
- [x] 2.2 Wire the publisher into the existing run-tree copyback lifecycle and expose the same publisher through a
      bounded backfill CLI; make the hook side-effect-safe and failure-explicit without changing scheduler
      submit/reconcile semantics.
- [x] 2.3 Cover publisher regressions for identical replay, stale/conflicting evidence, journal redaction, ambiguous
      task logs, missing log metadata, private-path/symlink/traversal rejection, and unchanged copyback ordering.

## 3. Node-27 projection

- [x] 3.1 Add the validated importer for `runs/<run_id>/input/pipeline_jobs.json` that binds source/cycle/run/model/job
      identity, preserves lifecycle fields, inserts missing rows idempotently, refuses conflicting job IDs, and can
      enrich a previously null verified log URI monotonically.
- [x] 3.2 Integrate the importer into the node-27 autopipeline as a lightweight catch-up phase that also covers
      already-ingested runs without re-running register/forcing/parse or touching hydro readiness.
- [x] 3.3 Prove ingest owner/write-role discipline on the real DB: `nhms_ingest_rw` can insert/update the job rows,
      `nhms_display_ro` cannot, and the existing readonly denial matrix still passes.

## 4. Display contract and consumers

- [x] 4.1 Return one coherent top-level `identity` object on all four strict Ops routes, including `job_id` on logs,
      without changing existing data containers, request-id behavior, 404/409/422 errors, or non-strict browsing.
- [x] 4.2 Update OpenAPI/runtime schema, generated frontend types, and any consumers that need the resolved identity;
      keep validator/C4 expectations honest and do not weaken them.

## 5. Verification, deployment, and evidence

- [x] 5.1 Add and run the focused unit/integration suites for publisher, importer, route responses, identity wrapper,
      and the readonly validator paths; include the actual base-red cases that fail on pre-change source.
- [x] 5.2 Rehearse the producer on node-22's active interpreter against the real journal and Slurm gateway in read-only
      fashion; confirm exact parent-array _task-entry_ selection (including envelope/task run_id mismatch) and no state
      mutation. Interpreter: `/scratch/frd_muziyao/NWM/.venv/bin/python` or a checked-in wrapper; `uv run --no-sync`
      only; never `uv sync`.
- [x] 5.3 Deploy the backfill + normal tick path on node-27 and produce the real GFS/IFS read receipts that were
      previously BLOCKED, including the successful jobs/status/stages/logs evidence for the same strict identity. Live
      identities are re-resolved at apply time.
- [x] 5.4 Re-run the formal public C4 path end-to-end on the reviewed SHA and capture PASS; then close #2420 and resume
      the held #2121-A / #2346 joint batch from a clean, current head.

Suggested fixture level: expanded Minimal mergeable slice: one PR covering publisher + importer + four-route identity;
live node-22 rehearsal and node-27 C4 are evidence, not a second code slice.

## Risk packs

- Selected Public API / CLI / script entry: four Ops envelopes, backfill CLI, OpenAPI/types — tasks 2.2, 4.1, 4.2, 5.1.
- Selected Config / project setup: node-22 journal/env roots, published-artifact root, ingest DSN/role — tasks 2.1, 3.2,
  5.2, 5.3.
- Selected File IO / path safety / overwrite: sidecar/log publication, symlink/traversal/redaction, atomic writes —
  tasks 2.1, 2.3, 3.1.
- Selected Schema / columns / units / field names: sidecar schema, `ops.pipeline_job` columns, envelope `identity`,
  `.out/.err` URIs — tasks 2.1, 3.1, 4.1, 4.2.
- Selected Auth / permissions / secrets: `nhms_ingest_rw` write / `nhms_display_ro` deny, no superuser importer, no
  credential export — tasks 3.3, 5.3.
- Selected Concurrency / shared state / ordering: copyback mutex preserved, source-version ordering, identical replay —
  tasks 2.2, 2.3, 3.1.
- Selected Resource limits / large input / discovery: parent-array cache-once, no unlimited task-log scan, no full
  river-fact walk — tasks 2.1, 3.2.
- Selected Legacy compatibility / examples: PostgreSQL-backed writers unchanged, existing `data` shapes, published log
  URI form — tasks 2.2, 4.1, 4.2.
- Selected Error handling / rollback / partial outputs: publisher fail-closed without copyback abort; importer conflict
  rollback; missing logs stay unpublished — tasks 2.2, 2.3, 3.1, 5.1.
- Not selected Release / packaging / dependency compatibility: no new dependency.
- Selected Documentation / migration notes: runbooks/receipts for backfill, rollback, C4; held #2450 resume order —
  tasks 1.2, 5.3, 5.4.

## Verification commands

- Local: `openspec validate restore-display-job-provenance --strict --no-interactive`; `uv run ruff check` on touched
  Python; focused pytest named in 5.1, including a base-red run against pre-change source.
- Node-27: real-DB importer/role tests; `scripts/validate_readonly_db_boundary.py` dual-source; formal public C4 on the
  reviewed SHA.
- Node-22: publication rehearsal with the active 3.12 interpreter only; journal and gateway reads; no scheduler
  mutation, no archived DB, no `uv sync`.

## Verification progress

Reviewed and deployed source: `87236ca540832ab6db6da96267234d00b6c92d61`. Final integration verification on node-27:
**1148 passed, 1 skipped**, including real PostgreSQL importer tests; the preceding provenance/attribution integration
ran **1196 passed**, and the canonical UTC repair ran **1027 passed**. CI run `35483956257` passed all applicable lanes.
Retained base-red evidence covers missing success identity, same-version stderr enrichment, reconciled child Slurm IDs,
and UTC `Z` versus `+00:00`; the validators and C4 were not weakened.

Node-22 used its existing Python 3.12.7 interpreter. Isolated real-journal/gateway rehearsal selected IFS task 23 even
though the parent envelope named a different run. Descriptor-resolved syscall evidence recorded no production-root
mutations in the captured calls. Full-scope rehearsal on `18af6d80f` (publisher unchanged in the final source) published
five jobs and one verified log for each current source. The live backfill used the existing mutex-protected copyback
path for only the two sidecar files; scientific manifests were unchanged.

The ingest role inserted ten source-authoritative rows, replayed all ten unchanged, and passed a rolled-back ten-row
UPDATE privilege probe. The normal autopipe tick imported both already-ingested runs without scientific re-ingest. Both
deployed-source readonly receipts passed nine reads and all 23 write denials across twelve targets. Formal public C4
**freeze, browser, bind, verify all passed** for QHH GFS/IFS `2026-09-19T00:00:00Z`.

Evidence and rollback history:
[deployment receipt](../../../../docs/runbooks/receipts/2026-09-20-issue2420-job-provenance.json). The earlier live UTC
spelling failure was rolled back before repair. Final deployment preserved node-27's intervening `000061`/query-index
changes and node-22's backport ancestry; no environment rebuild or role-default change occurred. Both timers resumed. C4
completion releases the #2420 merge gate; issue closure and the held joint batch follow that merge, not this source
receipt alone.
