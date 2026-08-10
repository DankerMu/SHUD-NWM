# Design: symlink-loop-errno-detection (#1332)

Contract source: issue #1332 (implementation-ready; root cause,
three call sites, cross-version differential evidence all in the
issue). Facts re-verified on master 9f8433bf: the three sites read
as quoted; local interpreter is CPython 3.14.2; the ELOOP paradigm
exists at `services/orchestrator/scheduler_lease.py:13,208,238`.

## D1. The predicate change

Old predicate (all three sites): `path.resolve()` inside
`try/except (OSError, RuntimeError)` — semantically "loops raise".
True on CPython ≤3.12 (pathlib walks itself, raises
`RuntimeError`/`OSError` on loops), FALSE on 3.13+ (pathlib
delegates to `os.path.realpath` non-strict: loops return the path
uncollapsed, nothing raises).

New predicate: `os.path.realpath(path, strict=True)` — and ONLY
that (round-1 P1: `Path.resolve(strict=True)` is forbidden — on
CPython ≤3.12 it raises `RuntimeError` WITHOUT an `errno`
attribute for loops, so an `except OSError` port crashes the
production interpreters and an `error.errno` read under a widened
tuple is an AttributeError; `os.path.realpath(strict=True)`
raises `OSError(errno=ELOOP)` uniformly, probed on
3.11/3.12/3.13/3.14). Kernel errno drives the split:

- `ENOENT` (and only it) → the path does not exist; each site
  keeps its pre-change nonexistence semantics (below). Non-strict
  resolution never raised for missing paths, so this lane is what
  prevents the port from inventing a new false positive. The
  ENOENT lane's fallback resolution is ALWAYS non-strict
  `os.path.realpath(path)` — NEVER pathlib (PR round-1 P1-1:
  non-strict `Path.resolve()` still raises errno-less
  RuntimeError on ≤3.12 when the `..`-collapse of a
  `<missing>/../<loop>` path meets a kernel loop; non-strict
  realpath returns the path unraised on all of 3.11-3.14,
  probed on real interpreters).
- any other `OSError` (`ELOOP`, `ENOTDIR`, `EACCES`, ...) → that
  site's unsafe/unresolvable classification. Rationale for
  fail-closed breadth: the old ≤3.12 arm also caught every
  `OSError`, so keeping the broad arm (minus ENOENT) preserves
  ≤3.12 classification exactly while restoring it on 3.13+.

Where an existing except tuple already contains `RuntimeError`, it
MUST route to the unsafe/unresolvable branch (never the ENOENT
lane) and any errno read MUST use
`getattr(error, "errno", None)` — with the mandated
`os.path.realpath` predicate the `RuntimeError` arm is
unreachable on every version, but if kept it must be safe, not
decorative (round-1 P1 hardening).

## D2. Per-site semantics (three edit sites)

1. `workers/model_registry/basins_discovery.py`
   `_safe_resolve_under_root` (def at `:506`): strict realpath;
   `ENOENT` → `resolved = Path(os.path.realpath(path))`
   (NON-STRICT realpath — PR round-1 P1-1 correction: the fallback
   must never raise; the earlier `path.resolve()` prescription was
   built on the false claim that "the strict walk already proved
   the failure is a missing component, not a loop" — strict
   realpath aborts at the FIRST missing component and proves
   nothing about the remainder, so a `<missing>/../<loop>` path
   lands in the ENOENT lane and non-strict `Path.resolve()` then
   raises errno-less RuntimeError on ≤3.12, an unhandled crash on
   the production interpreters; non-strict `os.path.realpath`
   never raises on 3.11-3.14, probed) and continue to the
   `relative_to` containment check, after which the existing
   `is_dir()` filter skips nonexistent/dangling entries silently,
   uniformly on every version; other `OSError` → append
   `BASINS_SYMLINK_UNRESOLVABLE`
   (existing `_append_warning_once` call, message/code unchanged)
   and return None. Blocking semantics need no edit:
   the code is already in `BLOCKING_WARNING_CODES` (`:43`), so the
   restored warning re-arms `importable=False` / `status='partial'`
   / quirks by itself.
