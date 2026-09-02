# Spec Delta: runtime-evidence-and-operations

## ADDED Requirements

### Requirement: The DB-free scheduler SHALL verify its journal root as a chain of real directories at construction and name the remedy when it is not

The db-free scheduler SHALL verify the configured journal root
(`NHMS_SCHEDULER_JOURNAL_ROOT`) through the same per-component no-follow walk
the journal's hardened readers use, at the moment the file journal repository
is constructed for the scheduler, before any journal byte is read. A root any
of whose path components is a symlink, is not a directory, or does not exist,
or that is blank or not absolute after tilde expansion (a relative value
would otherwise be anchored on the process working directory and verify a
directory the operator never named), SHALL be refused with the typed error `FILE_JOURNAL_INVALID_ROOT` whose
message states that every component of the configured journal root must be
a real directory and names the remedy (configure the realpath, as
`readlink -f` reports it); the message SHALL carry no filesystem path, no
traceback and no module name, and the configured value, the name of the
setting the caller read it from (`NHMS_SCHEDULER_JOURNAL_ROOT` for the
scheduler) and either the underlying error type or a stable token naming
the invalid shape (for a blank/relative or an unexpandable root) SHALL ride in
the error's structured details. The
verified root SHALL be the tilde-expanded, un-resolved configured path, so a
root that already is a realpath verifies to itself and the repository reads
the exact configured location. The scheduler command SHALL surface the
refusal as `<code>: <message>` on standard error with a non-zero exit, on
every command entrypoint it offers. The operator lane that already verifies
a journal root before acting (reserved-job demotion) SHALL use this same
verification seam, so there is one message and one error code for an invalid
root across the scheduler and its operator tooling. The db-free preflight's
own path adjudication SHALL be unchanged by this requirement — it remains the
lane that adjudicates absence, writability and containment — and the
constraint SHALL be documented at every place the repository's environment
examples set `NHMS_SCHEDULER_JOURNAL_ROOT` and in the production operations
runbook, together with the symptom an unverified root produced before this
requirement (preflight passing while every journal read reports a blocked row
with a diagnostic that does not mention a symlink).

#### Scenario: A symlinked ancestor passes preflight but is refused at construction

- **WHEN** `NHMS_SCHEDULER_JOURNAL_ROOT` names a directory that exists and is
  writable, reached through a path one of whose ancestor components is a
  symlink (for example the platform's `/var` alias of `/private/var`)
- **THEN** the db-free required-path check reports no blocker for it
- **THEN** constructing the scheduler from the environment raises
  `FILE_JOURNAL_INVALID_ROOT`, and the scheduler command exits non-zero with
  exactly `FILE_JOURNAL_INVALID_ROOT: <message>` on standard error, the
  message naming `readlink -f` and the real-directory rule, the details
  naming `NHMS_SCHEDULER_JOURNAL_ROOT`, with no traceback
- **THEN** no byte is written under either the alias or its target

#### Scenario: A realpath root at production depth constructs

- **WHEN** the configured root is a chain of real directories of the depth
  the production deployment uses (six components and more)
- **THEN** the scheduler constructs, and its journal repository's root is the
  verified configured path

#### Scenario: A root that is itself a symlink or a symlink loop is refused with the same code

- **WHEN** the final component of the configured root is a symlink, or the
  root is part of a symlink loop
- **THEN** construction fails with `FILE_JOURNAL_INVALID_ROOT` and the
  structured details carry the underlying error type

#### Scenario: A blank or relative root is refused rather than anchored on the working directory

- **WHEN** the configured root is the empty string, `.`, or any other
  non-absolute value after tilde expansion
- **THEN** the seam refuses it with `FILE_JOURNAL_INVALID_ROOT` before any
  directory is opened, on the scheduler lane, the operator demotion lane and
  the read-only census alike, so no lane reports on the process working
  directory in place of the configured root

#### Scenario: The operator demotion lane shares the seam

- **WHEN** the reserved-job demotion command is given an invalid journal root
- **THEN** it fails with the same error code and the same message the
  scheduler construction uses, its details naming `--journal-root` as the
  setting, still without a traceback or module name on standard error, and
  still with zero journal bytes written
