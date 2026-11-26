# Tasks: compression per-tick bound 3 (#3380 slice 2)

Fixture level: `minimal`. The implementation fixture is the issue comment "Slice 2" of #3380.

## 1. Spec

- [ ] 1.1 The MODIFIED requirement reproduces the main-spec requirement apart from the three edited passages; `openspec validate compression-per-tick-bound-3 --strict --no-interactive` passes.
- [ ] 1.2 Review of the delta together with the code review of the PR.

## 2. Evidence Floor

- [ ] 2.1 The template's assignment line is `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=3`, exactly once, and a test pins it (mutation back to 2: red).
- [ ] 2.2 The in-code value for direct invocations is 3 and a test pins it (mutation back to 2: red).
- [ ] 2.3 The runbook's "Per-tick capacity" section states both constraints, the measured inputs, the invalidation conditions and the cadence conclusion for bound 3.
- [ ] 2.4 Touched test files pass locally and on node-27; `ruff check --no-cache .` clean; entropy hard gate unchanged; CI green.

Owed after the merge, on the issue (not a task of this change): the live env carries 3 (readback, budget
`--check` exit 0) and the first tick's receipt echoes `per_tick_bound` 3.
