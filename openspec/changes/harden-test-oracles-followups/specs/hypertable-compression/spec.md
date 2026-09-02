## MODIFIED Requirements

### Requirement: Every replace-path DELETE on a compression-capable hypertable MUST be bounded by the window its guard certified

A writer that replaces a lineage's rows in a hypertable eligible for compression SHALL NOT issue a DELETE without a `valid_time` bound, and the window it passes to `check_batch_targets_uncompressed` SHALL be the same window the DELETE targets — the union of the rows already stored for that
lineage and the rows in the incoming batch. A guard window narrower than
the DELETE's target set certifies rows it never inspected and makes the
fail-closed contract hollow; an unbounded DELETE is rejected outright by
TimescaleDB once any chunk of the hypertable is compressed, even when zero
rows match.

The `valid_time` bound SHALL be enforced structurally, not only by the
per-writer unit tests: the wire-site invariant
(`tests/test_timescale_write_guard_wire_site_invariant.py`) SHALL require
that every `DELETE FROM <schema>.<table>` string literal passed as a call
argument and targeting a `HYPERTABLES_GUARDED` pair carries, in the same
literal, both a lower bound spelled `valid_time >=` and an upper bound
spelled `valid_time <=`. Those two spellings are the whole admitted set: a
half-open upper bound (`valid_time <`) is NOT admitted, because the guard's
certified window is closed on both ends and a half-open DELETE targets a
different window than the one certified. A writer that needs another
spelling SHALL widen the admitted set in the same change with a recorded
reason. The predicate SHALL be pinned on synthetic literals so that the
unbounded, lower-only, upper-only and half-open forms are each shown to
fail independently of the repository's current writers.

#### Scenario: Existing rows outside the incoming batch widen both windows

- **WHEN** `workers/forcing_producer/store.py::replace_forcing_timeseries`
  runs for a `forcing_version_id` whose stored rows extend beyond the
  incoming batch's `valid_time` range
- **THEN** `check_batch_targets_uncompressed` receives the union of the
  stored and incoming ranges, not the incoming range alone
- **AND** the emitted DELETE carries `valid_time >= %s AND valid_time <= %s`
  bound to that same union
- **AND** the guard's SQL is executed before the DELETE

#### Scenario: An empty batch with stored rows still purges within the stored window

- **WHEN** the same replace runs with an empty incoming batch for a
  `forcing_version_id` that has stored rows
- **THEN** the guard receives the stored rows' `valid_time` range
- **AND** a DELETE bounded to that range is executed, preserving the
  replace path's existing purge semantics
- **AND** no INSERT is executed

#### Scenario: No stored rows and an empty batch skip the DELETE

- **WHEN** the same replace runs for a `forcing_version_id` with no stored
  rows and an empty incoming batch
- **THEN** no DELETE statement is executed at all

#### Scenario: A guard refusal still precedes every write

- **WHEN** the guard reports a compressed chunk that lies inside the union
  window but outside the incoming batch's own range — the case that
  previously returned PASS while the unbounded DELETE still targeted it
- **THEN** `CompressedChunkWriteError` is raised, no DELETE and no INSERT
  are executed, and the transaction is rolled back

#### Scenario: The write-guard wire-site invariant remains intact

- **WHEN** `tests/test_timescale_write_guard_wire_site_invariant.py`
  inspects `workers/forcing_producer/store.py`
- **THEN** `replace_forcing_timeseries` is still defined, still makes
  exactly one `self._replace_values(...)` call, and still binds
  `pre_write_cursor_hook=` to a locally-defined function

#### Scenario: An unbounded DELETE on a guarded hypertable fails the wire-site invariant

- **WHEN** a production module under the scan roots issues `DELETE FROM
  <schema>.<table>` for a `HYPERTABLES_GUARDED` pair whose literal lacks
  `valid_time >=`, lacks `valid_time <=`, or spells the upper bound as
  `valid_time <` — even when the same function also calls
  `check_batch_targets_uncompressed`
- **THEN** the wire-site invariant fails and names the module, the enclosing
  function and the missing bound
- **AND** the same predicate, applied to synthetic literals in each of those
  three shapes, fails, while the bounded form with newline, indentation and
  parameter-style variations passes

#### Scenario: The three existing bounded writers pass the window predicate

- **WHEN** the predicate is applied to every DELETE hit of the repository
  scan at the current writers (`workers/output_parser/parser.py`,
  `workers/forcing_producer/store.py`,
  `packages/common/forcing_domain_handoff_apply.py`)
- **THEN** every hit passes without any allowlist entry
