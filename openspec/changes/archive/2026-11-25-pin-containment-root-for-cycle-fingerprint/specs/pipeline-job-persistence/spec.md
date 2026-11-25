## ADDED Requirements

### Requirement: The cycle-rows fingerprint SHALL open its containment root once per computation without changing its value

One computation of the cycle-rows source fingerprint SHALL resolve the
journal root's no-follow chain once and stat every source path from that
handle. The fingerprint SHALL still be computed on every read outside a
write window, SHALL still route every stat through the containment-aware
signature helper, one call per path, and SHALL have the same value, or the
same containment-fault marker, as a computation that opens the root chain
for every path. No handle SHALL survive the computation.

#### Scenario: equal value on every tree

- **WHEN** the fingerprint of a cycle is computed for an empty root, a
  populated cycle under one and under both source spellings, a cycle with
  rollover segments, and a cycle with one surface absent
- **THEN** it equals the fingerprint computed with the root chain opened per
  path

#### Scenario: a containment fault is still the marker

- **WHEN** a surface directory, a segment slot, the latest directory, the
  `pipeline-jobs` root, or the journal root itself is a symlink
- **THEN** the fingerprint is the containment-fault marker, which is neither
  a hit nor stored

#### Scenario: the per-path seams are unchanged

- **WHEN** one fingerprint is computed
- **THEN** the signature helper and the module's `stat_no_follow` are each
  called once per source path with the arguments they receive today, and the
  root chain is opened once

#### Scenario: a swap after a stored read fails loud

- **WHEN** a read has stored rows and a surface directory is then replaced
  by a symlink
- **THEN** the next read raises what a cold repository raises on that tree
