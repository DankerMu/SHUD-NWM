# Tasks — bound-producer-replace-delete

Issue: #1119 (arm A — fix the DELETE; arm B forbidden, see proposal § Non-Goals)

## 1. Production fix

- [x] 1.1 Add `_coerce_valid_time`-equivalent handling in
      `workers/forcing_producer/store.py`, applied to **both** the incoming
      batch values and the cursor-returned window values (design D9 — the
      batch side is the one that can actually be naive).
- [x] 1.2 Rewrite `replace_forcing_timeseries`'s `_guard` to do the
      existence probe → `AS MATERIALIZED` window read → union with the
      incoming batch → `check_batch_targets_uncompressed(union)` → stash
      the DELETE parameters in an enclosing cell (design D2), reproducing
      the reference's two explanatory comments in substance (design D4).
- [x] 1.3 Change the DELETE statement to
      `... WHERE forcing_version_id = %s AND valid_time >= %s AND valid_time <= %s`.
- [x] 1.4 Add `delete_parameters_factory: Callable[[], tuple[Any, ...] | None] | None = None`
      to `_replace_values`, invoked after the pre-write hook and after
      `pre_delete_statement`, immediately before the DELETE; a `None`
      return skips the DELETE (design D3).
- [x] 1.5 **Do NOT add an `if not rows: return` short-circuit.**
      `replace_forcing_timeseries` has no early return today, so an empty
      batch means "purge this forcing_version" and that semantics must
      survive. `workers/output_parser/parser.py:800` has exactly such a
      short-circuit and is the most tempting thing in the repo to copy;
      the correct reference here is
      `forcing_domain_handoff_apply.py:744-797`, which does **not**
      short-circuit.
- [x] 1.6 The three other `_replace_values` call sites keep their argument
      lists byte-for-byte unchanged (MP4).

## 2. Tests

All in `tests/test_timescale_write_guard_wired.py`, mock-cursor oracle.

- [x] 2.1 Setup extension: add a met-side `existing_forcing_window` knob to
      `_RecordingConnection` and the two matching probe branches to
      `_RecordingCursor.execute`, mirroring the existing
      `existing_river_window` pair at `:85-95`. **For the new tests only** —
      the three existing tests need no setup change (design § Expected
      collateral (2)).
- [x] 2.2 New: existing rows extend beyond the incoming batch → guard
      receives the **union** window and the DELETE parameters equal that
      union. Red before the fix for the *guard* half of the bug.
- [x] 2.3 New: existing rows present + **empty** batch → guard receives the
      existing-only window, the DELETE **is** issued bounded to it, and
      `execute_values` does not fire. This is the test that catches a
      1.5 violation; without it the purge regression is silent.
- [x] 2.4 New: no existing rows + empty batch → **no** DELETE statement in
      `connection.executions`.
- [x] 2.5 New: compressed chunk reported inside the **union** window while
      the incoming batch alone would have been clean → `CompressedChunkWriteError`,
      no DELETE, no INSERT, rollback. This is the end-to-end discriminator
      for issue #1119's core complaint ("guard PASSes while the DELETE
      still fails"); the existing `:376` test cannot see it because its
      fixture has no existing rows.
- [x] 2.6 Tighten `:413`: `assert delete_calls[0][1] == ("fv_a",)` becomes
      the bounded triple. **Required** — see design § Expected collateral (1).
      No other assertion in this file may change.
- [x] 2.7 `tests/test_timescale_write_guard_wire_site_invariant.py` passes
      **unmodified** (MP3).

## 3. Verification matrix

| # | Command | Expected |
|---|---------|----------|
| V1 | `uv run pytest -q tests/test_timescale_write_guard_wired.py` | pass |
| V2 | `uv run pytest -q tests/test_timescale_write_guard_wire_site_invariant.py` | pass, file unmodified |
| V3 | every suite in the blast-radius closure (below) | pass |
| V4 | `uv run ruff check .` | clean |
| V5 | `openspec validate bound-producer-replace-delete --strict --no-interactive` | valid |
| V6 | `git diff` contains no hunk touching the three other `self._replace_values(...)` call sites (stations / interp weights / components) | confirmed |

Blast radius for V3 is enumerated by **AST reverse-import closure**, not by
chasing failure traces — the method correction recorded in ADR 0003 after
#1513. Concretely: every test module that transitively imports
`workers.forcing_producer.store` or `packages.common.timescale_write_guard`.
Known members to include explicitly:
`tests/test_forcing_producer.py`, `tests/test_direct_grid_variant_registration.py`
(both hand-copy `_replace_values`'s signature — see design D3).

## 4. Evidence Floor

- E1 — V1/V2/V4/V5 green on the final HEAD, output pasted.
- E2 — tests 2.2 and 2.5 demonstrated **red before / green after**: run
  each against the pre-fix `store.py` and paste the failures.
- E3 — `git diff --stat tests/test_timescale_write_guard_wire_site_invariant.py`
  is empty.
- E4 — the `tests/test_timescale_write_guard_wired.py` diff is quoted in
  full, with each hunk classified as (a) the required `:413` tightening,
  (b) setup extension for new tests, or (c) new test bodies. **Any hunk
  that fits none of those three is a finding.**
- E5 — blast-radius closure list and its pytest result.
- E6 — local-only is sufficient: no node-22/node-27 evidence required
  (mock-cursor oracle, per the issue's arm-A ruling).

## 5. Report, don't fix (out of scope)

- [x] 5.1 `replace_forcing_components` (`store.py:727`) has the same
      unbounded-DELETE shape; its target `met.forcing_version_component` is
      a plain table (`db/migrations/000005_met.sql:112,126` create
      hypertables for `forcing_station_timeseries` and
      `best_available_selection` only), so it is currently harmless.
      Report if that changes.
- [x] 5.2 The union-window logic now exists in three copies
      (`forcing_domain_handoff_apply.py`, `workers/output_parser/parser.py`,
      `store.py`). Deduplication rejected here (design D6) — file a
      follow-up if a fourth appears.
- [x] 5.3 Anything discovered about the DB-mode producer's retirement
      belongs to the separate arm-B issue, not here.
