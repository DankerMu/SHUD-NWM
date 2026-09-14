## Risk triage

Issue type: release/operational repair. Profile: NHMS existing production-maintenance surface. Fixture: expanded; repair intensity: high; effective review tier: high. Upstream suggested fixture: absent. Minimal slice: one incident recovery, no general maintenance redesign.

Selected packs: Public API / CLI / script entry (actual wrapper semantics); Config / project setup (private budget copies); File IO / path safety / overwrite (no-clobber and protected bytes); Schema / columns / units / field names (existing receipt interpretation, ms/seconds); Auth / permissions / secrets (0600 copies, no credential output); Concurrency / shared state / ordering (timer/lock/DB ordering); Resource limits / large input / discovery (one600GiB chunk, finite walls); Legacy compatibility / examples (frozen OLD unchanged); Error handling / rollback / partial outputs (fail closed and preserved evidence); Documentation / migration notes (explicit retired-recipe exception). Release / packaging / dependency compatibility: not selected, no dependency or runtime release change. Domain PostgreSQL/TimescaleDB and production state-machine: selected, actual catalog and service evidence. Domain numerical/geospatial/SHUD/Slurm/provider/scientific-format/UI packs: not selected, no changes to these surfaces.

## 1. Diagnosis and authorization

- [x] 1.1 Preserve statement/wrapper timeout, catalog, receipt freshness and two-lock inference with limits.
- [x] 1.2 Record explicit temporary paired-budget exception and unexpected retention dry-run/enforce deviation (#2355).
- [x] 1.3 Pass readonly fixture review and strict OpenSpec validation before implementation (RecoveryFixtureReview: pass; strict CLI: valid).

## 2. Bounded recovery

- [x] 2.1 Implement an incident-only private-copy preparation/verification helper; preserve all original files and identity (f53181cd4; real preparation/check and isolated cleanup smoke passed).
- [x] 2.2 Verify candidate, regeneration safety, timer/lock/process boundaries and actual budget/outer-wall agreement before one enforce (dry receipt selects only river107; actual unit10242s, backend78398 started09:02:51Z).
- [ ] 2.3 Obtain clean river107 committed receipt and catalog evidence; clean only owned copies and verify protected bytes.
- [ ] 2.4 Obtain genuine original compression and retention service success with fresh semantic receipts and preserved historical retention receipt; restore captured timer states.

## 3. Admission and evidence

- [ ] 3.1 Pass a new hash-bound prepare at target415 with never-created state and active/pin unchanged; no T0/window/recover/unstage.
- [ ] 3.2 Publish incident conclusion, two-lock guidance, authorization/deviation, reviews and exact verification limits.

## Evidence Floor

- Local: `openspec validate restore-node27-maintenance-admission --strict --no-interactive`; targeted Ruff on incident tools. No backend pytest locally.
- Helper safety: disposable node27 smoke verifies existing destination/wrong active/timer-running rejection before a side effect, then valid private-copy creation and original hashes unchanged; no production DB mutation in this smoke. No new permanent tests for mechanical field copying.
- Live: actual paired preflight assembly output, actual transient RuntimeMaxUSec, distinct compression dry and enforce receipts, precise selection and catalog before/after, backend/lock checks, original-unit fresh receipt and status, protected hash comparison, timer restoration.
- Fresh prepare: existing reviewed child2325 executor and fresh hash-bound input/state; record actual result and never promote old qualification to this pass.
- Every named issue2349 acceptance criterion maps to tasks1.1–3.2; direct historical lock holder observation is unavailable and remains a clearly labeled source/log inference.

## 4. Separately approved scan comparison

- [x] 4.1 Validate isolated TimescaleDB2.10.2 on/off compression preserves exact row fingerprints (200000 rows each, MD5 75d8bfe99d051fbe7d4881092adc03d5 before/after); do not extrapolate small-data timing.
- [x] 4.2 Review the amended fixture and bind private-only PGOPTIONS plus manifest scan mode, leaving original env/DSN/global settings unchanged (amendment review/strict validation pass; bb2db64c helper rejects v1 and semantic scan drift).
- [x] 4.3 Prove the prepared private connection resolves indexscan=off and perform one separately approved comparison with unchanged100-minute ceiling and exact river107 selection (completed with partial/failed_before_mutation after6011.332s; NOT a recovery pass).
- [ ] 4.4 After successful comparison and original compression service success, immediately restore its timer as explicitly directed; on failure retain the pause and do not retry automatically.

The first6000-second attempt failed cleanly before committed mutation; actual owned-copy cleanup passed. Retention service/timer recovered independently with zero drops. Tasks2.3/2.4/3.1 remain incomplete until compression genuinely recovers. A new input bundle exists for tool publication92a5b4350992 with byte-identical executors; its preparation state has not been created or run.

The second comparison also timed out before committed mutation. Its private envs were cleaned and retention scheduling restored; compression scheduling remains paused under the explicit user instruction. There was no implicit retry authorization; the subsequent explicit six-hour approval is recorded below. Logged sort temporary files total182.426GiB; this is not a progress percentage or a prediction of completion time. Compression recovery, timer restoration after repair, and fresh prepare remain incomplete.

## 5. Explicitly approved six-hour window

- [x] 5.1 Review the new authorization and update only the private time budget to21600000/21900/25842, retaining scan-off, bound1 and all memory/authority protections (focused fixture PASS; strict OpenSpec and Ruff PASS; helper5f51312f; node27 smoke rejects old schema/budget, scan drift and unsafe cleanup).
- [ ] 5.2 Recheck full outer-envelope schedule and capacity, private connection setting and exact dry selection; execute at most one newly approved attempt.
- [ ] 5.3 Preserve actual terminal outcome and cleanup; on success complete original-service proof, immediate compression-timer restoration and fresh prepare; on failure restore retention scheduling and do not attempt a fourth run.
