# Tasks: fix-retention-freed-bytes-compressed

Fixture level: expanded
Upstream suggested level: none declared (issue #1125 predates the template;
filed from #1072 Step C live-enforce evidence). Expanded triggers genuinely
hit: production script in the `delete`-executing retention path, `writer`
(receipt payload), `field` semantics (`freed_bytes`) — no downgrade.

Change surface:
- `scripts/node27_timeseries_retention.py` — `_default_measure_chunk_bytes`
  query swap (design D1) + header H4 note + docstring; nothing else in the
  script.
- `tests/test_node27_timeseries_retention.py` — new default-path measurement
  tests (design D4).
- `openspec/changes/tier-node27-timeseries-storage/design.md:1903-1904` —
  design #855 H4 back-fill (measurement path naming + recorded sibling
  divergence, design D1/D3). Runbook `:1870` and design `:1964` are NOT
  stale (they state only the ordering, which stays true) — no edit gated.
- `tests/test_node27_timeseries_retention.py:2258` region — the B1
  isolation test's fake-cursor SQL matcher (`"pg_total_relation_size" in
  sql`) MUST be updated to match the new query; its isolation semantics,
  5-chunk shape, and byte-value assertions stay unchanged (it is the #855
  Class C `InFailedSqlTransaction` regression guard — weakening it is an
  oracle violation).
- `docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-retention/README.md`
  — known-limitation section gains the resolution note.

Must preserve:
- H4 ordering: measurement strictly BEFORE drop; existing mock-ordering test
  untouched and green.
- Per-chunk isolated connection + per-chunk failure → 0 → continue
  (best-effort receipt; drop proceeds); 60 s statement timeout.
- H5 whole-tick fail-closed on drop failure; H7 predicate; per-tick bound;
  gate/deferral/salvage-window logic — zero diff outside
  `_default_measure_chunk_bytes` and comments/docs.
- Receipt schema byte-identical (`schemas/timeseries_retention_receipt.schema.json`).
- Wire codes byte-identical (H6).
- The historical 2026-07-25 receipt JSON — immutable, never edited.
- Injection seams `fetch_chunks` / `measure_chunk_bytes` / `drop_chunk`
  signatures unchanged (test stubs elsewhere in the suite depend on them).

Must add/change:
- Query: `SELECT total_bytes FROM chunks_detailed_size(%s::regclass) WHERE
  chunk_schema = %s AND chunk_name = %s` with params
  `(schema.table, chunk_schema, chunk_name)`; result coercion keeps the
  `int((row[0] if row else 0) or 0)` shape (zero-rows/NULL → 0).
- Docs synced per design D3 list.

Seams under test:
- Existing injection seam `measure_chunk_bytes` (upstream-declared, consumed
  not renegotiated) — used by mock-ordering tests.
- Default-path seam: fake psycopg2 via `sys.modules` (in-suite precedent
  from compression sibling tests) to drive `_default_measure_chunk_bytes`
  directly. No new production seam introduced.

Risk packs:
- File IO / path safety / overwrite / delete: selected — the function under
  edit feeds the receipt of a chunk-DROPPING script. Evidence: zero-diff
  assertion outside the measurement function (task 2.6); H4/H5 tests green;
  drop loop untouched.
- Schema / columns / fields / units: selected — `freed_bytes` semantics
  change from "main relation bytes" to "total incl. compressed sibling".
  Receipt schema shape unchanged (integer >= 0); the semantic is what the
  issue demands. Evidence: schema zero-diff (2.6) + compressed-fixture unit
  row (2.2).
- Public API / CLI / script entry: not selected — no flag/env/exit-code
  change; internal measurement query only.
- Error handling / rollback / partial outputs: selected — per-chunk
  failure/empty-result semantics must stay best-effort-0. Evidence: unit
  rows for exception edge + zero-rows edge (2.2).
- Backward compatibility / legacy: not selected — receipt consumers read
  `freed_bytes: int >= 0`; larger accurate values break no reader
  (schema-validated); historical receipts untouched.
- Config / project setup: not selected — no config change.
- Concurrency / ordering: selected — H4 "measure BEFORE drop" is the
  flagship ordering invariant of this function. Evidence: existing
  mock-ordering test (`tests/test_node27_timeseries_retention.py:1712`,
  injection seam, unaffected by the query swap) stays untouched and green
  (2.2).
- Resource limits: selected — per-call cost rises from O(1 relation) to
  O(hypertable chunks) because `chunks_detailed_size` computes
  hypertable-wide before the filter; live instance has 8 chunks per
  hypertable, milliseconds vs the 60 s statement timeout (design D1
  records the magnitude). Evidence: design analysis; no timeout change.
- Other packs (auth/secrets, release/packaging, documentation/migration):
  not selected — none touched beyond the doc syncs listed in the change
  surface (documentation sync is itself a task with a lint gate, 2.4).

Non-goals:
- No live node-27 enforce as merge evidence (design D5/Non-goals: drops are
  irreversible; unit oracle + recorded live discrepancy suffice; next
  scheduled tick verifies opportunistically).
- No receipt-schema field additions (e.g. per-chunk breakdown) — out of
  issue scope.
- No changes to the compression sibling script or its measurement — it is
  already compression-aware by construction (its `after=True` branch
  deliberately sizes the compressed sibling relation,
  `scripts/node27_timeseries_compression.py:328-337`); it never had this
  defect. Only retention's "total reclaimed" semantics were wrong.
- #856/#845 cascade behavior untouched.

## 1. Implementation

- [x] 1.1 Swap the measurement query in `_default_measure_chunk_bytes` to
  the D1 `chunks_detailed_size` form; sync header H4 note + docstring.
- [x] 1.2 Add unit tests per design D4 (compressed fixture w/ SQL+params
  assertion, zero-rows → 0, exception → 0 + continue; red-first proof
  recorded).
- [x] 1.3 Sync design #855 H4 entry (:1903-1904) and receipts README
  resolution note.

## 2. Verification (evidence mapping)

- [x] 2.1 Red-first: query-shape/params unit assertion fails against the
  current `pg_total_relation_size` implementation (stash-or-precommit run
  recorded), passes after.
- [x] 2.2 `uv run pytest -q tests/test_node27_timeseries_retention.py` —
  all green incl. new rows (compressed-inclusive value, zero-rows edge,
  exception edge) and untouched H4 mock-ordering row.
- [x] 2.3 `uv run ruff check .` clean.
- [x] 2.4 `openspec validate fix-retention-freed-bytes-compressed --strict
  --no-interactive` passes; markdownlint clean on the two touched
  runbook/README docs (repo markdown-lint gate covers `docs/**`).
- [x] 2.5 Adjacent suites green: `uv run pytest -q
  tests/test_node27_archive_rebuild_drill.py` (consumes retention module's
  `_overlaps`/receipt shapes) — no regression.
- [x] 2.6 Zero-diff assertions: `git diff origin/master --
  schemas/timeseries_retention_receipt.schema.json` empty; script diff
  confined to `_default_measure_chunk_bytes` + comments (reviewer-checkable
  hunk list); historical receipt JSON untouched.
- [x] 2.7 Doc-pin consistency (stale surfaces only): script header +
  docstring, design #855 `:1903-1904`, and the receipts README resolution
  note all name `chunks_detailed_size` after the change (grep proof);
  runbook `:1870` / design `:1964` intentionally not gated (not stale).
- [x] 2.8 Live read-only API oracle (node-27 primary, TimescaleDB 2.10.2,
  SELECT-only, 2026-08-01): `chunks_detailed_size('hydro.river_timeseries'::regclass)`
  returns `chunk_schema, chunk_name, table_bytes, total_bytes`; join
  against `timescaledb_information.chunks` shows every compressed chunk's
  main relation at exactly 57,344 B (the incident signature) vs
  `total_bytes` 134 MB–5.9 GB — query shape, column names, and
  compression-inclusiveness all verified on the target instance before
  implementation (probe output recorded in design D1).
