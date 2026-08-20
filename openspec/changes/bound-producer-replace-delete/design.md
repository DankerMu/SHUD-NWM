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
  (`store.py:994-995`) and asserted by
  `tests/test_timescale_write_guard_wired.py:400-415`.
- **MP2** — when the guard raises, **no** DELETE and **no** INSERT fire and
  the transaction rolls back
  (`tests/test_timescale_write_guard_wired.py:418-436`).
- **MP3** — the AST meta-guard
  `tests/test_timescale_write_guard_wire_site_invariant.py:401-487` stays
  green **unmodified**. Its four assertions: `store.py` must define
  `replace_forcing_timeseries`; that function must contain **exactly one**
  `self._replace_values(...)` call; that call must bind
  `pre_write_cursor_hook=` to an `ast.Name` naming a function defined
  locally inside `replace_forcing_timeseries` (not `None`, not a
  module-level name); and that local hook function must itself call
  `check_batch_targets_uncompressed` (`:483-487`). This test is the
  write-guard's own tamper detector — weakening it to fit the fix is an
  oracle weakening and is forbidden.
- **MP4** — `store.py:322`, `:397`, `:727` (`replace_*` for stations,
  interpolation weights, components) keep their exact current SQL and
  parameters.
- **MP5** — the INSERT statement, `execute_values` template, page size, and
  `expected_insert_count` conflict path are untouched.

## Decisions

### D1 — two shapes satisfy the meta-guard; we choose the honest one

MP3 rules out bypassing `_replace_values` or opening a second connection.
It does **not**, however, force the choice made here. Two shapes pass all
four of its assertions:

- **Shape a (rejected)** — move the whole sequence, *including the bounded
  DELETE*, inside `_guard(cursor)`, and pass `delete_statement=None` to
  `_replace_values` (a `None` delete statement is already an established
  pattern at `store.py:397-401`). This passes MP3 — still one
  `_replace_values` call, still a local hook Name, still a guard call
  inside it — and also passes the co-location invariant
  `_delete_site_has_guard_in_same_function`
  (`tests/test_timescale_write_guard_wire_site_invariant.py:197-233`),
  because `_guard` would contain both the DELETE literal and the guard
  call. Its blast radius on `_replace_values` is zero.
- **Shape b (selected)** — keep the DELETE inside `_replace_values` and
  let the cursor-derived parameters travel to it (D3).

Shape a is rejected on a contract argument, not a mechanical one: it makes
a hook named `pre_write_cursor_hook` perform the destructive write, which
directly contradicts D7's insistence that the hook stays
`Callable[[Any], None]` and read-only. It also dissolves the structural
"guard strictly before any write" property that `_replace_values`
currently enforces for **every** caller (`store.py:994-995`) into
statement ordering inside one private closure. On a fail-closed
write-guard seam that is the wrong direction, and the blast radius it
saves is close to zero anyway — see D3.

**This is a preference argument. Do not restate it as necessity.**

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

Measured blast radius of this keyword: the three other callers do not pass
it and are untouched (MP4). The two test doubles that hand-copy
`_replace_values`'s signature — `tests/test_forcing_producer.py:5805` and
`tests/test_direct_grid_variant_registration.py:1730` — do not accept
`pre_write_cursor_hook` either, so they are only ever driven by the
interp-weight / met-station / component paths, never by
`replace_forcing_timeseries`. A keyword that only
`replace_forcing_timeseries` passes cannot reach them.

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
attaches UTC when naive.

Coercion must be applied to **both** sides, as the reference does
(`forcing_domain_handoff_apply.py:754` for the batch, `:779-780` for the
cursor values). Coercing only the cursor side would be a no-op in
production: `db/migrations/000005_met.sql` declares `valid_time` as
`TIMESTAMPTZ`, so psycopg2 always returns aware datetimes. The side that
can actually be naive is the **batch** — `ForcingTimeseriesRow.valid_time`
is annotated `datetime` with no tz constraint. A naive/aware comparison
raises `TypeError`, so the batch-side coercion is the one carrying real
weight; the cursor-side coercion exists so test doubles may return ISO
strings.

## Expected collateral (not scope creep)

Two distinct effects on `tests/test_timescale_write_guard_wired.py`, and
they point in opposite directions. Getting this backwards is the single
easiest way to mis-deliver this change.

**(1) One assertion MUST be tightened.** `:457` (`:413` before this change) asserts
`delete_calls[0][1] == ("fv_a",)`. Once the DELETE is bounded, that tuple
becomes `("fv_a", batch_min, batch_max)`. This assertion **has to
change**, and the change is positive evidence that the fix landed — it is
not an oracle weakening. An implementer who refuses to touch it can only
satisfy it by leaving the DELETE unbounded.

**(2) No setup repair is needed for the three existing tests.**
`_RecordingCursor.execute` special-cases only the `hydro.river_timeseries`
probes; every other statement falls through to
`self._last_fetchone = None`. For the met-side probes that fall-through
*is* the correct answer — "no existing rows". So `:400` and `:418` keep
passing untouched.

*Post-implementation correction (kept, not silently edited, because the
prediction above was right in outcome but wrong in mechanism).* The
delivered fix adds met-side probe branches to `_RecordingCursor`, and the
handoff path issues **byte-identical** probe SQL
(`forcing_domain_handoff_apply.py:763-777`). Those branches therefore also
intercept the handoff tests — the fall-through is no longer what answers
them. Behaviour is preserved regardless: with `existing_forcing_window`
unset the existence branch evaluates to `None`, exactly what the
fall-through returned, so the handoff tests take `existing = (None, None)`
and never reach the second statement. Equivalence, not non-interception,
is what makes the three existing tests safe.

Setup **extension** is still required, but only for the new tests: a
met-side `existing_forcing_window` knob on `_RecordingConnection` plus the
two matching probe branches in `_RecordingCursor`, mirroring the existing
`existing_river_window` pair. Adding a knob for new tests is not a repair
of the old ones.

The deviation record must state which of these actually happened, and must
flag any assertion change *other* than that one for scrutiny.

## Seams under test

- `PsycopgForcingRepository.replace_forcing_timeseries` driven by the
  existing fake psycopg2 module (`database_url="postgres://unused"`) —
  the established oracle for this wire site.
- `connection.executions` ordering (guard SQL marker → DELETE) for MP1.
- The AST meta-guard reading `store.py`'s source for MP3.

## Non-goals

See `proposal.md` § Non-Goals. Restating the sharpest one: **arm B is
forbidden in this change.**
