## Context

Commit `94194509` legitimately added six HHe `GEOL_KSATH=2.0` declarations beside the existing hetianhe `GEOL_DMAC=4` entry. The production publisher correctly refuses a declaration whose basin is absent from the discovered inventory. Two tests were stale: one fixture built only hetianhe, so the six declarations triggered that refusal; the other required the declaration list to equal the old singleton. A config-only pull request did not run these consumers because `config/calibration_overrides.yaml` matched neither the CI backend filter nor an explicit selector rule.

Fixture level is **expanded** because the change touches the shared CI routing entrypoint. Repair intensity is **medium**: the product publisher and declaration semantics are frozen; only tests and CI routing change. There is no upstream suggested fixture level.

## Goals / Non-Goals

**Goals:**

- Restore honest tests for a multi-entry checked-in declaration without weakening unknown-basin fail-closed behavior.
- Make a future calibration declaration diff run the package/publication consumers and selector contract assertions before merge.
- Preserve the exact seven current declarations and unchanged-value byte-copy oracle.

**Non-Goals:**

- Change override values, reasons, schema, or production validation.
- Make calibration configuration a generic platform routine or alter package identity.
- Add node-22/node-27 rollout evidence; no runtime or deployed state changes.
- Repair unrelated selector gaps.

## Decisions

### D1: Generalize the default-load fixture, retain one exact declaration oracle

The default-load test will build a valid source-calibration basin for every basin named by the checked-in declaration and derive expected applied entries/bytes from those loaded declarations. This tests the behavioral contract—default loading and applying every declared entry—without requiring its fixture setup to be edited whenever a legitimate entry is added.

A separate test will still assert the exact current seven `(basin_slug, parameter, value)` tuples and measurement anchors. That test is the deliberate review gate for declaration-content changes, so dynamically building the publication fixture cannot hide an accidental slug or value change.

Alternative rejected: hard-code the six new basins in both tests. It restores green now but duplicates the declaration in fixture setup and guarantees the same stale-fixture failure on the next legitimate entry.

### D2: Preserve fail-closed behavior at its existing independent seam

The production validator is unchanged. Existing tests continue to create a declaration for a genuinely absent `charlie` basin and assert `CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY`, zero publication, dry-run refusal, and source-tree immutability. The generalized happy-path fixture does not replace or weaken this negative oracle.

### D3: Route the non-Python producer through both CI legs

The workflow backend filter gains the exact path `config/calibration_overrides.yaml`. The selector gains an exact `PathTestRule` selecting `tests/test_publish_scheduler_file_registry.py`, `tests/test_basins_package.py`, and `tests/test_select_ci_tests.py`. The meta-guard is required because this non-Python producer has no mechanically derivable import closure, so the explicit route must test its own continued existence.

Alternative rejected: selector-only mapping. Without the backend filter, the targeted job never starts and the mapping is dead. A broad `config/**` filter/rule is also rejected because unrelated configuration files have different consumers.

## Risks / Trade-offs

- [Dynamic fixture could admit an accidental declaration] → the separate exact seven-entry assertion remains load-bearing and runs on every config-only PR.
- [One route leg drifts while the other still looks correct] → block-scoped backend-filter and constructed selector-rule mutation tests pin both legs independently.
- [Routing grows the PR lane] → only two focused consumer suites plus the selector meta-suite run; no core-smoke or live oracle is added.
- [Test repair weakens product refusal] → production code is untouched and the existing absent-basin failure tests remain required evidence.

## Risk Packs Considered

- Public API / CLI / script entry: **selected** — shared CI filter/selector entrypoint changes.
- Config / project setup: **selected** — checked-in calibration declaration is the producer.
- File IO / path safety / overwrite: **not selected** — no product path/read/write implementation changes.
- Schema / columns / units / field names: **selected** — exact basin/parameter/value tuples and calibration byte fields remain pinned; no schema change.
- Auth / permissions / secrets: **not selected** — no trust or credential surface.
- Concurrency / shared state / ordering: **not selected** — deterministic test selection only.
- Resource limits / large input / discovery: **not selected** — seven small fixture basins; no production discovery change.
- Legacy compatibility / examples: **selected** — singleton-era oracle becomes a valid multi-entry oracle while retaining the original hetianhe case.
- Error handling / rollback / partial outputs: **selected** — unknown-basin refusal and zero-publish behavior must stay unchanged.
- Release / packaging / dependency compatibility: **selected** — consumer tests pin package manifest and scheduler-registry publication.
- Documentation / migration notes: **not selected** — no operator procedure or migration changes.
- Domain packs (geospatial, time series, numerical runtime, PostGIS/Timescale, Slurm lifecycle, providers, run-manifest/QC, display identity): **not selected** — no domain runtime semantics change.
- Published NHMS artifacts / package identity: **selected** — existing package-manifest consumer coverage must run and remain green; identity semantics are unchanged.

## Migration Plan

Merge the test/routing repair, require targeted CI and full local pytest to pass, then consume the repaired master as the new base for blocked PR #1850. Rollback is a normal commit revert; no data or deployment migration exists.
