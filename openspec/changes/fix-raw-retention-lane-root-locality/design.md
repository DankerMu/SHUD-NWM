# Design: fix-raw-retention-lane-root-locality (#2104)

Fixture level: expanded
Project profile: NHMS (`openspec/project-profile.md`)
Upstream suggested level: absent (hand-written issue) — expanded by the
mandatory triggers `delete`, `path`, `file output` (the runner deletes trees
and writes the receipt the operator check reads).

## Change surface

- `scripts/node27_raw_retention.py`: `_collect_mapped_lane` (source-root
  probe), `_iter_dirs` (listing helper) and its three callers in
  `_collect_raw_lane` / `_collect_mapped_lane`, `_safe_resolved_dir` and
  `_resolve_lane_root` (skip payload).
- `tests/test_node27_raw_retention.py`: new tests beside
  `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes`.
- `infra/env/node27-raw-retention.example`: the documented operator check and
  the prose this change would otherwise falsify (see "Operator check").

## Must preserve

- Exit codes: `0` completed, `1` any `failed[]`, `2` preflight blocked. A skip
  is never a failure and never changes the exit code.
- `SCHEMA_VERSION` `nhms.node27_raw_retention.production.v4` and every
  existing summary key. New skip fields are additive.
- The existing pinned skip shapes `("<lane>_root_unsafe", "path_unavailable")`
  and `<lane>_root_missing`, and all existing `skipped[]` reasons
  (`source_not_enabled`, `grid_definitions_preserved`,
  `unparseable_cycle_name`, `within_retention_window`, `unsafe_target_path`,
  `source_unmappable`, `precip_cache_root_unconfigured`).
- Deletion semantics: `collect_targets` still completes before the first
  `rmtree`; `rmtree` failures still land in `failed[]` with `error_type`.
- `_resolve_lane_root`'s promise: absence and unsafety of a lane root retire
  that lane only.
- The 3.14 divergence documented in `_resolve_lane_root` (pathlib swallows
  every `OSError` there) stays a documented, un-fixed limit; tests that assert
  `path_unavailable` stay pinned to the repo's 3.11 interpreter exactly like
  the existing ancestor test.
- The documented operator check keeps exiting `0` on a healthy summary and
  `1` on every shape its exit-code table already lists.

## Must add/change

1. **Per-source locality (item 1).** In `_collect_mapped_lane`, wrap the
   `source_root.is_symlink() / is_dir()` probe in `try/except OSError`. On
   error append `{"key": "<lane>/<storage_source>", "reason":
   "<prefix>_source_unsafe", "path": str(source_root), "detail":
   "path_unavailable", "error": str(error), "error_type":
   type(error).__name__}` and `continue`. `<prefix>` is the lane's existing
   skip prefix (`canonical` for the canonical lane, `precip_cache` for the
   PNG cache lane) so the vocabulary lines up with `<prefix>_root_unsafe`.
2. **Listing locality (item 2).** `_iter_dirs` returns the directory list
   **and** the `OSError` it hit, if any (the comprehension moves inside the
   `try`). Every caller records a skip when the error is set:
   - raw root listing → `{"key": "raw", "reason": "raw_root_unsafe",
     "path", "detail": "path_unavailable", "error", "error_type"}`;
   - raw `<source_dir>` listing → key `raw/<source>`, reason
     `raw_source_unsafe`;
   - mapped-lane `<source_root>` listing → key `<lane>/<storage_source>`,
     reason `<prefix>_source_unsafe`.
   A listing that raised is therefore never reported as "empty directory".
3. **Errno in skip entries (item 3), fixed at the source.**
   `_safe_resolved_dir` builds `error_type` (`type(error).__name__`) next to
   the `error` it already builds for `path_unavailable` — additive for the
   preflight blocker too. `_resolve_lane_root` adds `error`/`error_type` in
   its own `except OSError` branch and forwards both fields verbatim from the
   blocker on the `resolved is None` path. `error_type` is always the
   exception class name, never a reason string.
4. **Operator check (`infra/env/node27-raw-retention.example`).** Without
   this, the change would turn "found after 26h" into "never found": a
   crashed tick used to trip the `finished_at` freshness clause, whereas a
   per-source skip leaves the summary fresh, `failed[]` empty, and the new
   reason invisible to clause 4 (`endswith("_root_unsafe")`) and to the
   documented lane-liveness reading (`select(.key | contains("/") | not)`).
   Route chosen: **(b)** widen clause 4 to `endswith("_unsafe")` so every
   `*_root_unsafe` and `*_source_unsafe` skip exits `1`; add a per-source
   reading (`select(.reason | endswith("_source_unsafe"))`) next to the
   lane-level one; rewrite the "Forward pointer … Tracked as #2104 item 3"
   paragraph and the exit-code table line to describe the errno-carrying
   entries. Route (a) — synthesising a lane-level `<prefix>_root_unsafe` when
   every configured source fails — is rejected: it would hide a single failing source among healthy ones, and
   the lane-level entry would have to be synthesised because the lane-root
   gate never exercises the lane root's own `x` (every probe stats it from
   its parent). The operator reading is therefore: per-source entries for
   every configured source under one lane prefix mean the lane root itself
   lost `x`; a single entry means one source root.
