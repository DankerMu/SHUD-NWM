# Tasks

Fixture level: `standard`. Risk packs: production download selection (a false
"not ready" re-downloads old cycles; a wrong order starves fresh cycles),
operator contract of the pass summary.

Must preserve: an explicit `--cycle-time` / `NODE27_DOWNLOAD_CYCLE_TIME` is
used as given; with no object-store root, or no manifest in the lookback
window, the latest allowed cycle is selected; the chain start is still the
oldest cycle with a manifest for any selected source (all three directory
spellings), or `NODE27_DOWNLOAD_CONTINUITY_START_TIME`; the selector never
lists a `raw/<source>` directory (`test_automatic_cycle_selection_probes_window_without_raw_directory_scan`
stays green without weakening); the existing summary keys and their meaning;
the signature and return type of `_select_automatic_cycle_time`; one pass still downloads one cycle for every selected source;
the adapters, the retention script and `source_cycle_raw_manifest.py` are not
edited.

## 1. Code (`scripts/node27_download_cycles.py`)

- [x] 1.1 A readiness probe for one source and cycle that calls
  `nfs_raw_manifest_readiness(source_id=…, cycle_time=…, object_store_root=…, object_store_prefix=…)`
  and returns its `status` and `reason`. The prefix is the environment's
  `OBJECT_STORE_PREFIX`, empty when unset, which is what the adapters started
  by the same pass write manifests with; it is not the readiness function's
  own default. Check that an empty prefix makes the readiness function expect
  the bare-key URI the adapters write in that case; if it does not, stop and
  report. An exception from the call is caught and reported as status
  `error`, reason `readiness_error`.
- [x] 1.2 Selection. A cycle is ready when every selected source answers
  `ready`; a cycle with at least one selected source that is not ready is a
  not-ready cycle. Scan every allowed cycle from the chain start to the
  latest allowed cycle and collect the not-ready cycles, each with the
  status and reason of every selected source. Select: the latest allowed
  cycle if it is in that list; else the oldest cycle in the list; else the
  latest allowed cycle. One new function returns both the selected cycle and
  the detail (latest allowed cycle, not-ready list); `main` calls it once.
  `_select_automatic_cycle_time` keeps its signature and becomes a thin
  wrapper that returns the selected cycle of that function. No module state.
- [x] 1.3 Summary. On automatic selection the pass summary
  (`last-summary.json` and the printed summary) and the `--preflight-only`
  output carry one new key holding: the latest allowed cycle, the count of
  not-ready cycles, and the not-ready cycles themselves (cycle time, and per
  source the status and reason), oldest first, at most 20 entries with the
  count telling how many there are in all. Every summary written after an
  automatic selection was made carries it, the lock-blocked one included.
  Summaries written before selection (blocked preflight, selection failure)
  have no detail and do not get the key; selection is not moved ahead of
  preflight to fill them. No key is added on explicit selection. Existing keys
  are unchanged.
- [x] 1.4 Runbook text: the two passages that say the runner selects "the
  first missing raw cycle after the existing contiguous raw chain"
  (`infra/env/README.md`, `infra/env/node27-download.example`) state the new
  rule: a cycle counts as downloaded when its raw files are complete, the
  latest allowed cycle is served first when it is incomplete, then the oldest
  incomplete cycle; name `NODE27_DOWNLOAD_CONTINUITY_START_TIME` as the way
  past a cycle the upstream no longer serves, and say that it ignores the
  lookback window and must be unset again once that cycle has left the
  window.

## 2. Tests (`tests/test_node27_download_cycles.py`)

- [x] 2.0 A fixture helper that writes a cycle the readiness rule accepts: a
  manifest with the source, cycle time, manifest URI and entries whose
  `local_key` values point at non-empty files under the cycle directory, in
  the directory spelling the live store uses (`raw/gfs/…`, `raw/IFS/…`). Take
  the exact manifest shape from what `nfs_raw_manifest_readiness` checks and
  from `tests/test_source_cycle_raw_manifest.py`; assert once that the helper's
  output is `ready`. The old `{}` helper stays for tests that need a
  manifest-only cycle.
