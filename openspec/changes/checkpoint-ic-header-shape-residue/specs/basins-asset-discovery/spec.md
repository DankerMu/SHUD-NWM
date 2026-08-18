## ADDED Requirements

### Requirement: Unreadable required files degrade registration health observably

The discovery checksum walk SHALL treat a required file that was matched
but cannot be read (stat or hashing fails with an OSError) as a
third-state degradation instead of silently skipping it: the model's
status SHALL drop from "valid" to "partial", an observable quirk naming
the unreadable file SHALL be recorded alongside a warning, and the
missing-required-files semantics SHALL stay unchanged (a matched file is
never reported as missing). Successful checksum entries keep their
existing shape.

#### Scenario: a matched but unreadable required file yields partial status

WHEN a required file matched by discovery raises OSError during stat or
hashing
THEN the model's status is "partial" with a quirk naming the unreadable
file, a warning is recorded, and the file does not appear in
missing_required_files

#### Scenario: readable required files keep the valid status

WHEN every required file is read and hashed successfully
THEN the status and checksum entries are byte-for-byte identical to the
pre-change behavior
