# Make D7#7 factual: manual publisher concurrency is operator-gated, not CAS-gated (#1104)

## Why

Design D7#7 (`openspec/changes/node22-scheduler-registry-refresh/design.md:228-240`, assertion at `:234-240`)
claims concurrent non-refresh writers — explicitly "the manual publisher" —
are "stopped at commit time by the `expected_preimage` CAS in
`atomic_replace_provider_bytes`". The CAS plumbing exists
(`publish_all_basin_scheduler_registry` accepts `expected_preimage` and
forwards it; the provider layer honors it), but the CLI `main()` never
populates it: `grep expected_preimage scripts/publish_scheduler_file_registry.py`
hits only the parameter and its pass-through. In the real concurrency
window — authoritative refresh commits between the CLI's snapshot and
commit — the manual CLI silently overwrites the refresh's canonical bytes
with zero `provider_preimage_changed` evidence. The only mitigation is an
IMPLICIT runbook convention. D7#7 is aspirational: a design/implementation
semantic drift on the #1080 governance surface.

## Decision (route recorded)

Adopt the issue's recommended route: **make the design factual**, not the
implementation aspirational-compliant. (1) Reword D7#7: manual-publisher
concurrency is operator-gated (runbook), not code-gated; the
`expected_preimage` parameter serves the internal refresh runner only.
(2) Upgrade the runbook's implicit convention to an explicit prohibition
with a timer-status check command. (3) The CLI prints a startup stderr
WARNING pointing operators at the gating rule. The alternative (wire CAS
into `main()`) is rejected per the issue's own tradeoff: it needs
snapshot-window handling, an operator retry path, and a larger test face,
while the CLI runs in maintenance windows where operator gating suffices
— revisit only if real concurrent use appears.

## What Changes

- `openspec/changes/node22-scheduler-registry-refresh/design.md` D7#7:
  replace the CAS claim for non-refresh writers with the factual wording
  ("manual publisher concurrency is operator-gated via the runbook; the
  `expected_preimage` CAS parameter is exercised only by the internal
  refresh runner; the CLI does not populate it").
- `docs/runbooks/current-production-ops.md`, TWO places: (a) the
  manual-publisher section (~:613-617) — explicit entry: running the
  manual publisher CLI while `nhms-scheduler-file-provider-refresh.timer`
  OR its oneshot service is active is PROHIBITED, with the paired
  `systemctl --user status <timer> <service> --no-pager` check and
  accept-criteria; (b) §3.1.2 (~:357-359) — the same false claim ("CLI
  shares the expected-preimage check; concurrent writers cannot overwrite
  newer authoritative content") is corrected to the operator-gated fact.
- `openspec/changes/node22-scheduler-registry-refresh/specs/scheduler-registry-refresh/spec.md:5-8`
  (the still-open change's requirement text): remove "manual" from the
  expected-preimage writer list — otherwise archiving that change lands a
  normative clause contradicting this one. Destination-lock serialization
  wording stays (the CLI does take that lock at commit).
- `scripts/publish_scheduler_file_registry.py` `main()`: startup WARNING
  line on stderr after `_parse_args`, before any I/O (mirroring the
  existing `--allow-uncovered-cutover` banner style) telling the operator
  to confirm the refresh timer AND its oneshot service are not active.
  Safe for existing consumers:
  every stderr JSON reader in the test suite parses
  `strip().splitlines()[-1]`.
- Tests: warning presence pinned on both a success run and a failure run;
  failure-run stderr JSON payload still parses from the last line.

## Out of Scope

- Wiring `expected_preimage` into the CLI (rejected alternative — the
  issue keeps it available as a future hardening if usage changes).
- `atomic_replace_provider_bytes` CAS semantics, `refresh_lock`, runner
  gate, cutover audit contract (#1097 family), file splits (#1098/#1100).
- The runner's existing (correct) preimage path.

## Impact

- Affected specs: `scheduler-registry-refresh` — one ADDED requirement
  (CLI startup warning + operator-gated concurrency boundary made
  normative). The D7#7 edit lives in the still-open
  `node22-scheduler-registry-refresh` change's design.md (wording fix in
  place; that change must stay `openspec validate --strict` green).
- Affected code: `scripts/publish_scheduler_file_registry.py` (one
  stderr line), `tests/test_publish_scheduler_file_registry.py`,
  `docs/runbooks/current-production-ops.md` (two sections),
  `openspec/changes/node22-scheduler-registry-refresh/design.md` (D7#7, D2
  heading, invariant matrix) + `.../specs/scheduler-registry-refresh/spec.md`
  + `.../tasks.md` (writer-list / preimage-population fixes).
- No receipt/schema/DB surface. No behavior change beyond the added
  warning line.