- [x] 2.1 The issue's red test: allowed hours `0,12`, delay 8 h; cycles
  C-12h, C and C+12h complete for GFS and IFS; one IFS entry file of C
  removed; `now` chosen so that C+12h is the latest allowed cycle ->
  `_select_automatic_cycle_time` returns C. Also the same with the file
  emptied instead of removed.
- [x] 2.2 The latest allowed cycle not ready (manifests only, no files) and
  an older cycle with no manifest for IFS -> the latest allowed cycle (before
  the change: the older cycle).
- [x] 2.3 Latest allowed cycle ready; two older cycles not ready: the older
  of the two has manifests for both sources and one IFS file removed, the
  newer of the two has no IFS manifest -> the older of the two (before the
  change: the newer of the two, the only one without a manifest).
- [x] 2.4 An older cycle with no IFS manifest, latest allowed with no
  manifest at all -> the latest allowed cycle (before the change: the older
  cycle).
- [x] 2.5 Complete chain -> the latest allowed cycle (same before the
  change), and the detail lists no not-ready cycle (new).
- [x] 2.6 Latest allowed cycle ready; an older cycle complete for GFS, with
  an IFS manifest and one IFS file missing -> that older cycle is selected
  and listed with IFS not ready and GFS ready (before the change: the latest
  allowed cycle). With `sources=("GFS",)` the same store selects the latest
  allowed cycle and lists nothing.
- [x] 2.6b The latest allowed cycle ready for GFS and not for IFS, no older
  gap -> the latest allowed cycle, listed as not ready.
- [x] 2.6c A non-default `OBJECT_STORE_PREFIX`, set explicitly in the test
  environment (every test sets or deletes the variable; a value from the
  developer's shell must not leak in). The latest allowed cycle and an older
  cycle are complete; the latest carries the environment's prefix in its
  manifest URI, the older one another prefix -> the older cycle is selected
  and listed with the readiness rule's reason (before the change: the latest
  allowed cycle). With both written with the environment's prefix nothing is
  listed.
- [x] 2.6d `NODE27_DOWNLOAD_CONTINUITY_START_TIME` set to a cycle after an
  older not-ready cycle: the older cycle is neither selected nor listed, and
  the scan starts at the given cycle even though earlier manifests exist.
- [x] 2.7 The readiness call raising for one cycle (patched) -> that cycle is
  listed with reason `readiness_error` and selection still returns.
