## ADDED Requirements

### Requirement: Released identity-blocked discovery SHALL be bounded by cycle size, not by retained journal history

The operator-facing enumeration of released identity-blocked reservation rows SHALL
remain usable on a journal of unbounded retained history.

The existing recoverability requirement already obliges the operator to be able to
enumerate rows in that shape. That obligation is unsatisfiable if enumeration reads
the whole tree under a single aggregate record budget, because the journal's
append-only history grows without bound and eventually — and then permanently —
exceeds any fixed cap. A discovery surface that fails closed on a real production
journal has no verified path from its intended invoker to its effect.

Enumeration SHALL therefore derive candidate cycle scopes first and read per scope,
so the record cost of any single read is bounded by one cycle's rows rather than by
all retained history.

**Coverage SHALL be structural, not observational.** The surface enumerated SHALL
be one that every row in the governed shape is guaranteed to occupy by construction
of the write path, and that no retention or pruning path removes while the row is
still retained. Enumerating a surface that merely happens to hold today's rows would
trade a fail-closed error for a silently short listing, which is strictly worse for a
tool whose purpose is that the operator can find the row.

**Scope derivation SHALL fall open, never short.** Where a candidate's scope cannot
be derived, discovery SHALL widen — deriving from row content where the identifier
does not parse, and falling back to the unscoped read where content also yields no
scope. A row whose scope is underivable SHALL cost the older, more expensive read;
it SHALL NOT be omitted from the result.

#### Scenario: discovery survives a budget the whole-tree read exceeds

- **WHEN** the journal holds more records in total than the configured record budget,
  while no single cycle's records exceed that budget, and one released
  identity-blocked row is present
- **THEN** the enumeration SHALL return that row
- **AND** SHALL NOT raise `file_journal_record_limit_exceeded`

#### Scenario: a row whose identifier does not parse is still discovered

- **WHEN** a row in the governed shape is persisted with an identifier from which no
  cycle scope can be parsed, but whose stored content does yield a source and cycle
- **THEN** the enumeration SHALL derive the scope from the row's content
- **AND** SHALL return that row

#### Scenario: an underivable scope widens the read instead of dropping the row

- **WHEN** neither the identifier nor the stored content of a candidate yields a
  cycle scope
- **THEN** the enumeration SHALL fall back to the unscoped read
- **AND** SHALL NOT report the absence of a row it did not look for

#### Scenario: the enumeration result is a point-in-time snapshot

- **WHEN** rows are written to the journal while an enumeration is in progress
- **THEN** the enumeration SHALL remain safe to act on: the recovery action SHALL
  re-read and compare under the write lock, so a stale listing SHALL cost at most a
  refused invocation and SHALL NOT permit a write against a superseded attempt
