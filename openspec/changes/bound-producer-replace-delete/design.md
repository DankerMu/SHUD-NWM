# Design — bound-producer-replace-delete

## Risk triage

- Fixture level: **standard** (upstream `Suggested fixture level` absent;
  issue estimates size **S**, arm A, local-only — triage agrees).
- Risk surfaces touched: **data-integrity / destructive-SQL**
  (a DELETE's target set changes), **fail-closed guard contract**
  (the guard's certified window changes), **shared private helper**
  (`_replace_values` has three other callers).
- Risk packs selected: `correctness`, `data-integrity`, `blast-radius`.
- Risk packs not selected: `security` (no authz/secret/input surface),
  `performance` (two extra probe queries per replace on a zero-traffic
  path; the probe shape is chosen *for* plan cost — see D4), `frontend`,
  `migration` (no schema change).

## Must-preserve behaviour

- **MP1** — the guard runs **before** any DELETE. Enforced today by
  `_replace_values` calling `pre_write_cursor_hook` first
  (`store.py:926-932`) and asserted by
  `tests/test_timescale_write_guard_wired.py:358-373`.
- **MP2** — when the guard raises, **no** DELETE and **no** INSERT fire and
  the transaction rolls back
  (`tests/test_timescale_write_guard_wired.py:376-394`).
- **MP3** — the AST meta-guard
  `tests/test_timescale_write_guard_wire_site_invariant.py:404-470` stays
  green **unmodified**: `store.py` must define `replace_forcing_timeseries`;
  it must contain **exactly one** `self._replace_values(...)` call; that
  call must bind `pre_write_cursor_hook=` to an `ast.Name` referring to a
  locally-defined function (not `None`). This test is the write-guard's
  own tamper detector — weakening it to fit the fix is an oracle
  weakening and is forbidden.
- **MP4** — `store.py:311`, `:386`, `:716` (`replace_*` for stations,
  interpolation weights, components) keep their exact current SQL and
  parameters.
- **MP5** — the INSERT statement, `execute_values` template, page size, and
  `expected_insert_count` conflict path are untouched.

## Decisions

### D1 — the meta-guard fixes the shape of the solution

MP3 forbids every design that bypasses `_replace_values` or opens its own
connection: exactly one call, and the cursor reaches user code only through
`pre_write_cursor_hook`. The union window is cursor-derived; the DELETE
runs inside `_replace_values`. Therefore the window **must** travel from
the hook into `_replace_values`, and `_replace_values` must be extended.
This is not one option among several — it is the only shape that satisfies
MP3.

### D2 — one cell, computed once, read by both guard and DELETE

`_guard(cursor)` does the whole job in one place: existence probe, window
read, union with the incoming batch, `check_batch_targets_uncompressed` on
the union, and stash of the resulting DELETE parameters in a cell enclosed
by `replace_forcing_timeseries`.

This is the centrepiece of the fix: **the guarded window and the deleted
window are the same object**, not two expressions that happen to agree.
The bug being fixed is precisely that they were two expressions; a fix that
recomputes the window separately for the DELETE reintroduces the same
failure class in a subtler form.

### D3 — `_replace_values` gains `delete_parameters_factory`

```python
delete_parameters_factory: Callable[[], tuple[Any, ...] | None] | None = None
```

Invoked after `pre_write_cursor_hook` and after `pre_delete_statement`,
immediately before the DELETE:

- factory absent → `delete_parameters` used verbatim (today's behaviour,
  MP4 satisfied byte-for-byte for the other three callers);
- factory returns a tuple → those parameters are used;
- factory returns `None` → **the DELETE is skipped entirely**.

The `None` return is how the empty-window case (no existing rows, empty
batch) skips the DELETE, mirroring the reference's
`if valid_time_min is not None:` at
`forcing_domain_handoff_apply.py:792`.

### D4 — the probe shape is load-bearing; copy it, do not "simplify" it

The reference does an existence probe first, then a
`WITH existing AS MATERIALIZED (...) SELECT min(valid_time), max(valid_time)`
fallback. Both halves exist for plan cost and MUST be reproduced with their
explanatory comment intact
(`forcing_domain_handoff_apply.py:757-777`):

- a bare `min/max ... WHERE forcing_version_id = %s` makes the planner walk
  `valid_time_idx` backward per chunk hunting the first matching row — for a
  **new** forcing version (0 rows) that is a full index scan of every chunk;
- `AS MATERIALIZED` fences that same min/max transform so the window read
  stays on the primary-key prefix and touches only this version's rows.

A future reviewer proposing to collapse these into one query is proposing a
per-chunk scan. Pin the comment so the reason survives.

### D5 — rejected: a self-bounding CTE DELETE

Deriving the bounds inside the DELETE
(`WHERE valid_time >= (SELECT min(...) ...)`) was rejected. Compressed-chunk
exclusion needs the bounds at **plan** time; psycopg2 interpolates `%s`
client-side so bound parameters arrive as literals the planner can prune on,
while a runtime subquery bound cannot be pruned. That lands in the same
failure class as today's unbounded DELETE while *looking* fixed — the worst
possible outcome. The reference's SELECT-then-bind shape is load-bearing,
not stylistic.

### D6 — rejected: extracting a shared union-window helper

The issue's listed alternative. Rejected: it widens the blast radius into
`forcing_domain_handoff_apply.py`, which is on the live production handoff
path, to deduplicate ~30 lines on a path with zero current traffic. Revisit
only if a third copy appears.

### D7 — rejected: the hook returning the parameters

Overloading `pre_write_cursor_hook`'s return value changes the contract
that MP3's meta-guard and the other guard wire sites are written against.
`Callable[[Any], None]` stays `Callable[[Any], None]`.

### D8 — accepted: probe→DELETE race semantics identical to the reference

Between the window read and the DELETE another writer could insert a row
for the same `forcing_version_id` outside the window; that row survives the
replace. This is exactly the semantics `731eb2a7` shipped and accepted on
the live path, both run inside one transaction, and the producer path has
no concurrent writer. Recorded as accepted, not as an open question.

### D9 — value coercion at the boundary

`ForcingTimeseriesRow.valid_time` is a `datetime`
(`workers/forcing_producer/producer.py:281`). Values returned by the cursor
for the existing window must be comparable to it before `min`/`max` — the
reference coerces via `_coerce_valid_time`
(`forcing_domain_handoff_apply.py:733-741`), which parses ISO strings and
attaches UTC when naive. `store.py` must handle the same shapes; test
doubles in `tests/test_timescale_write_guard_wired.py` are free to return
either. A comparison between naive and aware datetimes raises `TypeError`,
so this is a real failure mode, not defensive padding.

## Expected collateral (not scope creep)

The fix inserts two probe queries into the statement sequence, so the
mock-cursor tests at `tests/test_timescale_write_guard_wired.py:358`,
`:376`, `:397` will see a longer `connection.executions` list and their
fake cursors must answer the two new queries. **Setup-only repair there is
legitimate and expected**; changing or relaxing their assertions is an
oracle weakening and is not. Say which happened in the deviation record.

## Seams under test

- `PsycopgForcingRepository.replace_forcing_timeseries` driven by the
  existing fake psycopg2 module (`database_url="postgres://unused"`) —
  the established oracle for this wire site.
- `connection.executions` ordering (guard SQL marker → DELETE) for MP1.
- The AST meta-guard reading `store.py`'s source for MP3.

## Non-goals

See `proposal.md` § Non-Goals. Restating the sharpest one: **arm B is
forbidden in this change.**