2. `workers/model_registry/basins_package.py`
   `_resolve_package_path` (`:2754-2764`; round-1 P2-2 —
   attribution corrected, trace duty REMOVED, conclusion settled):
   the A3 test is restored by edit site 1 alone —
   `BASINS_DIRECTORY_UNREADABLE` comes from
   `basins_discovery.py:546` via `_ensure_readable_directory`
   (`:162`), and the py3.11 classification point for A3 is the
   symlink-descendant traversal refusal
   (`basins_package.py:2825-2830`, reached via
   `_directory_evidence :461`; spec
   shud-model-package-publication `:113-116`) — all untouched
   here. Empirical proof (fixture review): monkeypatching only
   `_safe_resolve_under_root` turns both test files green on
   3.14. THIS site's own 3.13+ degradation is different and
   currently untested: a loop AT the package source path now
   sails past the dead except and surfaces downstream as
   `BASINS_SOURCE_NOT_FOUND` (`:636-644`) instead of py3.11's
   `BASINS_PACKAGE_PATH_UNRESOLVABLE`. Fix: strict realpath;
   `ENOENT` → return the non-strict resolution (pre-change parity
   for every caller); other `OSError` → raise this helper's OWN
   existing `BASINS_PACKAGE_PATH_UNRESOLVABLE` — NEVER re-coded
   to `..._UNSAFE` (that would silently change the error contract
   for all callers with zero test coverage: repo grep shows no
   test asserts `BASINS_PACKAGE_PATH_UNRESOLVABLE`).
3. `services/orchestrator/scheduler_preflight.py` storage-root
   check (`:556-567`): strict realpath; on `ENOENT` KEEP the
   non-strict resolved value and CONTINUE through the existing
   contained→visible ladder — no early return (round-1 P2-3: the
   ladder checks `OUT_OF_ROOT` at `:581` BEFORE
   `SLURM_PREFLIGHT_{FIELD}_NOT_VISIBLE` at `:591`/`:595`; a
   missing root OUTSIDE the allowed roots must stay
   `OUT_OF_ROOT`, and no existing test pins that combination, so
   a literal early-return port would regress silently); other
   `OSError` → the existing
   `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH` structured error
   (`:567-...`), restoring py3.11 behavior.

Shared-helper option: a small
`resolve_strict_errno(path) -> tuple[Path | None, OSError | None]`
in ONE of the touched modules is acceptable if it reduces
duplication, but NOT in `packages/common/safe_fs.py` (that module
is `no_follow`-oriented; widening its API is out of scope). Three
inline try/except blocks are equally acceptable — KISS wins.

## D3. Anchors

- **A1 discovery loop blocks importability** (issue AC-1/AC-4):
  existing test
  `tests/test_basins_discovery.py::test_symlink_loop_descendant_is_skipped_with_stable_warning`
  (`:192-210`) — currently RED on 3.14 / GREEN on 3.11; must be
  GREEN on both after. Asserts `importable is False`, warnings
  contain `BASINS_SYMLINK_UNRESOLVABLE`. This is the cross-version
  differential anchor for the fail-open site.
- **A2 preflight loop is UNSAFE_PATH** (AC-3): existing
  `tests/test_production_scheduler.py::test_db_free_slurm_storage_root_check_masks_symlink_loop_path`
  (def at `:29822`) — RED on 3.14 today; GREEN both after.
- **A3 package loop is PATH_UNSAFE** (AC-2): existing
  `tests/test_basins_package_publication.py::test_basins_migration_report_rejects_unresolvable_symlink_descendant_as_json`
  (`:2531+`) — RED on 3.14 today; GREEN both after. NOTE: restored
  by edit site 1 alone (P2-2 attribution) — this anchor guards the
  discovery fix's downstream reach, not site 2.
- **A4 ENOENT is not unsafe** (AC-5, NEW tests, one per site):
  (a) discovery: a dangling symlink (or missing-target descendant)
  under the root does NOT produce `BASINS_SYMLINK_UNRESOLVABLE`,
  inventory importability is unaffected by it (pre-change silent
  skip preserved — pin `importable is True` on an otherwise-valid
  fixture and warnings free of `BASINS_SYMLINK_UNRESOLVABLE`).
  PLACEMENT (fixture-review reminder): the dangling entry must sit
  at a path discovery actually resolves — e.g. the model's
  `forcing` dir — NOT an arbitrary `model_dir/extra`, which is
  never passed through `_safe_resolve_under_root` and pins
  nothing (probed); and its TARGET must be a missing path INSIDE
  the root — a target outside the root correctly triggers the
  pre-existing `BASINS_SYMLINK_OUTSIDE_ROOT` blocker instead
  (probed, round-2 note);
  (b) preflight: nonexistent storage root still classifies
  `..._NOT_VISIBLE`, not `..._UNSAFE_PATH` (cite-or-add:
  `tests/test_production_scheduler.py:15052-15057` pins the
  missing-but-in-root case — cite it; add only what it lacks).
  A nonexistent root OUTSIDE allowed roots keeping `OUT_OF_ROOT`
  priority is currently unpinned — ADD that pin (it is the P2-3
  regression channel);
  (c) package: nonexistent source path keeps its current
  downstream code (`BASINS_SOURCE_NOT_FOUND`-family; cite-or-add).