- [x] 2.8 Summary (`main` takes no clock: patch `_latest_allowed_cycle` or
  build the cycles relative to it): a pass (with the adapter runs faked as
  the file's other tests do) and a `--preflight-only` run on a store with one not-ready older
  cycle both carry the new key with that cycle, its per-source status and
  reason and the count; with 25 not-ready cycles the list has 20 entries,
  oldest first, and the count says 25; an explicit cycle time adds no key.
- [x] 2.9 Existing tests.
  - `…_uses_first_raw_continuity_gap`: its latest allowed cycle is
    2026-07-02T12. All cycles it writes, the 07-02 ones included, become
    complete cycles; the expected 2026-06-28T00 stays.
  - `…_treats_any_selected_source_gap_as_missing` and
    `…_probes_window_without_raw_directory_scan`: their latest allowed cycle
    (2026-06-26T12) has no IFS manifest, so the expected 2026-06-26T12 stays
    under either order; the cycles they mean as downloaded become complete
    cycles. The "any source gap" case for an older cycle is carried by 2.6.
  - The no-scan test keeps forbidding `iterdir` on `raw/<source>` and adds
    the lower-case GFS directory (`raw/gfs`) to the forbidden set. The
    readiness rule stats files by key and reads the manifest; if it lists a
    directory, stop and report.
  - `test_missing_cycle_time_defaults_to_automatic_selection` replaces
    `_select_automatic_cycle_time` with a lambda; `main` now calls the new
    function, so this test patches the new function instead (the one allowed
    change to it; its assertions stay).

## 3. Evidence

- [x] 3.1 Red first, on the unmodified source: 2.1, 2.2, 2.3, 2.4, 2.6
  (first half), 2.6b's listing, 2.6c and 2.8 fail; the selection assertions
  of 2.5, 2.6b and 2.6d pass before and after (their detail assertions are
  new). Tests that call the new function cannot run before it exists: for
  the red run, express the selection assertions through
  `_select_automatic_cycle_time` and say which assertions could only be
  added afterwards. Output saved to
  `.workplans/3238/red-runs.log`.
- [x] 3.2 Mutations, each applied by hand, checked, recorded, restored
  (`cmp` against a scratch copy):
  - M1: the probe answers ready whenever the manifest exists -> 2.1 red.
  - M2: drop "latest allowed first" (oldest not-ready always wins) -> 2.2 and
    2.4 red.
  - M3: always select the latest allowed cycle when anything is not ready ->
    2.1 and 2.3 red.
  - M4: judge a cycle ready when any selected source is ready -> 2.6 and
    2.6b red.
  - M4b: pass the readiness function's default prefix instead of the
    environment's -> 2.6c red.
  - M5: let the readiness exception propagate -> 2.7 red.
  - M6: remove the cap of 20 -> 2.8 red; report newest first -> 2.8 red.
- [x] 3.3 `uv run --no-sync ruff check --no-cache .` clean;
  `uv run --no-sync pytest -q tests/test_node27_download_cycles.py tests/test_source_cycle_raw_manifest.py`
  green, plus any test file that imports `scripts/node27_download_cycles.py`
  (`git grep -l node27_download_cycles -- tests`). Markdown lint on the
  changed docs.
- [x] 3.4 node-27, before merge: the test files of 3.3 in a detached
  temporary worktree.
  - Evidence: `9ac9d76f5`, 251 passed, no skips; re-run on the last code commit (receipt on the PR).
- [x] 3.5 CI: full suite.

## 4. After merge (evidence owed on the issue, not a task of this change)

On node-27 after `git pull --ff-only`:

- `--preflight-only` against the production environment reports the same
  automatic cycle as before the pull and an empty not-ready list.
- On a scratch object-store root (never the live one) holding copies of the
  manifests of three consecutive cycles ending at the latest allowed one,
  with a one-byte placeholder file for every `local_key`, and one entry file
  of the middle cycle removed: the selector picks the middle cycle. Without
  the complete latest cycle in the scratch root the check proves nothing (the
  latest would be selected under either rule).
- Selection wall time before and after.
- Over the following 48 h, `last-summary.json` shows automatic selection with
  no not-ready cycle left behind, and the scheduler evidence shows no new
  raw-files-missing gap.

The issue is closed with that evidence, not by the merge.

## Review (both seats clean, round 1)

- Taken from the notes: a pass-level test for the complete chain; the
  selection tests clear `NODE27_DOWNLOAD_CONTINUITY_START_TIME`,
  `NODE27_DOWNLOAD_CONTINUITY_LOOKBACK_DAYS` and `NHMS_NODE27_DOWNLOAD_SOURCES`;
  the unused manifest-only helper removed; the wrapper's header comment and
  `docs/runbooks/production-ops/service-bringup.md` state the real rule (both
  described neither the old rule nor the new one).
- The red run used a variant of the test file that asserted selection only;
  detail assertions were added once the new function existed (noted in the
  evidence log). The readiness-error test was red before the change because
  the probe did not exist yet, which 3.1 did not list.
- Not changed: the chain-start scan (`is_file` on the manifest paths) can
  raise on an unreadable object-store root and end the pass without a
  summary. It is older than this change and outside the probe's guard.
- Not changed: the no-directory-scan test forbids `Path.iterdir` only.
