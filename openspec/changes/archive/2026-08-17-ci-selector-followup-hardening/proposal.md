# Proposal: ci-selector-followup-hardening

## Why

PR #1452 closed the fourth recurrence of the selector-mapping defect
family with a direct-importer mechanical guard, and its review deferred
three orthogonal residues into #1453/#1454/#1455. Each is a distinct
hole in the same gate: (#1453) a PR changing only a `tests/` support
file (`conftest.py`, `integration_helpers.py`, `__init__.py`)
self-selects that file, pytest returns `NO_TESTS_COLLECTED = 5`, and
`check=True` in ci.yml turns it into a misleading
`CalledProcessError` red — fail-closed but unreadable, and zero real
assertions run for a change that can affect the whole suite; (#1454)
the #1254 unconditional meta-guard accumulation survives the
missing-target filter, so a PR whose only backend change deletes a
`tests/test_*.py` now has count 1 instead of 0 and silently loses the
full-tree collect-only smoke that used to catch broken cross-test
imports (40 of 188 test files import other test modules at top level);
(#1455) the #1452 guard sees only direct top-level importers — a
`real_backend.py`-only PR leaves 8 one-hop non-gated suites unselected
(including `tests/test_reconcile_sacct_parse.py`, which consumes the
sacct parsing constants directly) — plus three adjacent unguarded
surfaces: the 9 directory-rule gap audit lives only in PR #1452's body,
duplicate `PATH_TEST_RULES` patterns split rule ownership silently
(#1443's `display_coverage.py` line will collide on merge), and
`GATING_MARKER_NAMES` has no mechanical anchor to the conftest
auto-skip set it mirrors.

## What Changes

One PR closing #1453 + #1454 and delivering #1455 items (1)(3)(4).
#1455 item (2) — the 9-directory rule-gap disposition — is split into
its own follow-up change/PR per the issue's own two-batch
recommendation: fixture review re-derived the gap set at HEAD as **73
distinct suites** across the 9 directories, a triage workload that
would dominate and destabilize this PR; #1455 stays open until that
second PR lands.

- **#1453**: in the changed-test branch of
  `scripts/select_ci_tests.py`, a `tests/` Python file that is not a
  pytest-collectible suite by BASENAME (final round-1/Phase-7 shape:
  basename matches neither `test_*.py` nor `*_test.py`, at any depth —
  mirroring pytest's default `python_files`) no longer self-selects;
  it maps to the selector meta-guard suite
  (`tests/test_select_ci_tests.py`), so the selection
  is always collectible. The affected class is 8 tracked files (not
  the issue's 3): `conftest.py`, `integration_helpers.py`,
  `__init__.py`, `mock_shud_omp.py`, `river_identity_backfill_fakes.py`,
  `slurm_template_helpers.py`,
  `fixtures/mapping_builder/in_memory_grid_snapshot.py`,
  `fixtures/mapping_builder/keliya/build.py` — all verified exit-5
  self-selections today. Recorded trade-off: today's misleading hard
  red (zero assertion information) becomes a green that runs the
  meta-guard suite plus the #1454 collect-only smoke; for
  `conftest.py`/`integration_helpers.py` the `database` filter's
  `real-db-integration` job still compensates, the other six have no
  compensating job — accepted because the red they lose carried no
  evidence either. The pin
  `test_meta_guard_accumulation_is_scoped_to_test_file_names` is
  updated (its meta-guard-non-spill intent survives; the pinned
  defect behavior does not). A tree-derived invariant test covers every
  current and future `tests/` support module without hardcoding names.
- **#1454**: `--github-output` gains `meta_guard_only=true|false`
  (true iff the post-filter selection equals exactly the meta-guard
  suite — a SHAPE property, honestly including selector-development
  PRs whose diff-specific target IS that suite; accepted, the extra
  cost is one collection pass on exactly the PR class that changes the
  gate itself). ci.yml's `unit-test-targeted` job runs the targeted
  selection AND the labeled full-tree `pytest tests/ -q --collect-only`
  smoke when `meta_guard_only == 'true'` — restoring the
  import-surface guard for deleted-test-file PRs, and extending it to
  #1453's support-file PRs (their selection collapses to the same
  shape). The smoke's labeling on this branch must NOT claim zero
  assertions were executed (the targeted run did execute); wording is
  adapted. Route-C pins comment updated to name the revived class.
- **#1455 (1)**: the guarded-module closure derivation is extended by
  exactly ONE import hop (tracked non-test modules that top-level
  import a guarded module contribute their own non-gated top-level
  importer suites), hop-bounded as forward-looking policy: fixture
  review re-derived at HEAD that under this top-level-edge definition
  the unbounded fixed point currently EQUALS the one-hop set (14
  required suites for `real_backend`, of which 8 are unselected
  today; the issue's "64" figure is not reproducible under any
  matching definition — an any-depth-edge derivation gives 71 suites,
  which is the blowup the bound guards against). The
  `services/slurm_gateway/**` (or a dedicated `real_backend.py`) rule
  grows to cover the derived one-hop set (measured cost of the 8:
  754 passed / ~20 s locally).
- **#1455 (2)** — SPLIT to the follow-up change/PR (see above).
- **#1455 (3)**: a guard over `PATH_TEST_RULES` duplicate patterns with
  an explicit intentional-duplicate allowlist (today exactly
  `services/orchestrator/scheduler.py`, whose two-entry layering is
  deliberate); any other duplicate — including the one #1443's merge
  would introduce for `packages/common/display_coverage.py` — goes
  red. `CHANGED_TEST_FILE_RULES` is exempt (its duplicates are the
  `only_when_any_changed` design).
- **#1455 (4)**: `GATING_MARKER_NAMES` is anchored: the auto-skip
  marker set is AST-derived from `tests/conftest.py`
  `pytest_collection_modifyitems`, the binding assertion is the
  EQUALITY `derived == GATING_MARKER_NAMES | {"grib"}` (design
  decision 7 — subset/difference framings pass silently when conftest
  stops skipping a marker), `grib`'s deliberate absence is a visible
  assertion, and `real_disk`/`timescaledb_210` are asserted NOT
  auto-skipped (adding them would wrongly exclude suites that really
  run).

Non-goals: no change to any production module logic or to the content
of any newly-selected test suite; no route-A/B empty-selection policy
change (the collect-only branch's semantics stay; #1454 only adds a
second trigger for the smoke); no `scripts/**/*.sh` gating (#1138); no
`CHANGED_TEST_FILE_RULES` early-continue semantics change (#1254
settled); [SUPERSEDED by round-1 R1.1 — nested suites are now
correctly classified as suites, see design decision 8's note];
no support-helper → importer-suite mapping (#1453's "按需追加"
option): the selector is static-pattern-based, a per-helper rule
snapshot invites exactly the rot this guard family exists against,
and the class's baseline today is a zero-information red — the
meta-guard + collect-only floor strictly improves it; revisit if a
helper-only PR ever ships a real regression past the smoke.

## Capabilities

- `ci-contract-baseline`: MODIFIED requirements "Empty targeted-test
  selection MUST be loudly self-identifying" (meta-guard-only collapse
  triggers the labeled collect-only smoke alongside the targeted run),
  "Guarded-module selector rules MUST cover their non-gated importer
  closure" (one-hop transitive extension), "Changed-test PRs MUST run
  the selector meta-guards" (support-file mapping replaces
  self-selection); ADDED requirements for the duplicate-pattern
  allowlist guard and the gating-marker anchor. The directory-rule
  disposition delta belongs to the follow-up change.

## Impact

- `scripts/select_ci_tests.py` (changed-test branch, PATH_TEST_RULES
  growth, `_write_github_output`), `.github/workflows/ci.yml`
  (`unit-test-targeted` run step only), `tests/test_select_ci_tests.py`
  (guard derivation, new pins, updated pins),
  `tests/conftest.py` read-only (AST anchor source, not modified).
- Closes #1453 (p2/S) + #1454 (p3/S); delivers #1455 (p2/M) items
  (1)(3)(4) with item (2) and issue closure in the follow-up PR.
  Verification per their `Verification:` fields — all local
  (`uv run pytest -q tests/test_select_ci_tests.py`,
  `uv run ruff check .`, selector CLI spot checks).
