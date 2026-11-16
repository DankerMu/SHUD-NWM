# retire-test-only-legacy-helpers (#3148)

## Why

The round-3 entropy audit (2026-11-16) found production symbols that no production code calls. Only tests reference
them. Most of those tests exercise the helpers themselves, so those helpers survive only because their own tests pin
them. A few are used instead by shared fakes to drive live code.
Many look like leftovers from retired topology and routes, such as the DB-backed job persistence path (the node-22
scheduler is now db-free and uses the file journal) and the station routes retired by #2773 item 4. Neither origin
is assumed: each row is decided on its own evidence.

Keeping them alive misleads readers and reviewers, keeps tests running against code that never ships, and blurs which
store surface is authoritative. #3147 (merged via #3152) already removed `_query_comment_accounting`, the only caller of
`_parse_comment_sacct_rows`. That helper is descoped to #3155, together with `safe_fs.prove_named_entry_absent_durable`,
because both files exceed the large-file guard (see tasks.md, Deviations).

## What

Each row in #3148 gets one of two verdicts, and the evidence is recorded in the PR:
- **retire**: delete the symbol and the tests whose only subject is that symbol, when its production purpose is gone;
- **keep**: leave it, with a one-line reason, when it is required by a protocol or ABC, listed on a governed
  compatibility surface, named by a requirement in `openspec/specs/**`, used by a shared test harness or fake to
  drive a live production protocol, or is a deliberate public or safety API or an operator tool.

Some modules stay imported by production even after their helpers go. For those (`persistence.py`, `retry.py`,
`reservation.py`) only the unused functions are deleted. Whole modules stay unless they become empty.

No production call path changes.

## Non-goals

- Zero-reference code (#3147, done).
- Retired route and template branches (#3149).
- Moving compatibility surfaces. A name listed by `services/orchestrator/chain_compat_static.py`, or a facade
  inventory, is kept unless its governed removal condition is met; if it is met, the inventory is updated in the same
  PR.

## Triage

Issue type: tech-debt cleanup.
Fixture level: expanded. The triggers are legacy compatibility (repository and store method surfaces, some listed in
`chain_compat_static.py`), public API (store and protocol methods in `packages/common`), and test removal.
Upstream suggested level: absent.
Blast radius: deleting a method that a Protocol, ABC or compatibility guard requires breaks conformance or the guard
test. Deleting a helper reached dynamically breaks production at runtime. Dropping a test that also covered a live
behaviour loses coverage.
Selected risk packs: Legacy compatibility; Public API / CLI / script entry; Error handling (store failure paths); Concurrency (shared reservation fake); Documentation.
Evidence floor: `uv run ruff check .`; the selector-chosen pytest set, plus the compatibility guard tests and the
directly affected suites (local and node-27 temp worktree); an import smoke of touched modules; the hard gate.
