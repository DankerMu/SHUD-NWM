# Spec Delta: forecast-api

## ADDED Requirements

### Requirement: A segment-pinned forecast-series read MUST reach its rows through an index that binds the segment

A forecast-series read that is pinned to one river segment SHALL have that segment's identity applied as
an index access condition on every fact-table node it plans, and MUST NOT reach those rows through an
index that can only apply the segment as a post-index filter. Which column carries that identity is a
property of the physical store the node reads: a store organised on surrogate keys binds the segment
key, while a store still organised on text identity binds the segment id. What is required is that the
segment is bound, not that a particular column is.

This holds regardless of the table statistics available to the planner. A read that satisfies the
requirement only after the chunk it touches has been analysed does not satisfy it: the chunk at the
write frontier is the one a live read is most likely to touch, and its statistics drift by construction.

The requirement applies to each predicate shape the read path issues, including the shape that binds a
single resolved run and the shape that binds a set of resolved runs, because those shapes are planned
independently and can select different access paths on different chunks.

A read whose purpose is to discover which runs, cycles or valid times exist is not segment-pinned and is
outside this requirement.

#### Scenario: A segment-pinned read on a chunk with no statistics

- **GIVEN** a narrow-store chunk that has never been analysed
- **AND** a forecast-series read pinned to one river segment and one resolved run
- **WHEN** the read's fact-table statement is planned
- **THEN** the node reading that chunk applies the segment key as an index access condition
- **AND** the node's rows removed by filter, divided by its returned rows, is within the plan gate's
  filter-ratio bound

#### Scenario: A segment-pinned read that binds a set of resolved runs

- **GIVEN** a narrow-store chunk whose statistics are absent or stale
- **AND** a forecast-series read pinned to one river segment whose run identity was resolved into a set
- **WHEN** the read's fact-table statement is planned
- **THEN** the node reading that chunk applies the segment key as an index access condition

#### Scenario: Identity predicates are preserved, not traded away

- **GIVEN** the narrow-store segment read constrains the basin version and the river network version
- **WHEN** the read is changed so that the segment key reaches the index access condition
- **THEN** both constraints are still enforced by the read
- **AND** the oracle that asserts key predicates are retained is updated deliberately if their spelling
  or position changed, rather than being bypassed

#### Scenario: Reads that legitimately carry no segment are unaffected

- **GIVEN** a display read that discovers source identities, valid times or per-run coverage without
  pinning a segment
- **WHEN** this change is applied
- **THEN** that read's measured plan and cost do not regress
