# Tasks: guard-mvt-instant-range-2033 (#2033)

Fixture level `expanded`; repair intensity `high`. Risk-pack ledger and the Invariant Matrix live in
`design.md`. Cite **symbol anchors**, never line numbers (line refs drifted between the issue text and
`origin/master`).

## 1. Shared helper (`services/tiles/mvt.py`)

- [x] 1.1 Add `class MvtTimeOutOfRangeError(ValueError)` with a docstring naming the CPython behavior
  it wraps (`astimezone` raises `OverflowError`, not `ValueError`, when normalization leaves
  `datetime.min/max`).
- [x] 1.2 Wrap **both** `.astimezone(UTC)` calls in `canonical_mvt_time` (the `isinstance(value, datetime)`
  branch and the `_parse_iso_datetime` branch) with
  `except (OverflowError, ValueError) as exc: raise MvtTimeOutOfRangeError(...) from exc`.
  No sentinel/fallback return — design D1 forbids it (a non-canonical string would enter `cache_key`
  and `_read_cache` identity).
- [x] 1.3 The unparseable-text path (`_parse_iso_datetime` returns `None`) still returns `text_value`
  unchanged.

## 2. Route-boundary validator (`apps/api/routes/hydro_display.py`)

- [x] 2.1 Extract `_require_representable_instant(value: datetime, field_name: str) -> datetime` from the
  existing `try/except (OverflowError, ValueError)` block inside `_require_seconds_precision_instant`.
  Same status/code/message/details as today (`422`, `VALIDATION_ERROR`,
  "Tile time instants must be representable in UTC.").
- [x] 2.2 `_require_seconds_precision_instant` keeps the `value.microsecond` 422 and then delegates the
  range check to `_require_representable_instant`. One implementation, no sibling copy (design D2).
  This is a **behavior-preserving extraction under existing callers**: `precip.py::precip_index`,
  `precip.py::precip_png`, and `hydro_display.py::list_layer_valid_times` (via
  `_validated_national_valid_time_selector`) all keep byte-identical status/code/message/details.
  **The success-path return value is part of the contract and must not change**:
  `_require_seconds_precision_instant` today returns the ternary
  `value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)` — copy it
  verbatim. Both branches are load-bearing: the newly guarded legacy routes take a lax
  `valid_time: datetime`, so a **naive** instant is a real input class there, and a literal
  `return value.astimezone(UTC)` would reinterpret it in server-local time (platform-dependent:
  macOS local vs node-27 UTC+8) and newly 422 a naive extreme. It must keep returning a UTC-normalized
  instant — returning the caller's original object instead would still pass
  every error-path test while breaking `precip.py::_require_whole_hour_instant`, which reads
  `.minute`/`.second`. `_RFC3339_INSTANT_RE` accepts half-hour offsets, so
  `2026-09-02T20:00:00+05:30` (14:30 UTC) would pass the whole-hour gate and be floored to hour 14 by
  `cycle_token` — the exact cache poisoning that gate's docstring exists to prevent.
- [x] 2.3 `hydro_mvt_tile`: call `_require_representable_instant(valid_time, "valid_time")` immediately
  after `validate_xyz(z, x, y)`, discarding the return (design D4). Everything downstream keeps
  receiving the original `valid_time`.
- [x] 2.4 `hydro_national_mvt_tile`: same placement, before the `TileInput(...)` whose `source_version=`
  kwarg calls `national_discharge_source_version(session, ...)`.
- [x] 2.5 Do **not** apply `_require_seconds_precision_instant` to the legacy routes (design D3 —
  would newly reject in-range sub-second instants).

## 3. Tests (`tests/test_hydro_display_mvt_scaling.py`, local oracle)

Every new-behavior test ships with its **red proof**: run it against pre-change source and paste the
failing output in the implementer report.

- [x] 3.1 Helper unit: `canonical_mvt_time` raises `MvtTimeOutOfRangeError` (and `isinstance(exc, ValueError)`)
  for `9999-12-31T23:59:59-08:00` and `0001-01-01T00:00:00+08:00`, in **both** the `datetime` form and
  the `str` form. Assert `not isinstance(exc, OverflowError)` is irrelevant — assert the type is
  `MvtTimeOutOfRangeError`.