- **A6 package-source loop restores UNRESOLVABLE** (edit site 2's
  own anchor, NEW): a symlink loop AT the package source path ⇒
  `BASINS_PACKAGE_PATH_UNRESOLVABLE` — RED on current 3.14 (today
  it degrades to `BASINS_SOURCE_NOT_FOUND` through the dead
  except), GREEN-both-sides after (py3.11 already raised
  UNRESOLVABLE here). First-ever pin on this code (repo grep:
  zero tests assert it).
- **A5 publish-path non-interference** (non-goal 4):
  `tests/test_basins_package_publication.py:1730+` stays green and
  unedited (it is green on 3.14 today — different gate).

RED/GREEN discipline: A1-A3 are pre-existing tests — the "RED"
side is the CURRENT 3.14 run (recorded before the fix), the fix
turns them green with zero test edits; that plus CI py3.11 green
is the cross-version matrix. A4 additions must be RED-provable
against a naive port (strict resolve WITHOUT the ENOENT split
misclassifies nonexistence as unsafe → A4 catches it; implementer
records this differential by temporarily removing the split).

## D4. Known limits

- `EACCES`/`ENOTDIR`-class strict-resolution failures now emit the
  unsafe/unresolvable classification on ALL versions — on 3.13+
  that replaces today's silent fail-open; on ≤3.12 it is a
  fail-closed WIDENING for inputs where non-strict resolve
  succeeded (probed: mode-000 parent, file-as-directory). At the
  DISCOVERY site this lane CHANGES SHAPE on ≤3.12 (round-3 note —
  the resolve at `:159` runs BEFORE `_ensure_readable_directory`
  at `:162`, so no earlier interceptor exists): today such input
  hard-errors `BASINS_DIRECTORY_UNREADABLE` (nonzero exit, no
  inventory); after this change it converges to the
  `BASINS_SYMLINK_UNRESOLVABLE` blocking warning + skip
  (inventory produced, `importable=False`) — explicitly ratified
  by the new spec scenario ("another kernel resolution error"),
  both outcomes fail-closed, zero existing tests affected
  (probed: 88 passed with the prescription monkeypatched).
  Round-1 P3 stands: "byte-identical on ≤3.12" holds only for the
  loop and ENOENT lanes. At the PACKAGE site there is no such
  interceptor (round-2 note, recorded as an accepted drift): a
  `<file>/sub` source path today returns successfully on ALL
  versions and classifies downstream as `BASINS_SOURCE_NOT_FOUND`;
  after this change it raises `ENOTDIR` → the helper's own
  `BASINS_PACKAGE_PATH_UNRESOLVABLE` on all versions — a more
  precise, still-rejecting code; the only existing
  `SOURCE_NOT_FOUND` pin
  (`tests/test_basins_package_publication.py:1477`) rides the
  ENOENT lane and is unaffected. No sub-classification beyond the
  ENOENT split.
- `<missing>/../<loop>` inputs (strict walk aborts ENOENT at the
  missing component; the non-strict collapse would meet a loop)
  classify as NONEXISTENCE on every version (PR round-1 P1-1 fix,
  disclosure folded per P2-1 DEFER): on ≤3.12 this is a behavior
  change — pre-change code raised and classified them
  UNRESOLVABLE/UNSAFE; on 3.13+ it is byte-identical to current
  behavior (silent skip / downstream nonexistence codes). The
  kernel itself reports ENOENT for these paths on stat, the
  ratified ENOENT scenario covers them, and nothing unsafe is
  admitted (dangling entries fail `is_dir()`/existence checks
  downstream). The earlier D4 claim "node interpreters see zero
  behavior change" is corrected to: zero change EXCEPT this
  input class, which converges from hard-classified to uniform
  nonexistence semantics.
- The dozens of defensive `except (OSError, RuntimeError)` blocks
  elsewhere do not use raise-on-loop as a predicate and are out of
  scope (issue boundary). `_preflight_allowed_roots`
  (scheduler_preflight.py:516-530) DOES use the old predicate on
  the producer side of the same ladder — routed as follow-up
  issue #1345 (PR round-1 note), not expanded here.
- Node interpreters (27=3.11.15, 22=3.12.7) see zero behavior
  change; no remote receipt is required — the oracle is the
  cross-version test matrix (local 3.14 + CI/venv 3.11), per the
  issue's own Verification field.
