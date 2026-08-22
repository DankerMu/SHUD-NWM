## Context

The selector already classifies Python under `apps/api/`, `packages/`, `services/`, `workers/`, and `scripts/` as backend surface, but `_same_name_script_test()` derives `tests/test_<stem>.py` only for `scripts/`. The caller already unions a derived target with explicit `PATH_TEST_RULES`, verifies the target exists, and suppresses unknown-backend core-smoke fallback only after a real mapping hit. Current-master census finds 66 tracked source/same-name-suite pairs across the five prefixes; 15 do not select the suite (13 core-smoke-only and 2 partially covered by the broad API rule).

**Fixture level:** expanded. This changes a shared CI routing entrypoint and therefore meets a mandatory expanded trigger. **Repair intensity:** medium: the implementation seam is isolated, but false-negative or false-positive routing affects every backend PR.

## Goals / Non-Goals

**Goals:**

- Reach each tracked same-name suite from its source under all five existing backend Python prefixes.
- Preserve explicit-rule union and core-smoke fallback for sources without an existing same-name suite.
- Turn current and future source/suite pairs into a mechanically enforced tree invariant.
- Make cross-prefix basename collisions a checked compatibility boundary.
- Bound the added PR-lane cost with current-tree collection and execution evidence.

**Non-Goals:**

