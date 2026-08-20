## ADDED Requirements

### Requirement: Direct-grid station CSV staging SHALL treat a prior attempt's own residue as replaceable while still refusing anything this attempt staged

Staging SHALL remove only station CSV residue that predates this attempt's
staging, and SHALL keep failing closed when a file this same attempt just
staged — a model package member, a forcing package member, or an initial
state — already occupies a declared station CSV target path. A retried SHUD
attempt reuses the same deterministic run workspace, so its own previous
output is not a collision; a file the current attempt produced is. Removal is
no-follow and contained within the model input directory, and a removal
failure SHALL abort the attempt with the existing residue-cleanup error code
rather than continuing to stage. Two rows declaring the same filename in one
row set remain a refusal, not a last-write-wins overwrite.

#### Scenario: a second attempt on the same run workspace stages successfully

WHEN the same manifest is staged twice into the same run workspace, so the
station CSV targets from the first attempt are still present
THEN the second staging succeeds and every staged station CSV holds the
content produced by the current staging pass

#### Scenario: a file staged by this same attempt still fails closed

WHEN the model package staged earlier in this same attempt carries a member
whose name equals a declared station CSV target
THEN staging fails with the direct-grid station filename collision error and
the already-staged copy of that member inside the model input directory is
left byte-for-byte unchanged

#### Scenario: duplicate declarations in one row set are refused

WHEN the forcing station row set declares the same filename twice
THEN staging fails with the direct-grid station filename collision error
rather than silently letting the second copy overwrite the first

#### Scenario: residue deletion failure aborts loudly

WHEN removing a prior attempt's station CSV fails
THEN the attempt terminates with the existing direct-grid residue cleanup
error code, staging does not continue, and no partially staged station CSV
set is left behind
