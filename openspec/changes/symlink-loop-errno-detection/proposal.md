# Proposal: Errno-driven symlink-loop detection (Python-version-independent)

## Why

Issue #1332 — three production gates use "non-strict `Path.resolve()`
raises on a symlink loop" as their unsafe-path trigger. CPython 3.13+
delegates pathlib resolution to `os.path.realpath`, which in
non-strict mode returns the looping path unraised — the `except
(OSError, RuntimeError)` arms become dead code. Consequences on
3.13+ (reproduced on CPython 3.14.2, this repo's dev interpreter;
production nodes 27=3.11.15 / 22=3.12.7 and CI py3.11 are NOT
affected today — this is an upgrade-armed regression plus three
permanently red tests on 3.13+ dev machines):

1. `workers/model_registry/basins_discovery.py:511-522`
   (`_safe_resolve_under_root`) — the only FAIL-OPEN: a self-looping
   symlink under the Basins root resolves to itself, passes the
   containment check, fails `is_dir()`, and is silently skipped.
   Inventory reports `importable=True`, `warnings=[]`,
   `status='valid'`, `default_import_eligible=True`; the blocking
   warning `BASINS_SYMLINK_UNRESOLVABLE` (in
   `BLOCKING_WARNING_CODES`, `basins_discovery.py:43`) can never
   fire. Downstream reach of THIS site (round-2 P2-1 attribution):
   the package migration-report test's observed downgrade —
   `BASINS_DIRECTORY_UNREADABLE` where py3.11 yields
   `BASINS_PACKAGE_PATH_UNSAFE` — is caused HERE
   (`basins_discovery.py:546` via `_ensure_readable_directory
   :162`) and is cured by this site's fix alone.
2. `workers/model_registry/basins_package.py:2754-2764`
   (`_resolve_package_path`) — error-code downgrade of its own,
   currently untested: a symlink loop AT the package source path
   sails past the dead except and surfaces downstream as
   `BASINS_SOURCE_NOT_FOUND` (`:636-644`) instead of py3.11's
   `BASINS_PACKAGE_PATH_UNRESOLVABLE` (still rejects; no
   fail-open).
3. `services/orchestrator/scheduler_preflight.py:556-567`
   (storage-root check) — error-code downgrade:
   `SLURM_PREFLIGHT_..._NOT_VISIBLE` instead of `..._UNSAFE_PATH`
   (still rejects).

## Ruling

Replace exception-dependence with EXPLICIT strict resolution at the
three sites, using `os.path.realpath(path, strict=True)` +
`except OSError` and classifying by errno — the paradigm already
used correctly elsewhere in this repo
(`services/orchestrator/scheduler_lease.py:208`
`error.errno in {ELOOP, ENOTDIR}`; sibling usages across
`services/production_closure/**`, `packages/common/safe_fs.py`).
`Path.resolve(strict=True)` is FORBIDDEN for this fix (fixture
round-1 P1): on CPython ≤3.12 — exactly the production
interpreters — it raises `RuntimeError` WITHOUT an `errno`
attribute for symlink loops, so a literal `except OSError` port
would crash 3.11/3.12 where today's code warns/blocks, and an
`error.errno` read under `except (OSError, RuntimeError)` is an
AttributeError. `os.path.realpath(strict=True)` raises
`OSError(errno=ELOOP)` uniformly on 3.11-3.14 (probed on all
four).
`ENOENT` MUST be split out at each call site and preserve that
site's pre-change nonexistence semantics — strict resolution raises
on missing paths where non-strict never did, so an unsplit port
would misclassify "does not exist" as "unsafe" (a new false
positive this fixture explicitly guards against with ENOENT
anchors). The alternative (pin `requires-python <3.13`) is
rejected: it downgrades a semantic defect to a toolchain freeze and
leaves the fail-open in the code.

## What Changes

1. `workers/model_registry/basins_discovery.py`
   `_safe_resolve_under_root`: strict resolve; `ENOENT` → fall back
   to non-strict resolution and continue to the containment check
   (pre-change semantics for dangling/missing descendants: silently
   skipped later by `is_dir()`); any other `OSError` →
   `BASINS_SYMLINK_UNRESOLVABLE` warning + skip (fail-closed,
   restores the blocking-warning path on 3.13+; on ≤3.12 behavior
   is unchanged because loops raise in both modes).