5. **Item 4 (stale receipt).** No code. Route (a) is already live in the
   example (`finished_at` within 26h). The evidence is a receipt line from
   node-27: the documented `jq` on a stale summary file exits non-zero and on
   the freshest file exits zero. Route (b) (`status: "crashed"` receipt) is a
   recorded non-goal: it would repair the receipt without repairing locality,
   and items 1–2 remove the crash path it would report.
6. **Stale claims this change makes false.** The docstring sentence in
   `_resolve_lane_root` and the docstring of
   `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes` that
   say the 3.14 label fix "is tracked by #2104" are reworded: this change
   does not fix that label (non-goal below) and must not claim it.

## Seams under test

- `node27_raw_retention.main([...])` with env — the public runner seam used by
  every existing lane test (exit code, printed payload, summary file).
- `node27_raw_retention.collect_targets(config, now=...)` — the collector seam
  for the skip vocabulary.
- `node27_raw_retention._iter_dirs` — the one private helper whose return
  contract changes; tested directly for the `0o444` root because the public
  seam cannot distinguish "empty" from "raised" without it.
- The documented `jq` program in `infra/env/node27-raw-retention.example`,
  executed by the real `jq` against summaries the runner produced (skipped
  where `jq` is not installed; it is on node-27).

## Selected risk packs

- Error handling / rollback / partial outputs: every `OSError` at the two new
  seams becomes one skip entry with a stable reason; no partial summary, no
  swallowed listing; the summary file is written on the failing tick.
- File IO / path safety / overwrite: no new path is opened; the wrapped probes
  are the existing ones; symlink rejection at `source_root` and in
  `_iter_dirs` (`not entry.is_symlink()`) is preserved inside the `try`.
- Public API / CLI / script entry: `main`, `collect_targets`, `run_retention`
  signatures unchanged; CLI flags unchanged; summary keys additive.
- Legacy compatibility / examples: the documented operator check must go red
  on the new per-source skip and stay green on a healthy summary; proven by
  running the example's own `jq` program (tasks 2.8).
- Documentation / migration notes: the example's prose that this change
  falsifies is corrected in the same PR (tasks 1.4).

## Risk packs considered (core)

- Public API / CLI / script entry: selected — additive summary payload on a
  production runner.
- Config / project setup: not selected — no env key added or changed.
- File IO / path safety / overwrite: selected — see above.
- Schema / columns / units / field names: not selected — `SCHEMA_VERSION`
  unchanged; additive optional fields on skip entries only; `schemas/` holds
  no JSON Schema for this summary.
- Auth / permissions / secrets: not selected — the change reacts to `EACCES`;
  it grants nothing and reads no secret (`error` strings are `OSError` text
  with a path, same as the existing `failed[]` entries).
- Concurrency / shared state / ordering: not selected — single process under
  the wrapper's `flock -n`; ordering of collect-then-delete unchanged.
- Resource limits / large input / discovery: not selected — the listing set
  is unchanged; only its error path moves.
- Legacy compatibility / examples: selected — see above.
- Error handling / rollback / partial outputs: selected — core of the change.
- Release / packaging / dependency compatibility: not selected — stdlib only.
- Documentation / migration notes: selected — see above.

Domain packs (NHMS profile): none selected — no geospatial, time-series,
numerical, PostGIS/Timescale or Slurm surface is touched.

## Required evidence

- `tests/test_node27_raw_retention.py::test_an_untraversable_canonical_root_retires_only_that_lane`:
  `store/canonical` chmod `0o000` (not `store`; `os.geteuid() == 0` skip
  guard and `finally` mode restore as the sibling test) with
  `--sources gfs,ifs`, an aged raw cycle, an aged canonical cycle and an aged
  cache cycle → `exit_code == 0`, `status == "completed"`, summary file
  written and equal to the printed payload, `deleted` keys
  `== ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]`, `skipped[]`
  contains `("canonical_source_unsafe", "path_unavailable")` for
  `canonical/gfs` **and** `canonical/IFS`, each with non-empty `error` and
  `error_type == "PermissionError"`, `counts.failed == 0`, the canonical
  cycle directory still exists.
- `tests/test_node27_raw_retention.py::test_a_readable_but_untraversable_raw_root_retires_only_the_raw_lane`:
  `store/raw` chmod `0o444` (same guard/restore) → `exit_code == 0`, summary
  written, `skipped[]` contains `("raw_root_unsafe", "path_unavailable")`
  with non-empty `error`/`error_type`, canonical and cache aged cycles
  deleted, raw cycle still present.