- [x] 3.2 Helper unit: in-range regression — `...T12:00:00Z`, `...T12:00:00+00:00`, `...T12:00:00.000Z`,
  `...T20:00:00+08:00` all canonicalize exactly as on `origin/master`; unparseable text round-trips.
- [x] 3.3 Route: `TestClient` + fake session that **counts `session.execute` calls**. For both legacy
  routes × both extreme instants: status `422`, body code `VALIDATION_ERROR`, and
  `execute_count == 0`. (This is the discriminating assertion for acceptance criterion 3; the issue's
  measured baseline was `sql=1` for national and two queries for single-run.)
- [x] 3.4 Route regression: `not-an-instant` still 422 with `sql=0` (FastAPI-level, unchanged), and an
  in-range instant still reaches SQL (`execute_count > 0`) on both legacy routes.
- [x] 3.5 Route regression: the `{source}/{cycle}` route still returns 422 for both extreme instants
  **and** for an in-range sub-second instant (its existing guard must not regress).
- [x] 3.6 Legacy compatibility: an in-range sub-second instant on both legacy routes is **not** 422 and
  canonicalizes to `...:00.500000Z` — this is the test that fails if someone reuses
  `_require_seconds_precision_instant` on the legacy path.
- [x] 3.7 Cache identity: `cache_key(TileInput(...))` for the four in-range spellings of one instant is
  byte-identical to the value computed by `origin/master`'s helper (import the old module via
  `git show origin/master:services/tiles/mvt.py` into a throwaway module, or assert against the
  literal digest recorded in the implementer report).
- [x] 3.8 Sibling tile routes untouched: for `river-network-national`, `river-network/{basin_version_id}`
  and `met-stations/{basin_version_id}` (all `valid_time=None`), assert
  `cache_key(TileInput(..., valid_time=None, ...))` is byte-identical to the digest computed by
  `origin/master`'s helper and the 200 response bytes are unchanged — or name the existing test
  functions in `tests/test_hydro_display_mvt_scaling.py` that already pin exactly this and report them
  as already-covered.
- [x] 3.9 Refactor blast radius — `list_layer_valid_times`: request
  `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=<out-of-range>`. **`source` and `cycle`
  must both be present**: a `cycle`-only request is rejected by
  `_validated_national_valid_time_selector`'s "source and cycle must be given together." branch and
  never reaches the refactored validator, so it has zero oracle power. Assert status 422 **and**
  `message == "Tile time instants must be representable in UTC."` (asserting only `code` cannot tell
  the two 422 branches apart). With an in-range `cycle`, assert an unchanged `cycle_key` and unchanged
  response body.
- [x] 3.10 Refactor blast radius — precip (`tests/test_precip_overlay.py`): `precip_index` and
  `precip_png` still return 422 `VALIDATION_ERROR` with the same status/code/message/details for an
  out-of-range `cycle` / `valid_time`, and are byte-identical on the in-range whole-hour path (index
  body + PNG bytes + `cache_key` string). `precip.py` itself must NOT be edited (design D4b).
- [x] 3.11a Naive-instant branch lock: both legacy routes with a **naive** `9999-12-31T23:59:59` and
  `0001-01-01T00:00:00` are **not** 422 and reach SQL (`execute_count > 0`), identical to today —
  `canonical_mvt_time` treats naive as UTC and never overflows on them.
- [x] 3.11 Return-value contract lock (the test that fails if 2.2 returns the original object):
  `precip_index` / `precip_png` with an in-range **half-hour-offset** instant such as
  `2026-09-02T20:00:00+05:30` (= 14:30 UTC) still return 422
  "Precipitation instants must fall on a whole hour." Plus a direct unit assertion that
  `_require_seconds_precision_instant(datetime.fromisoformat("2026-09-02T20:00:00+05:30"), "cycle")`
  returns a value whose `utcoffset()` is zero and whose `minute` is 30.

## 4. Local verification (orchestrator, Phase 2)

- [ ] 4.1 `uv run ruff check .` — zero findings.
- [ ] 4.2 `uv run pytest -q tests/test_hydro_display_mvt_scaling.py` — green.
- [ ] 4.3 `uv run pytest -q tests/test_hhe_mvt_binding.py tests/test_mvt_tile_generation_lock.py
  tests/test_precip_overlay.py` — green (helper + refactored-validator consumers).
