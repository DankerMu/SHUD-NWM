# Tasks: align-multibasin-oom-non-transient

Spec-only change (issue #1323). Fixture level: none (docs/spec prose,
no runtime behavior; OpenSpec change is the deliverable itself).

- [x] 1. MODIFIED delta for `Resumable downstream failures` removing
  `out-of-memory` from the transient array-task retry scenario, with a
  cross-reference to `job-retry-mechanism`'s non-transient exclusion.
- [x] 2. Edit
  `openspec/changes/fix-node22-scheduler-business-concurrency/specs/multibasin-state-idempotency/spec.md`
  same scenario, same wording, so archive cannot re-inject.
- [x] 3. Sweep `openspec/specs/multibasin-state-idempotency/spec.md` for any
  other OOM-as-transient wording (issue audit says none besides :53).

Required evidence:
- [x] `openspec validate align-multibasin-oom-non-transient --strict --no-interactive` PASS
- [x] `openspec validate fix-node22-scheduler-business-concurrency --strict --no-interactive` still PASS
- [ ] After archive (merge follow-up):
  `grep -rn "out-of-memory" openspec/specs/multibasin-state-idempotency/spec.md` → empty
- [x] `git diff --stat` contains only `openspec/**` (zero code diff)
- [x] `uv run pytest -q tests/test_retry.py` (job-retry-mechanism parity
  anchor host) green
- [x] `uv run ruff check .` (tracked tree) PASS