- `tests/test_node27_raw_retention.py::test_an_untraversable_canonical_source_root_retires_only_that_source`:
  `store/canonical` traversable, `store/canonical/IFS` chmod `0o444` (same
  guard/restore), aged cycles in `canonical/gfs`, `canonical/IFS`, raw and
  cache → `skipped[]` contains `("canonical_source_unsafe",
  "path_unavailable")` for `canonical/IFS` with non-empty `error`/`error_type`,
  `deleted` includes the `canonical/gfs`, raw and cache cycles, the IFS cycle
  still exists, `exit_code == 0`.
- `tests/test_node27_raw_retention.py::test_an_untraversable_raw_source_root_retires_only_that_source`:
  `store/raw` traversable, `store/raw/gfs` chmod `0o444` (same guard/restore),
  aged cycles in `raw/gfs`, `canonical/IFS` and the cache →
  `skipped[]` contains `("raw_source_unsafe", "path_unavailable")` with key
  `raw/gfs` and non-empty `error`/`error_type`, the raw cycle survives, the
  canonical and cache cycles are deleted, `exit_code == 0`.
- `tests/test_node27_raw_retention.py::test_iter_dirs_reports_the_listing_error_instead_of_raising`:
  direct call on a `0o444` directory containing one subdirectory (same
  guard/restore) → returns no entries and an `OSError` whose `errno` is
  `EACCES`; on a readable directory returns the subdirectories and `None`; a
  symlinked child is excluded.
- Extend `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes`:
  both `*_root_unsafe` entries carry non-empty `error` and
  `error_type == "PermissionError"` (the existing `(reason, detail)`
  assertions stay byte-identical).
- `tests/test_node27_raw_retention.py::test_a_stale_lane_root_handle_is_reported_with_its_errno`:
  monkeypatch `Path.resolve` (or the equivalent seam) to raise
  `OSError(errno.ESTALE, ...)` for `<store>/raw` only → the raw lane skip is
  `raw_root_unsafe` / `path_unavailable` with `error` containing the ESTALE
  text and `error_type == "OSError"`; other lanes unaffected.
- `tests/test_node27_raw_retention.py::test_documented_operator_check_goes_red_on_an_unsafe_skip`:
  extract the `jq -e '…'` program from `infra/env/node27-raw-retention.example`
  and run it with the installed `jq` (skip when absent) on (i) the summary of
  the canonical-`0o000` case → exit `1`, (ii) a healthy production-shaped
  summary the runner produced → exit `0`.
- Red proof: the canonical-`0o000` and raw-`0o444` lane tests fail on
  pre-change source with `PermissionError` escaping `main` (batched red run,
  per implementer contract).
- `uv run pytest -q tests/test_node27_raw_retention.py`, `uv run ruff check .`
  locally; the same pytest file on node-27 in a disposable worktree under
  `/home/nwm/tmp` with `TMPDIR=/home/nwm/tmp` (Linux, non-root `nwm`, `jq`
  present, so every mode case and the operator-check test bite; the active
  tree `/home/nwm/NWM` is not touched).
- Item 4 receipt (node-27, read-only): the documented `jq` check run against
  `raw-retention-20260912T054536Z.json` (stale, healthy) exits non-zero and
  against the freshest file exits zero.

## Divergence recorded

`openspec/specs/mvt-tile-cache-lifecycle/spec.md` makes an enumeration
`OSError` a `failed[]` entry with exit `1`. This runner keeps the lane-local
`skipped[]` + exit `0` contract that #2099 established and #2104's
acceptance criteria require; visibility is restored through the operator
check (clause 4 now matches every `*_unsafe` skip) rather than through the
exit code, so the unit's `ActiveState` stays a raw-lane signal.

## Non-goals

- #2100's permission remedy (this change is its precondition, not its
  substitute).
- The 3.14 `pathlib` mislabel (`*_root_missing` instead of `path_unavailable`
  on an unreadable root) — documented limit, out of scope here as in #2099.
- `_safe_target`'s trailing `is_dir()/is_symlink()` (race-only, not reachable
  by a permission state).
- Route (b) `status: "crashed"` receipt (see item 5).
- Any change to the unit, the wrapper, exit codes, or `SCHEMA_VERSION`.

## Review focus

- Locality: with `canonical/` (or `raw/`) untraversable, the other lanes still
  delete and the summary is written on that same tick.
- No listing error is reported as an empty directory (`_iter_dirs` callers).
- Skip payload is additive; pinned shapes unchanged; no exit-code drift.
- The documented `jq` really exits `1` on the new per-source skip and `0` on
  a healthy summary (the test executes the program from the example file,
  not a copy).
- Tests really run the failing syscall on 3.11 (non-root guard, `0o000` on
  `store/canonical` not `store`, `0o444` on `store/raw` / `canonical/IFS`).