- [ ] 4.4 `openspec validate guard-mvt-instant-range-2033 --strict --no-interactive` — pass.
- [ ] 4.5 `grep -rn "DEBUG-" services/tiles/mvt.py apps/api/routes/hydro_display.py tests/` — clean.
- [ ] 4.6 `git diff --stat origin/master -- apps/api/routes/precip.py` is empty (design D4b: precip is
  proven-unaffected, not modified).

### Non-goal: `tests/test_mvt_national_identity_probe_integration.py`

Issue acceptance criterion 5 names this file. It is an **explicit non-goal** here: that file is the
real-DB `NHMS_RUN_INTEGRATION=1` oracle for the 424 identity-existence probe, and this change's 422
fires before any SQL — so an out-of-range case added there would exercise no probe behavior and would
add no oracle power over task 3.3's `sql=0` assertion. The out-of-range coverage the criterion asks for
lands in `tests/test_hydro_display_mvt_scaling.py` (3.1–3.7) instead.

## 5. node-27 live receipt (orchestrator, isolated worktree, RO role, `TMPDIR=/home/nwm/tmp`)

Method mirrors PR #2164 / #2030 task 5: an **isolated worktree at the PR head**, never the production
checkout (`/home/nwm/NWM` is parked on `hotfix/node27-rollback-pre-2073`, see design D6),
`NHMS_ENABLE_LIVE_POSTGIS_MVT=true`, RO role URL read on the node and never written into the repo.

- [ ] 5.1 All **six** tile routes (five layers, with both the legacy `hydro-national` alias and the
  `{source}/{cycle}` route covered) × one real tile each at a normal in-range instant: tile bytes `md5`,
  ETag, and cache status identical between the master checkout and the PR-head worktree.
- [ ] 5.2 Out-of-range instants against the in-process app on the live RO DB: both legacy routes ×
  both extreme instants → `422` / `VALIDATION_ERROR` / `sql=0`. **Method for `sql=0` on a real session**:
  register a SQLAlchemy `before_cursor_execute` event listener on the live RO engine and count
  invocations across the request; the receipt records the listener snippet and the count, so the claim
  is reproducible rather than asserted.
- [ ] 5.3 Pre-fix public baseline recorded verbatim (measured 2026-09-08 on `https://test.nwm.ac.cn`:
  `not-an-instant` 422, `9999-12-31T23:59:59-08:00` 500, `0001-01-01T00:00:00+08:00` 500).
- [ ] 5.4 Receipt `docs/runbooks/receipts/2026-09-08-issue-2033-mvt-instant-range-node27.md`: method,
  worktree SHA, 5.1–5.3 tables, and the explicit statement that the public-URL post-fix re-measure is
  deferred to #2162's maintenance window (design D6).

## Evidence Floor (issue #2033 acceptance criteria → evidence)

| Criterion | Evidence |
|---|---|
| Both `.astimezone(UTC)` branches no longer let `OverflowError` escape | 1.2, 3.1 |
| Both legacy routes return 422 + `VALIDATION_ERROR` for the two extreme instants | 2.3, 2.4, 3.3, 5.2 |
| The 422 is returned before any SQL (`sql=0`) | 3.3, 5.2 |
| Zero shift for in-range instants (cache key, SQL bind, sub-second on new route still 422) | 3.2, 3.5, 3.6, 3.7, 5.1 |
| Tile contract tests extended with out-of-range cases and green | 3.1–3.11, 4.2, 4.3; `test_mvt_national_identity_probe_integration.py` is a recorded non-goal (§4 note) |
| node-27 live receipt: five layers unchanged; public URL 422 | 5.1, 5.2, 5.4 — public-URL half **deferred to #2162** (design D6) |

## Risk pack → evidence

- Public API / CLI / script entry: 3.3, 3.4, 3.5, 3.9, 3.10, 3.11, 5.2
- Error handling / rollback / partial outputs: 1.2, 3.1, 3.3
- Legacy compatibility / examples: 3.4, 3.6, 3.8, 3.9, 3.10, 3.11, 4.6
- Hydro-met time series / forcing windows: 3.2, 3.7
- Published NHMS artifacts / display identity: 3.7, 5.1
