# Tasks — bound-producer-replace-delete

Issue: #1119 (arm A — fix the DELETE; arm B forbidden, see proposal § Non-Goals)

## 1. Production fix

- [ ] 1.1 Add `_coerce_valid_time`-equivalent handling in
      `workers/forcing_producer/store.py` so cursor-returned window values
      are comparable to `ForcingTimeseriesRow.valid_time` (design D9).
- [ ] 1.2 Rewrite `replace_forcing_timeseries`'s `_guard` to do the
      existence probe → `AS MATERIALIZED` window read → union with the
      incoming batch → `check_batch_targets_uncompressed(union)` → stash
      the DELETE parameters in an enclosing cell (design D2), reproducing
      the reference's two explanatory comments verbatim in substance
      (design D4).
- [ ] 1.3 Change the DELETE statement to
      `... WHERE forcing_version_id = %s AND valid_time >= %s AND valid_time <= %s`.
- [ ] 1.4 Add `delete_parameters_factory: Callable[[], tuple[Any, ...] | None] | None = None`
      to `_replace_values`, invoked after the pre-write hook and before the
      DELETE; `None` return skips the DELETE (design D3).
- [ ] 1.5 Confirm `store.py:311`, `:386`, `:716` are untouched (MP4).

## 2. Tests

- [ ] 2.1 New: existing rows extend beyond the incoming batch → guard
      receives the **union** window and the DELETE parameters equal that
      union. This is the test that is red before the fix for the *guard*
      half of the bug.
- [ ] 2.2 New: no existing rows + empty batch → **no** DELETE statement in
      `connection.executions`.
- [ ] 2.3 New: no existing rows + non-empty batch → DELETE bounded to the
      batch window (the previously-unbounded statement never appears).
- [ ] 2.4 Repair the fake cursor in
      `tests/test_timescale_write_guard_wired.py` so it answers the two new
      probe queries. **Setup only** — assertions must not be relaxed
      (design § Expected collateral).
- [ ] 2.5 `tests/test_timescale_write_guard_wire_site_invariant.py` passes
      **unmodified** (MP3).

## 3. Verification matrix

| # | Command | Expected |
|---|---------|----------|
| V1 | `uv run pytest -q tests/test_timescale_write_guard_wired.py` | pass |
| V2 | `uv run pytest -q tests/test_timescale_write_guard_wire_site_invariant.py` | pass, file unmodified |
| V3 | `uv run pytest -q tests/test_forcing_producer_store.py` (if present) + every suite importing `workers.forcing_producer.store` | pass |
| V4 | `uv run ruff check .` | clean |
| V5 | `openspec validate bound-producer-replace-delete --strict --no-interactive` | valid |
| V6 | `git diff` on `store.py` shows no change at `:311`, `:386`, `:716` | confirmed |

Blast radius for V3 is enumerated by **AST reverse-import closure**, not by
chasing failure traces — the method correction recorded in ADR 0003 after
#1513. Concretely: every test module that transitively imports
`workers.forcing_producer.store` or `packages.common.timescale_write_guard`.

## 4. Evidence Floor

- E1 — V1/V2/V4/V5 all green on the final HEAD, output pasted.
- E2 — the union-window test (2.1) demonstrated **red before / green
  after**: run it against the pre-fix `store.py` and paste the failure.
- E3 — the AST meta-guard file shows zero diff (`git diff --stat` on
  `tests/test_timescale_write_guard_wire_site_invariant.py` is empty).
- E4 — the `test_timescale_write_guard_wired.py` diff is setup-only;
  quote the diff hunks and state explicitly that no assertion changed.
- E5 — blast-radius closure list and its pytest result.
- E6 — local-only is sufficient: no node-22/node-27 evidence required
  (mock-cursor oracle, per the issue's arm-A ruling).

## 5. Report, don't fix (out of scope)

- [ ] 5.1 `replace_forcing_components` (`store.py:716`) has the same
      unbounded-DELETE shape; its target `met.forcing_version_component` is
      a plain table, so it is currently harmless. Report if that changes.
- [ ] 5.2 The duplicated union-window logic across
      `forcing_domain_handoff_apply.py`, `workers/output_parser/parser.py`,
      and now `store.py` is a third copy. Deduplication rejected here
      (design D6) — file a follow-up if a fourth appears.
- [ ] 5.3 Anything discovered about the DB-mode producer's retirement
      belongs to the separate arm-B issue, not here.
