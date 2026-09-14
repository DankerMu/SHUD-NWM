## Risk triage

Issue type: release/operational repair. Profile: NHMS existing production-maintenance surface. Fixture: expanded; repair intensity: high; effective review tier: high. Upstream suggested fixture: absent. Minimal slice: one incident recovery, no general maintenance redesign.

Selected packs: Public API / CLI / script entry (actual wrapper semantics); Config / project setup (private budget copies); File IO / path safety / overwrite (no-clobber and protected bytes); Schema / columns / units / field names (existing receipt interpretation, ms/seconds); Auth / permissions / secrets (0600 copies, no credential output); Concurrency / shared state / ordering (timer/lock/DB ordering); Resource limits / large input / discovery (one600GiB chunk, finite walls); Legacy compatibility / examples (frozen OLD unchanged); Error handling / rollback / partial outputs (fail closed and preserved evidence); Documentation / migration notes (explicit retired-recipe exception). Release / packaging / dependency compatibility: not selected, no dependency or runtime release change. Domain PostgreSQL/TimescaleDB and production state-machine: selected, actual catalog and service evidence. Domain numerical/geospatial/SHUD/Slurm/provider/scientific-format/UI packs: not selected, no changes to these surfaces.

## 1. Diagnosis and authorization

- [x] 1.1 Preserve statement/wrapper timeout, catalog, receipt freshness and two-lock inference with limits.
- [x] 1.2 Record explicit temporary paired-budget exception and unexpected retention dry-run/enforce deviation (#2355).
- [x] 1.3 Pass readonly fixture review and strict OpenSpec validation before implementation (RecoveryFixtureReview: pass; strict CLI: valid).

## 2. Bounded recovery

- [ ] 2.1 Implement an incident-only private-copy preparation/verification helper; preserve all original files and identity.
- [ ] 2.2 Verify candidate, regeneration safety, timer/lock/process boundaries and actual budget/outer-wall agreement before one enforce.
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
