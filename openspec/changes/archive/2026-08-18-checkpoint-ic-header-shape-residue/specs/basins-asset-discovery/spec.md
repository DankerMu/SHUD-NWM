## ADDED Requirements

### Requirement: Unreadable required files degrade registration health observably

The discovery checksum walk SHALL treat a required file that was matched
but cannot be read (stat or hashing fails with an OSError) as a
third-state degradation instead of silently skipping it: the walk SHALL
surface the unreadable files as their own collection (mirroring the
existing invalid-required-files mechanism — the status expression consumes
the collection directly, since quirks alone do not drive status), the
model's status SHALL drop from "valid" to "partial" through that
collection, an observable quirk marking the model as carrying an
unreadable required file SHALL be recorded — with the file named by the
collection entry and the accompanying warning, not by the quirk token
itself — and the discovery payload SHALL carry the collection under its
own key alongside the invalid-required-files key.
The missing-required-files semantics stay unchanged (a matched file is
never reported as missing), the pre-existing unsafe-symlink arm (a resolve
outside the root, which already warns) keeps its own semantics and is not
folded into the OSError arm, and successful checksum entries keep their
existing shape.

#### Scenario: a matched but unreadable required file yields partial status

WHEN a required file matched by discovery raises OSError during stat or
hashing
THEN the model's status is "partial" with an unreadable-required-file
quirk, the file is named in the collection entry and the recorded
warning, and the file does not appear in missing_required_files

#### Scenario: readable required files keep the valid status

WHEN every required file is read and hashed successfully
THEN the status and checksum entries are byte-for-byte identical to the
pre-change behavior
