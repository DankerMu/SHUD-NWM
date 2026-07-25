# Tasks: pin-lenient-receipt-order-failsafe

Fixture level: compact (tests-only, single file, no production-code change;
issue is implementation-ready with enumerated branches and minimal inputs)
Repair intensity: normal

Risk packs considered (core):
- Public API / CLI / script entry: not selected - no runtime surface touched
- Error handling / rollback / partial outputs: selected - the artifact under
  test IS the fail-safe contract; tests must pin None-not-raise on every
  malformed shape
- Legacy compatibility / examples: selected - existing T9 + monotonic tests
  must pass unmodified; they stay the well-formed oracle
- Test-oracle integrity: selected - new tests must be mutation-capable, not
  tautological; e2e must prove the caller path, not a mock
- File IO / path safety / overwrite: not selected - only test-side seeding of
  a corrupt latest.json into tmp_path; production write path untouched

## 1. Regression tests

- [x] 1.1 Parametrized direct test of `refresh._lenient_receipt_order` in
  `tests/test_scheduler_file_provider_refresh.py`, asserting `is None` for:
  (a) `None`, `"not a mapping"`, `[1, 2, 3]` (non-Mapping);
  (b) `{"started_at": "2026-07-01T00:00:00Z"}` (run_id missing),
  `{"run_id": "", "started_at": "2026-07-01T00:00:00Z"}` (empty),
  `{"run_id": 42, "started_at": "2026-07-01T00:00:00Z"}` (non-str);
  (c) `{"run_id": "refresh_x", "started_at": "not-a-datetime"}` (unparsable),
  `{"run_id": "refresh_x"}` (started_at missing).
  Evidence floor: all params green on head; issue acceptance criterion 1
  (≥4 cases across a/b/c/d-missing) satisfied.
- [x] 1.2 Happy-path pin: valid payload
  (e.g. `{"run_id": "refresh_x", "started_at": "2026-07-01T00:00:00Z"}`)
  returns a `(datetime, run_id)` tuple, `run_id` equal to input, datetime
  tz-aware (`tzinfo is not None` and equals the parsed instant in UTC).
  Evidence floor: green on head (issue acceptance criterion 3).
- [x] 1.3 End-to-end fail-safe test, parametrized over corrupt seeds
  `[b"{ not json", b"\x80\x81"]` (the first triggers `JSONDecodeError`, the
  second `UnicodeDecodeError` — pins BOTH members of the catch tuple at
  `scripts/scheduler_file_provider_refresh.py:1517`): build a valid receipt
  via the file's existing helpers (mirror
  `test_publish_primary_receipt_upgrades_over_pre_1080_latest` at `:3763`),
  seed `root / "latest.json"` with the corrupt bytes, call
  `refresh._publish_primary_receipt(root, receipt)`, assert:
  publish does not raise; `latest.json` bytes now equal
  `refresh._receipt_bytes(refresh._validate_receipt(receipt))`; AND an
  implementation-independent check `json.loads((root / "latest.json").read_text())
  == receipt` (mirrors the monotonic test's style at `:1853`).
  Evidence floor: both params green on head (issue acceptance criterion 2).
- [x] 1.4 MANDATORY mutation-check: locally change the non-Mapping branch
  `return None` (`scripts/scheduler_file_provider_refresh.py:1480` — the
  line INSIDE the `if not isinstance(payload, Mapping):` guard at `:1479`,
  NOT `:1481` which is the `run_id` line) to
  `raise ValueError("boom")` → the (a) params of 1.1 AND the 1.3 e2e test
  must go red; all other new tests survive. Restore the original line,
  re-run green, and verify
  `git diff --stat -- scripts/scheduler_file_provider_refresh.py` empty.
  Record red and green outputs verbatim in the report/PR body.

## 2. Change-level verification floor

- [x] 2.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
  green (full suite; T9 tests `:3763`/`:3839` and monotonic test `:1830`
  unmodified).
- [x] 2.2 `uv run ruff check .` clean.
- [x] 2.3 `openspec validate pin-lenient-receipt-order-failsafe --strict
  --no-interactive` PASS.
- [x] 2.4 Zero production-code change (issue acceptance criterion 4):
  `git diff --name-only origin/master...HEAD -- scripts/ packages/ apps/
  services/ workers/` is empty; outside `openspec/` the only changed file is
  `tests/test_scheduler_file_provider_refresh.py`.
