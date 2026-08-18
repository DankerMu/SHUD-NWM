## ADDED Requirements

### Requirement: Checkpoint IC header consumption is shape-gated

The checkpoint publish and rekey paths SHALL validate the IC header's shape
through the single shared shape helper before consuming the minute-index
position: a header whose shape is invalid (such as the two-token incident
form where the last numeric token is the mesh-state column count, not a
minute-time) SHALL cause the normalization path to refuse publication with
a structured, machine-greppable error instead of overwriting the column
count with an epoch-minute, and SHALL cause the header-minute reader to
return no minute so the checkpoint keeps its manifest-declared valid time
and lead hours instead of being rekeyed from a column count. Headers with
valid native and compatible shapes keep their existing normalization and
rekey behavior byte-for-byte, and the shape verdict comes only from the
shared helper — no second token-counting rule is introduced.

#### Scenario: two-token header refuses normalization instead of writing a poisoned IC

WHEN the checkpoint normalization path reads an IC header with the
two-token incident shape
THEN no normalized IC file is produced and a StateManagerError with a
greppable reason token is raised

#### Scenario: two-token header does not rekey the checkpoint

WHEN the header-minute reader encounters the two-token incident shape
THEN it returns no minute, the checkpoint's manifest-declared valid time
and lead hours are preserved unchanged, and an observable warning is
recorded

#### Scenario: valid shapes keep their behavior

WHEN the IC header has the native three-token or compatible four-token
shape
THEN normalization, rekey, and the no-op path behave byte-for-byte as
before
