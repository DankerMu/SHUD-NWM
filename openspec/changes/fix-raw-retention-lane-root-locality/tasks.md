# Tasks: fix-raw-retention-lane-root-locality (#2104)

Fixture level: expanded (design.md carries the risk triage, seams and pack
decisions). Ordering gate: this change merges **before** #2100's mode change.

## 1. Runner and operator check

- [x] 1.1 `scripts/node27_raw_retention.py` `_collect_mapped_lane`: wrap the
  `source_root` probe in `try/except OSError`; on error append the
  `<prefix>_source_unsafe` / `path_unavailable` skip (key
  `<lane>/<storage_source>`, `path`, `error`, `error_type`) and continue with
  the next source. Replace the "Known limit (#2104)" comment with a one-line
  pointer to the locality contract.
- [x] 1.2 `_iter_dirs`: move the `is_dir()/is_symlink()` comprehension inside
  the `try`; return `(entries, error)`; update the three callers so a raised
  listing records `raw_root_unsafe` (raw root), `raw_source_unsafe`
  (`raw/<source>`) or `<prefix>_source_unsafe` (mapped lane source root), each
  with `path`, `detail: path_unavailable`, `error`, `error_type`.
- [x] 1.3 `_safe_resolved_dir` adds `error_type` next to `error` on the
  `path_unavailable` blocker; `_resolve_lane_root` adds `error`/`error_type`
  in its `except OSError` skip and forwards both from the blocker on the
  `resolved is None` path. Reword the docstring sentence that says the 3.14
  label fix "is tracked by #2104" (this change does not fix it).
- [x] 1.4 `infra/env/node27-raw-retention.example`: clause 4 of the documented
  `jq` becomes `select(.reason | endswith("_unsafe"))`; add the per-source
  reading `jq '.skipped[] | select(.reason | endswith("_source_unsafe"))'`
  next to the lane-level one; then correct **every** sentence of the
  "HOW TO READ THIS UNIT" block that describes clause 4's match set or the
  item-1 crash path — at least: the stale-receipt cause ("#2104 items 1 and
  4"; item 1's crash path no longer exists), the "seven summary shapes" count
  (test 2.8 drives an eighth), the exit-code table line for `*_root_unsafe`,
  the "Exit 0 means those four clauses … no `*_root_unsafe` lane root"
  sentence, and the "Forward pointer … Tracked as #2104 item 3" paragraph
  (entries now carry the errno). Keep every other line of the exit-code
  table true. Append a one-line supersession pointer (原文保留、追加指针, per
  that file's r2-cand-03 precedent) after the sentence in
  `openspec/changes/display-v2-national-timeline-precip-overlay/tasks.md`
  that describes the shipped clause as matching only `_root_unsafe`.

## 2. Tests (`tests/test_node27_raw_retention.py`, pinned to 3.11 like the ancestor test; every mode-changing test carries the `os.geteuid() == 0` skip guard and restores modes in `finally`)

- [x] 2.1 `test_an_untraversable_canonical_root_retires_only_that_lane`
  (`store/canonical` at `0o000`; expected values in design.md Required
  evidence).
- [x] 2.2 `test_a_readable_but_untraversable_raw_root_retires_only_the_raw_lane`
  (`store/raw` at `0o444`).
- [x] 2.3 `test_iter_dirs_reports_the_listing_error_instead_of_raising`.
- [x] 2.4 Extend `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes`
  with the non-empty `error`/`error_type` assertions; keep the existing
  `(reason, detail)` assertions byte-identical; reword its docstring sentence
  "The label fix is tracked by #2104".
- [x] 2.5 Red proof: 2.1 and 2.2 fail on pre-change source with an escaping
  `PermissionError` (batched red run, output kept in the PR evidence).
- [x] 2.6 `test_an_untraversable_canonical_source_root_retires_only_that_source`
  (`store/canonical/IFS` at `0o444`, `canonical/` traversable).
- [x] 2.7 `test_a_stale_lane_root_handle_is_reported_with_its_errno`
  (monkeypatched `ESTALE` on `<store>/raw` resolve).
- [x] 2.8 `test_documented_operator_check_goes_red_on_an_unsafe_skip`
  (real `jq` running the program extracted from the example file; skip when
  `jq` is absent).
- [x] 2.9 `test_an_untraversable_raw_source_root_retires_only_that_source`
  (`store/raw/gfs` at `0o444`, `raw/` traversable; the raw per-source caller
  builds its key from the on-disk name, a different caller from 2.6's).

## 3. Evidence Floor

- [ ] 3.1 `uv run ruff check .` clean.
- [ ] 3.2 `uv run pytest -q tests/test_node27_raw_retention.py` green locally.
- [ ] 3.3 node-27: same pytest file green in a disposable worktree
  (`/home/nwm/tmp/wt-2104`, `TMPDIR=/home/nwm/tmp`, `uv sync` inside the
  worktree only; `/home/nwm/NWM` untouched), run as `nwm` so the mode cases
  and the `jq` test execute (no skips other than the documented root guard).
- [ ] 3.4 node-27 item-4 receipt: documented `jq` check exits non-zero on
  `raw-retention-20260912T054536Z.json` and zero on the freshest summary.
- [ ] 3.5 `openspec validate fix-raw-retention-lane-root-locality --strict
  --no-interactive` valid.
- [ ] 3.6 PR body states the ordering gate: merged before #2100's mode change.

## Known limits (recorded, not deferred)

- CPython 3.14 `pathlib` swallows every `OSError` on the wrapped probes, so
  there the same states surface as `*_root_missing` / empty listings; the
  supported interpreter is the pinned 3.11 and the tests say so.
- This runner keeps `skipped[]` + exit `0` for an enumeration `OSError`
  where `openspec/specs/mvt-tile-cache-lifecycle` uses `failed[]` + exit `1`;
  the divergence and its reason are recorded in design.md.
