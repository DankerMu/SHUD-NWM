## ADDED Requirements

### Requirement: Checkpoint IC header consumption is shape-gated

The checkpoint publish and rekey paths SHALL validate the IC header's shape
through the single shared shape helper wherever a minute-index position is
actually about to be consumed: a header that yields a minute-index but an
invalid shape (such as the two-token incident form, where the located
"minute" token is really the mesh-state column count, or a
five-or-more-numeric-token form) SHALL cause the normalization path to
refuse that checkpoint's publication with a structured, machine-greppable
error instead of overwriting the column count with an epoch-minute, and
SHALL cause the header-minute reader to return no minute so the checkpoint
keeps its manifest-declared valid time and lead hours instead of being
rekeyed from a column count. Headers that yield no minute-index at all
(empty, single-token, or non-numeric-tail forms) keep today's tolerant
no-consumption behavior byte-for-byte — the gate never widens the refuse
set to values the code never consumed. Headers with valid native and
compatible shapes keep their existing normalization and rekey behavior
byte-for-byte, and the shape verdict comes only from the shared helper —
no second token-counting rule is introduced.

#### Scenario: two-token header refuses normalization instead of writing a poisoned IC

WHEN the checkpoint normalization path reads an IC header with the
two-token incident shape
THEN no normalized IC file is produced and a StateManagerError with a
greppable reason token is raised

#### Scenario: two-token header does not rekey the checkpoint

WHEN the header-minute reader encounters the two-token incident shape
THEN it returns no minute, the checkpoint's manifest-declared valid time
and lead hours are preserved unchanged, and a stdlib-logging warning
carrying a greppable reason token is recorded

#### Scenario: valid shapes keep their behavior

WHEN the IC header has the native three-token or compatible four-token
shape
THEN normalization, rekey, and the no-op path behave byte-for-byte as
before

#### Scenario: headers without a minute-index keep the tolerant branch

WHEN the IC header yields no minute-index (empty, single-token, or
non-numeric-tail)
THEN the normalization and header-minute paths behave byte-for-byte as
today (no new refusal, no rekey — the tolerant no-consumption branch)
