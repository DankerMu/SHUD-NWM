## Context

Fixture level: **expanded** (agrees with issue #1912). Repair intensity: **high** because collection identity, monkeypatch oracles and targeted-CI ownership can silently disappear during a large physical move. Project profile: NHMS.

At baseline `d93efaecc01c6f0dfedc5546b9b50a9e4726cbd9`,
`tests/test_basins_package_publication.py` is 3,582 lines, defines 80 test functions and
collects 88 unique nodes. The sorted baseline full node-ID digest is
`f609688b6b6df4e870ef1add8afa56f009a06ef3b819257b19018731f186c138`; after removing
the module prefix, the stable sorted suffix digest is
`f8d203d5d6637541201300d0d0be3b5863c670904a556e18fd12e94801ed6787` on both
baseline and partitioned layouts. Source digest is
`c9833641a0e5379044e49ebf610f81668cd2ca3b62a9bb3ac3c642a0aaf30670`. Its only
sibling helper importer is `tests/test_basins_package.py`.

## Goals / Non-Goals

**Goals:**

- Produce exactly six collectible suites and one non-collectible helper, every replacement/new Python file strictly below 1,000 lines.
- Preserve every baseline node suffix exactly once plus normalized test body, decorator, parameter, fixture, assertion, skip and monkeypatch semantics.
- Keep the historical file as a real core suite, not a re-export shim.
- Make production-owner and helper-only targeted selection complete and mutation-proven.
- Keep the guard configuration byte-identical and current validation commands complete.

**Non-Goals:**

- No #1903 mapping tests, `.sp.riv`/`.sp.rivseg` fixture edits, production owner change or behavior fix.
- No assertion, test name, parameter ID, skip/xfail, fixture meaning, monkeypatch target or expected byte change.
- No registry-import test split, database filter, CI workflow, dependency, schema, DB, frontend, Slurm or SHUD runtime change.
- No update to archived OpenSpec/history and no collectible compatibility/re-export shim.

## Decisions

### D1: Freeze six natural responsibility owners

The finite layout follows existing behavior families rather than arbitrary equal-size chunks:

- `tests/test_basins_package_publication.py`: baseline lines 1–847; core source identity, valid publication and forcing policy (20 functions).
- `tests/test_basins_package_publication_refusal.py`: 848–1495; source/path/model/refusal contracts (17 functions).
- `tests/test_basins_package_publication_failures.py`: 1496–2007; output/planning/lock and stale-state failures (12 functions).
- `tests/test_basins_package_publication_toctou.py`: 2008–2611; object/source/forcing TOCTOU and streaming (9 functions).
- `tests/test_basins_migration_report.py`: 2612–3241; migration evidence and opt-in real smoke (16 functions).
- `tests/test_basins_package_forcing_identity.py`: 3420–3582; forcing-identity invariants (6 functions).

The current core/refusal/failure/TOCTOU/migration sections are 512–847 lines before import/helper rewiring, leaving finite headroom. A fifth partition would exceed or crowd the threshold; a seventh collectible owner adds no requirement.

### D2: One non-collectible helper owns only shared test support

`tests/basins_package_helpers.py` owns the baseline import surface and lines 3242–3417
helpers: `_write_valid_inventory`, `_object_store_env`, canonical required-file builder,
manifest helpers, CLI wrapper, valid model builder and identity snapshot. Every consuming
partition imports support at module scope. The helper contains no `test_*` definitions
and contributes zero collected nodes. `tests/test_basins_package.py` imports its two
existing helpers from this owner.

Alternative rejected: duplicating helpers risks fixture drift; `conftest.py` makes
private package fixtures repository-wide; retaining helper definitions in a collectible
suite couples all partitions to collection order and defeats helper-only routing.

### D3: Compare stable node suffixes and normalized definitions

Moved node prefixes necessarily change. Stable identity is everything after the first
`::`; pre/post sorted suffix sets must contain exactly 88 unique entries and remain
byte-identical. A second comparator maps all 80 baseline functions to one post owner and
compares normalized AST, decorators, parameter values/IDs, fixture arguments and
monkeypatch target literals. Import rewiring and source locations are the only permitted
mechanical differences; test bodies/oracles are not edited.

### D4: Make both selector boundaries explicit and biting

A `BASINS_PACKAGE_PUBLICATION_TESTS` tuple in `scripts/select_ci_tests.py` is the single
six-partition route authority. The existing `workers/model_registry/**` rule includes
all six so any Basins package production-owner change runs the whole corpus. All six
edges, including retained core, are rule-only for a
`workers/model_registry/basins_package.py` change: same-name derivation yields
`tests/test_basins_package.py`, not the publication core. `SUPPORT_MODULE_TEST_RULES`
maps `tests/basins_package_helpers.py` to exactly the six partitions plus
`tests/test_basins_package.py`; the selector's existing meta-guard rider remains
additive. Meta-tests derive the tracked six-file set, assert both routes and remove every
one of the six owner edges and seven helper edges one at a time to construct RED before
restoring GREEN.

### D5: Update current commands, not history

Every live validation command that uses the historical core path as publication coverage
uses the explicit six-file list: the M9 closeout block, the #148 regression block and the
`NHMS_RUN_BASINS_SMOKE` block. The opt-in command therefore still executes
`test_real_basins_package_smoke_opt_in` after that node moves to
`tests/test_basins_migration_report.py`. Historical M9 result bullets and archived
OpenSpec evidence remain byte-identical and continue to describe the then-current
monolith.

### D6: Keep the current validation matrix under the same structural guard

The required command update exposed a pre-existing structural blocker:
`docs/VALIDATION.md` is 1,242 lines on baseline `master`, is not grandfathered by
`.large-file-guard.json`, and any staged edit is rejected. The smallest coherent closure
moves the complete M10 #147–#152 production-closure family into new current authority
`docs/validation/production-closure.md`, using heading identity rather than mutable line
coordinates: from baseline line 172 `## M10 #147 Production Slurm Closure` through the
blank line immediately before baseline line 842 `## M19 Production Readiness Proof`
(670 lines). The root content remainder is 572 baseline lines before six short stubs.

The root matrix retains each of the six original `## M10 ...` heading texts
byte-identically as link stubs, preserving their GitHub anchor slugs, and remains the
index plus non-M10 validation matrix. The moved M10 block is byte-identical except for
the already-required #1912 six-file publication command expansion and self-lint path
changes from `docs/VALIDATION.md` to `docs/validation/production-closure.md`; no other
evidence or prose is rewritten. Commands outside that family are updated in place only
where task 3.4 requires the six publication suites. `docs/governance/DOC_STATUS.md`
recognizes both `docs/VALIDATION.md` and `docs/validation/**` as current validation
matrices. Both resulting current documents stay below 1,000 lines; no exclusion, content
compression or history rewrite is used.

Alternative rejected: dropping the required command update leaves current authority stale; adding a guard exclusion or bypass violates the issue; moving arbitrary line ranges obscures ownership. The M10 family is already one contiguous responsibility boundary with six named lanes.

## Risk Packs Considered

- Public API / CLI / script entry: **selected narrowly** — the CI selector script is a changed operational entry; product APIs/CLIs are unchanged.
- Config / project setup: **selected** — selector routing and structural guard identity are release gates.
- File IO / path safety / overwrite: **not selected** — runtime file IO is only exercised, never changed.
- Schema / columns / units / field names: **not selected** — no product schema; pytest suffix and parameter identity are covered under compatibility.
- Auth / permissions / secrets: **not selected** — no auth/secret surface.
- Concurrency / shared state / ordering: **selected narrowly** — module-scope helper imports and unique collection must not depend on import order; runtime concurrency is unchanged.
- Resource limits / large input / discovery: **selected** — every output is below 1,000 lines and selector/test discovery remains complete.
- Legacy compatibility / examples: **selected** — node suffixes, test bodies, monkeypatchs, historical core path and sibling import are compatibility contracts.
- Error handling / rollback / partial outputs: **selected** — no test may disappear, duplicate, skip or become a zero-assertion route; rollback is a source revert.
- Release / packaging / dependency compatibility: **selected narrowly** — targeted PR selection must execute every moved oracle; no package/dependency change.
- Documentation / migration notes: **selected** — active commands name all partitions; the current validation matrix is split on the complete M10 production-closure boundary without changing historical evidence; no data migration.
- Geospatial / CRS / basin geometry: **not selected** — test layout only; GIS fixtures/oracles remain byte-identical.
- Hydro-met time series / forcing windows: **not selected** — forcing tests move intact; no semantics change.
- SHUD numerical runtime / conservation / NaN: **not selected** — no solver/runtime behavior.
- PostGIS / TimescaleDB domain behavior: **not selected** — no DB tests or filter change in this corpus.
- Slurm production lifecycle / mock-vs-real parity: **not selected** — no scheduling surface.
- External hydro-met providers / snapshot reproducibility: **not selected** — no provider surface.
- Run manifest / QC provenance: **not selected** — no run evidence change.
- Published NHMS artifacts / display identity: **selected narrowly** — package artifact oracles move intact and must remain selected; artifacts themselves do not change.

## Invariant Matrix

- Governing invariant: physical ownership may change, but pytest and production-owner CI SHALL execute exactly the same 88 publication cases with the same oracles exactly once.
- Source-of-truth identity/contract: baseline sorted node suffixes, 80 normalized function/decorator/parameter fingerprints, helper bindings, selector route sets, file counts/lines and guard digest.
- Producers: six collectible publication modules, `tests/basins_package_helpers.py`, root validation index and M10 production-closure validation child.
- Validators/preflight: pytest collection, AST/fingerprint comparator, selector route/mutation tests and large-file guard.
- Storage/cache/query: none; test source layout only.
- Public routes/entrypoints: explicit six-file pytest command, `select_tests` production-owner and helper-only paths.
- Frontend/downstream consumers: `tests/test_basins_package.py`, targeted PR lane, full pytest and current validation docs.
- Failure paths/rollback/stale state: dropped/duplicated/renamed node, param/decorator/body drift, helper collection, stale import, missing selector edge or over-limit file.
- Evidence/audit/readiness: pre/post test manifests, focused/full pytest, selector RED/GREEN, root/child line counts, six heading-text/slug identities and stub-link resolution, DOC_STATUS routing, guard digest, Markdown lint, ruff, entropy, strict OpenSpec, diff and ordinary hook.
- Regression rows:
  - baseline 88 cases/80 functions → six suites collect exactly 88 unique identical suffixes and normalized fingerprints;
  - `workers/model_registry/basins_package.py` change → all six partitions selected; removing any of the six route edges, retained core included, makes the meta-test RED;
  - helper-only change → exactly six partitions + `tests/test_basins_package.py` (+ selector meta rider) selected and all imports execute;
  - seven Python outputs plus root/child validation matrices + unchanged guard → every changed/new text source under 1,000 lines, original M10 anchors resolve through stubs, no production or #1903 diff.

## Boundary-Surface Checklist

- Shared helper root: one non-collectible, module-scope-imported helper with seven explicit consumers.
- Public/test entrypoints: six-file pytest list, historical core suite, root validation index and linked M10 child.
- Read/write/delete/publish: none in implementation; existing test oracles move unchanged.
- Producer/consumer boundary: partition/helper files → selector → targeted/full pytest.
- Stale/idempotency boundary: suffix/fingerprint uniqueness prevents omissions and duplicates.
- Unchanged downstream consumers: production model-registry owners, registry test corpus and database lane.

## Risks / Trade-offs

- [Mechanical move loses or duplicates a case] → exact suffix multiset plus function fingerprints and full execution.
- [Import rewrite changes monkeypatch behavior] → AST/literal inventory and all existing biting tests.
- [Core path appears green while moved tests are blind] → explicit six-route set and per-edge mutation proof.
- [Helper becomes a suite or misses sibling importer] → non-`test_` name, zero collection, exact seven-consumer route and sibling import test.
- [Documentation keeps running only core] → update current commands; archive remains untouched.

## Migration Plan

Capture baseline contracts, mechanically generate the frozen owner files/helper, compare
before any semantic edit, update routing/current docs, verify, and merge as a
test-layout-only change. Rollback is reverting the PR. After merge and post-merge
closure, #1913 becomes unblocked; no production migration is required.

## Open Questions

None.