2. `workers/model_registry/basins_package.py`
   `_resolve_package_path` (round-1 P2-2 correction — attribution
   fixed): the failing A3 test is restored by the DISCOVERY fix
   alone (`BASINS_DIRECTORY_UNREADABLE` originates from
   `basins_discovery.py:546` via `_ensure_readable_directory
   :162`; the py3.11 classification point for that test is the
   symlink-descendant traversal refusal
   `basins_package.py:2825-2830`, untouched here; empirically
   verified — monkeypatching only `_safe_resolve_under_root`
   turns both test files green on 3.14). This site's OWN 3.13+
   degradation is loop → downstream `BASINS_SOURCE_NOT_FOUND`
   (`:636-644`) instead of its py3.11
   `BASINS_PACKAGE_PATH_UNRESOLVABLE`. Fix: same strict +
   ENOENT-split pattern; non-ENOENT failures raise this helper's
   OWN existing `BASINS_PACKAGE_PATH_UNRESOLVABLE` (NEVER
   re-coded to `..._UNSAFE`); `ENOENT` returns the non-strict
   resolution (pre-change parity for all callers).
3. `services/orchestrator/scheduler_preflight.py` storage-root
   check: strict + errno split; non-ENOENT failures produce the
   existing `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH` structured
   error (py3.11 parity); on `ENOENT` keep the non-strict
   resolved value and CONTINUE through the existing
   contained→visible ladder (no early return — `OUT_OF_ROOT` at
   `:581` must keep priority over `..._NOT_VISIBLE` at `:591`;
   round-1 P2-3).
4. Spec delta: MODIFIED requirement "Basins root discovery is
   explicit" (basins-asset-discovery) — adds the unresolvable-
   descendant blocking scenario and the ENOENT-is-not-unsafe
   scenario. The package/preflight surfaces need no delta: A3's
   traversal refusal code is specified at
   shud-model-package-publication `:113-116`; site 2's own
   `BASINS_PACKAGE_PATH_UNRESOLVABLE` restoration is pure
   version-parity with zero requirement change (the code appears
   nowhere in `openspec/specs/` — A6 is its first-ever pin,
   round-2 note); and the preflight spec surface
   (`slurm-array-runner-integration/spec.md:38-42`) requires only
   a storage preflight blocker without code granularity (round-1
   P3 cite correction — no `SLURM_PREFLIGHT*` token exists
   anywhere in `openspec/specs/`).

## Non-goals

1. No repo-wide sweep of `except (OSError, RuntimeError)` — only
   the three sites that use raise-on-loop as a predicate.
2. No change to `BLOCKING_WARNING_CODES` membership, warning
   vocabularies, or any error-code renames.
3. No Python-baseline decision (`pyproject.toml` untouched).
4. The 3.14-green publish-path case
   (`tests/test_basins_package_publication.py:1730+`) goes through
   a different gate and is not touched.

## Risk triage

- Level: compact — three localized call-site edits following an
  in-repo paradigm; the defining risk is the ENOENT reverse
  regression, held by dedicated anchors.
- Must-preserve: the loop and ENOENT lanes bit-identical to
  current ≤3.12 behavior at all three sites; other errno lanes
  (`EACCES`/`ENOTDIR`-class) become uniformly fail-closed on ALL
  versions (round-1 P3 correction: non-strict resolve DID succeed
  on some of those inputs even on ≤3.12; at the discovery site
  that lane converges from today's hard
  `BASINS_DIRECTORY_UNREADABLE` to the blocking
  `BASINS_SYMLINK_UNRESOLVABLE` warning — ratified by the new
  spec scenario, both fail-closed; round-3 note); the three
  existing
  symlink-loop tests stay the contract (they are the cross-version
  matrix: GREEN on CI py3.11 today, RED on local 3.14 → both-green
  after); publish-path test `:1730+` untouched and green.
- Risk packs: filesystem-safety (fail-open closure is the point);
  error-contract fidelity (codes must not drift on either Python).
  Not selected: DB/scheduler-runtime (preflight edit is a pure
  classification branch), concurrency (none).
- Evidence mapping (exact, round-2 note): A1 → AC-1/AC-4, A2 →
  AC-3, A3 → AC-2, A4 → AC-5; AC-6 (floor greens) is covered by
  the Evidence Floor itself; A5 guards non-goal 4 and A6 (added
  in fixture round 1) is edit site 2's own first-ever code pin —
  neither maps to a numbered AC;
  floor = `uv run pytest -q tests/test_basins_discovery.py
  tests/test_basins_package_publication.py` + targeted
  `tests/test_production_scheduler.py -k
  symlink_loop` + `uv run ruff check .` + `openspec validate
  symlink-loop-errno-detection --strict --no-interactive`; dual
  interpreters: local CPython 3.14.2 (this venv) + the hand-built
  py3.11 venv from #1330 (or CI py3.11) for the both-versions
  proof.
