## 1. Fixture and red evidence

- [ ] 1.1 Review expanded fixture and pass strict OpenSpec validation; review service availability is a blocking prerequisite, not an assumed pass.
- [ ] 1.2 Capture deterministic red real HTTP classification→file journal→next scheduler pass feedback for forcing502 and cross-key duplicate execution; no production submit reproduction.

## 2. Issue acceptance

- [ ] 2.1 Preserve forcing ambiguity and complete pre-submit identity through row/event/result/scheduler evidence, including transport and invalid success cases.
- [ ] 2.2 Prevent automatic retry/permanent failure and cross convert_cohort/forcing_cohort or overlapping-member resubmission; explicit rejection and unrelated work remain correct.
- [ ] 2.3 Verify unsuperseded incident adoption against full task mapping/accounting and actual forcing object integrity; prove normal continuation reaches forecast without forcing resubmit.
- [ ] 2.4 Implement explicit authorization, pinned CAS, atomic projection and preserved audit; prove repeat/crash replay/concurrent-writer behavior.
- [ ] 2.5 Refuse missing/conflicting identity, incomplete/extra tasks, invalid objects, unsafe paths and superseded targets with zero authority mutations and no Slurm mutation.
- [ ] 2.6 Retain forecast historical digest/identity/reconcile and #2439 stage/strict-witness contracts.

## 3. Verification and delivery

- [ ] 3.1 Parent runs local Ruff/OpenSpec and node27 red/green focused affected suites against exact commit/import paths; skip implementer validation while editing.
- [ ] 3.2 Complete expanded correctness, evidence/spec and invariant-state review with mechanical fix gate, then green exact-head CI.
- [ ] 3.3 Validate the exact minimal production deployment tree and active Python3.12 compatibility without environment rebuild; no unrelated master rollout.
- [ ] 3.4 In quiescent window deploy; first live operator dry-run on superseded49174 must refuse write-free. Never apply an invalid adoption to manufacture evidence.
- [ ] 3.5 Verify normal later dual-source compute/publication and exact-run readable results; document limits, update runbook, merge/close/archive only when acceptance is complete.

## Evidence Floor

- Regression: real HTTP client receives gateway502 with origin code and file-backed stage/scheduler flow; old path becomes permanent failure or resubmits under changed cohort key, new path retains ambiguity and sbatch call count stays one across passes/restart/overlapping cohort selection. Test successful gateway identity and explicit proven rejection as siblings.
- Recovery: isolated real-journal incident fixture and actual small forcing packages, complete task evidence, explicit operator apply→normal scheduler continuation; assert observable no forcing submit and legal forecast progression. Mutate one identity/artifact, race current revision, introduce a newer execution and repeat successful apply; assert zero partial authority and no duplicate execution. Preserve legacy history.
- CLI: launch both supported entrypoints for dry-run, successful isolated apply and refusal; errors typed, nonzero refusal exit and no production mutation. Pin dangerous roots and bounded read budgets using existing seams.
- Production: source e7bd816 currently operates; user requested #2447 after #2439 restoration. Old49174 completed but later49309 replaced it; live adoption must refuse. Two sources published2026091512 as of23:06Z on2026-09-16; that is historical baseline, not proof newfix works. No DB on22; no uv sync/bare uv run; exact active interpreter only.
- Main tests run node27 isolated checkout, TMPDIR=/home/nwm/tmp and df -h / /home /data/GHDC before pytest. Reuse environment only with matching manifests. Local validation: uv run --no-sync ruff check changed files; openspec validate preserve-forcing-submit-ambiguity --strict --no-interactive. Record exact selected pytest files in implementation handoff after mapping new seams; required families cover stage execution, file journal/reconcile, production scheduler, operator CLI and forcing integrity.
- Reference evidence: #2445 receipt and change evidence, #2447 incident/comment. Do not assert stdout capture race caused this event without a separate deterministic reproducer.

## Risk packs

| Pack | Selection and required proof |
| --- | --- |
| Public API / CLI / script entry | Selected: shared Click/argparse dry-run/apply/refusal runtime smoke, task2.4/3.1. |
| Config / project setup | Selected: no new config/dependencies; exact environment/tree deployment admission3.3. |
| File IO / path safety / overwrite | Selected: real package checks, journal-root/containment/budget refusal2.3/2.5; no direct raw journal edits. |
| Schema / columns / units / field names | Selected: forcing typed identity and persisted ambiguity; historical forecast digest unchanged2.1/2.6. |
| Auth / permissions / secrets | Selected: explicit operator authorization, no display mutation path or secrets in receipts2.4. |
| Concurrency / shared state / ordering | Selected: cross-key member fence, CAS/revision, crash replay and atomic projection2.2/2.4. |
| Resource limits / large input / discovery | Selected: bounded targeted evidence/object reads, fail closed budgets2.5; no historical unbounded scan. |
| Legacy compatibility / examples | Selected: unprovable legacy refusal, auditable reconstruction and superseded49174 refusal2.3/2.5. |
| Error handling / rollback / partial outputs | Selected: ambiguous vs rejection, no partial write, quiescent rollback2.1/2.4/3.3. |
| Release / packaging / dependency compatibility | Selected: actual deployment tree validated, Python3.12 retained3.3. |
| Documentation / migration notes | Selected: recovery command authority and refusal docs, receipts, no blanket migration3.5. |