- Suite-to-suite import routing (#1561), non-same-name importer closure (#1455), or whole-tree importer analysis.
- Changes to `PATH_TEST_RULES`, `CORE_SMOKE_TESTS`, `.github/workflows/ci.yml`, CI timeout, test gating markers, or any production/test behavior outside selector routing.
- Requiring every existing `scripts/` same-name suite to prove a top-level import; that is a separate importer-closure policy.

## Decisions

### D1: Reuse one backend-prefix authority

The same-name helper SHALL consume the same five-prefix classification used by `_is_backend_python_path`, preferably through one shared immutable prefix constant. Maintaining a second prefix tuple is rejected because it recreates the drift that this change removes.

### D2: Keep derivation file-level and existence-gated

The helper returns only `tests/test_<PurePosixPath(path).stem>.py`; the existing caller remains responsible for `_test_target_exists`, set union, and `matched=True`. Sources without a matching file therefore retain explicit routing or core-smoke fallback, and node-id behavior remains untouched. Adding 13 explicit rules is rejected as another hand-maintained patch.

### D3: Derive completeness from the tracked tree

The existing scripts-only pair helper/test SHALL be generalized over the same five prefixes. It asserts every tracked source with an existing same-name suite selects that suite. Smoke-overlap assertions must distinguish unknown-fallback leakage from explicit broad-rule overlap: `apps/api/**` deliberately selects `tests/test_api.py`, which is also a core-smoke member.

### D4: Guard basename collisions rather than banning them

Two source paths can map to one basename suite. Current master has one such cross-prefix stem, `best_available`; its suite imports both source modules. A tree-derived guard SHALL require a shared suite to import every module represented by a colliding stem. Hard-coding `best_available` or rejecting all collisions is less faithful and would turn a valid shared suite into an exception.

### D5: Measure both complete and issue-baseline cost

The PR SHALL report current `pytest --collect-only` count and actual wall clock for all 14 unique suites reached by the 15 previously missing source/suite pairs. It SHALL separately report the issue's 13 core-smoke-only suite subtotal so the historical 309-node acceptance baseline is not conflated with the full current 383-node gap set (which also includes `tests/test_runtime_mode.py`). No timeout or marker changes are permitted to make the budget pass.

### D6: Every same-name source route schedules the selector meta-guard

A source-only PR can ADD a new colliding source that maps to an existing same-name suite, which is exactly the change class the collision/import contract exists to reject — but the selector meta-guard was only scheduled from changed-test and routed-support-module branches, so the collision guard first ran after merge. At the same-name derivation chokepoint, an EXISTING same-name target accepted for a backend source therefore also selects `SELECTOR_META_GUARD_TEST`. This is the simplest bounded guard route: it reuses the existing meta-guard rider instead of adding per-basename runtime logic, and it does not weaken the collision/import contract or the no-suite fallback. The cost is one additional suite (the selector's own, 209 tests / ~16 s measured after syncing the final branch with `origin/master` at `57a14098`) on every same-name source PR, far inside the 35-minute targeted-job cap. D6 intentionally broadens the OUTPUT of every existing same-name source route (and scripts/ same-name routes, which now gain the rider too): the same-name suite inclusion, explicit business targets, set-union, target-existence gate, missing-target filtering, and no-suite fallback semantics are preserved exactly, and only the meta-guard target is added to the emitted list. Sources without a same-name suite keep their existing explicit/fallback output byte-for-byte.

## Must Preserve

- Same-name suite inclusion and explicit business targets: a backend source with an existing same-name suite still selects that suite, and an explicit rule still contributes its targets; only the meta-guard target is intentionally ADDED to the emitted list for same-name routes (D6), never replacing or removing a target.
- Set-union semantics when an explicit rule and same-name derivation both match.
- Core-smoke fallback for backend Python sources with neither an explicit rule nor an existing same-name suite: the fallback output stays byte-for-byte unchanged (no rider is added there).
- Existing broad `apps/api/**` rule, changed-test/meta-guard routing, missing-target warnings, gated-suite policy, and all guarded-module/importer closure contracts.

## Seams Under Test

- `select_tests([source], repo_root=...)`: source with matching suite -> suite included; source without one -> existing explicit/fallback route.
- Tracked-tree pair derivation: all current and future pairs under the five prefixes -> same-name suite selected.
- Collision derivation/import inspection: every source sharing a mapped stem -> shared suite imports its dotted module.
- CLI selector invocation for `packages/common/storage.py`: output includes `tests/test_storage.py` without unknown-backend core-smoke substitution.

## Risk Packs Considered

- Public API / CLI / script entry: **selected** — shared selector CLI and `select_tests()` routing behavior change; covered by direct and CLI-level selector tests.
- Config / project setup: **not selected** — no configuration or dependency change.
- File IO / path safety / overwrite: **not selected** — only tracked path strings and existing file-existence checks; no new read/write primitive.
- Schema / columns / units / field names: **not selected** — no payload or data schema.
- Auth / permissions / secrets: **not selected** — no credential or permission surface.
- Concurrency / shared state / ordering: **not selected** — pure serial selection; no shared mutable runtime state.
- Resource limits / large input / discovery: **selected** — tracked-tree growth and added test execution can expand PR cost; covered by pair census, collection count, and wall-clock run.
- Legacy compatibility / examples: **selected** — scripts mapping, explicit rules, API broad rule, and no-suite fallback must remain compatible.
- Error handling / rollback / partial outputs: **selected** — nonexistent derived targets must not count as mappings or suppress fallback.
- Release / packaging / dependency compatibility: **not selected** — no package/dependency change.
- Documentation / migration notes: **selected** — OpenSpec contract must replace the scripts-only wording; no user migration.
- NHMS domain packs: **not selected** — no geospatial, forcing, SHUD, DB, Slurm, provider, manifest, or published-artifact semantics.

## Required Evidence

- Reverted production helper with new tests retained -> new-scope/tree guard fails; restored helper -> green.
- `uv run pytest -q tests/test_select_ci_tests.py` -> selector contract suite passes.
- Selector CLI for `packages/common/storage.py` -> includes `tests/test_storage.py`; unknown module without a suite -> retains core smoke.
- Full 14-suite gap set: 383 collected nodes on the fixture baseline and actual `pytest -q` wall clock remeasured on the implementation head; 13 core-smoke-only suites are also reported separately against the 309-node / ~12-second baseline, with no marker/timeout weakening.
- `uv run ruff check scripts/select_ci_tests.py tests/test_select_ci_tests.py` and strict OpenSpec validation -> clean.

## Risks / Trade-offs

- **[Basename collision selects an unrelated suite]** -> mechanically require a collision suite to import every colliding source module.
- **[Generalized no-smoke guard false-reds on the API broad rule]** -> assert absence of fallback, not absence of every target that happens to belong to `CORE_SMOKE_TESTS`, when an explicit rule independently owns that target.
- **[Added suites lengthen PR CI]** -> measure actual current suite cost; do not broaden beyond same-name pairs.
- **[Two prefix authorities drift]** -> one shared constant feeds backend classification and derivation.

## Invariant Matrix

Not required: repair intensity is medium, not high/broad-expanded. The governing routing invariant and all relevant surfaces are nevertheless explicit above; there is no persistence/state machine.

## Migration Plan

One atomic selector-and-meta-test commit. Rollback reverts that commit; no stored data or deployment migration exists.

## Open Questions

None. Current-tree census and the one collision are mechanically resolved before implementation.
