# Pin `_lenient_receipt_order` malformed-input fail-safe with regression tests (#1094)

## Why

`_lenient_receipt_order` (`scripts/scheduler_file_provider_refresh.py:1468-1488`)
is the lenient reader guarding the C-A2 promise: a legacy or corrupted on-disk
`latest.json` must never brick the next refresh's primary-receipt publish.
Contract: any parse failure returns `None`, and the caller
(`_publish_primary_receipt`, `:1491-1549`) defaults to `replace_latest = True`
(`:1519-1521`); the same reader is reused for history rotation (`:1535`).

Fail-safe branches, all currently untested
(`grep -rn "_lenient_receipt_order" tests/` → no hits, verified 2026-07-25):
- (a) non-Mapping payload → `None` (`:1479-1480`) — also the landing path for
  wholly-invalid JSON bytes, which `_publish_primary_receipt` catches at
  `:1515-1518` (`UnicodeDecodeError`/`JSONDecodeError` → `existing_payload = None`)
- (b) `run_id` missing / non-str / empty → `None` (`:1481-1483`)
- (c) `started_at` unparsable → `_parse_receipt_datetime` (`:2093-2102`)
  raises `ValueError` → `None` (`:1484-1487`); missing `started_at` follows
  the same path (`None` is not a str → `ValueError`)

The two T9 tests (`tests/test_scheduler_file_provider_refresh.py:3763`,
`:3839`) feed only schema-valid pre-#1080 payloads — the happy "well-formed
but no `registry_classification`" path. If a refactor turns any fail-safe
branch into a raise, the first truncated / half-written / hand-edited
`latest.json` on node-22 crashes the next daily refresh's publish, and full
pytest stays green. This is a test gap; current behavior is correct and is
the contract to pin.

## What Changes

- `tests/test_scheduler_file_provider_refresh.py` only:
  1. One parametrized test calling `refresh._lenient_receipt_order` directly,
     asserting `None` for: non-Mapping (`None`, `"not a mapping"`,
     `[1, 2, 3]`); `run_id` missing / empty-str / non-str (`42`);
     `started_at` unparsable (`"not-a-datetime"`) / missing.
  2. One happy-path test: a valid payload returns `(datetime, run_id)` with
     the datetime tz-aware (`tzinfo is not None`) — pins against a future
     naive-datetime regression.
  3. One end-to-end test: seed `latest.json` with malformed bytes
     (parametrized: `b"{ not json"` → `JSONDecodeError`, and `b"\x80\x81"` →
     `UnicodeDecodeError` — both catch-tuple members at `:1517` pinned), call `refresh._publish_primary_receipt` with a valid
     new receipt, assert publish succeeds and `latest.json` content equals
     the new receipt's canonical bytes.
- MANDATORY mutation-check: locally change branch (a) `return None` at
  `:1480` to `raise ValueError("boom")` → the non-Mapping params AND the
  e2e corrupted-bytes test must go red; restore and re-run green, production
  file byte-identical.

## Out of Scope

- Any change to `_lenient_receipt_order`, `_publish_primary_receipt`, or
  `_parse_receipt_datetime` semantics (fail-safe behavior is correct design).
- Monotonic-order comparison logic and the existing T9 / monotonic tests
  (`:1830`, `:3763`, `:3839`) — untouched, kept as the well-formed oracle.
- Schema / receipt validator / cutover gate.

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement pinning the
  lenient-reader fail-safe).
- Affected code: `tests/test_scheduler_file_provider_refresh.py` only; zero
  production-code change (issue acceptance criterion 4).
- `_lenient_receipt_order` has exactly two callers (`:1519`, `:1535`), both in
  the same file; direct unit coverage benefits both, no sibling copies.
