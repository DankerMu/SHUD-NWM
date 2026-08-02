# hypertable-compression Specification

## Purpose
TBD - created by archiving change cleanup-orphan-execution-audit-validator. Update Purpose after archive.
## Requirements
### Requirement: The compression live-evidence module MUST NOT retain unwired trust-boundary validators

The compression live-evidence validation module SHALL NOT contain
validator functions asserting a trust boundary that no execution path
enforces: when a validation lane is replaced (as `aace0913` replaced the
pgaudit lane with the supervisor-owned execution lane), every validator
orphaned by that replacement is deleted rather than left implying an
audit gate that is not wired. Attestation fields for unwired lanes stay
schema-pinned to their honest value (`authorization.database_audit_proof`
and `execution.database_audit_proof` const `false`) rather than being
"supported" by unreachable code.

#### Scenario: aace0913 orphan validators removed

- **WHEN** the compression live-evidence module's Python sources are
  scanned for the validators orphaned by the pgaudit-lane retirement
- **THEN** `grep -rn --include="*.py"` for `_validate_execution_audit`,
  `_validate_invocation_record`, and `_artifact_refs_in` each return
  zero hits, and the `database_audit_proof` schema pins under
  `authorization` and `execution` remain `{"const": false}`

### Requirement: The G14 write-privilege probe MUST derive its target from the single recovery-target source and fail closed

The compression supervision plane SHALL embed in every
`has_write_privilege_on_target` probe (supervisor checkpoint SQL and
benchmark activity SQL) only a target that was produced by the shared
fail-closed target validator from the single recovery-target constant;
the validator SHALL raise — before any SQL is built — for any target
that is not a member of the supervised-hypertable whitelist or does not
match a strict `schema.table` identifier form. No probe SQL may embed a
target table as an independent inline literal that can drift from the
pinned recovery target.

#### Scenario: Probe target and recovery target share one source

- **WHEN** the supervisor builds its G14 checkpoint activity SQL and its
  expected decompress argv
- **THEN** both derive the hypertable schema and name from the same
  shared constant, and
  `grep -rn "has_table_privilege(usename" scripts/` returns zero hits

#### Scenario: Non-whitelisted target is refused before SQL exists

- **WHEN** the target validator is invoked with a target outside the
  supervised-hypertable whitelist or with a malformed identifier
- **THEN** it raises an error and no probe SQL is produced

#### Scenario: Switching the target moves the probe with it

- **WHEN** an activity-SQL builder is invoked with
  `met.forcing_station_timeseries`
- **THEN** the emitted SQL probes write privilege on
  `met.forcing_station_timeseries` and no longer references
  `hydro.river_timeseries`

### Requirement: The recovery-target six-field contract MUST have a single Python source bound to the schema consts by an automated guard

The recovery target SHALL be defined as one six-field contract
(hypertable schema and name, chunk schema and name, range start and
end) with a single Python source of truth; the supervisor expected
decompress argv and the capture evidence target SHALL derive from that
source, and an automated guard SHALL assert field-by-field equality
between the source and the bound copy set — the schema consts that pin
the same values, the synthetic `decompress_return_relation` const, and
the verifier's recovery-target module constants — so that mutating any
copy in that set alone fails a test instead of shipping a half-migrated
target. Copies that cannot derive from the source directly MAY instead
be covered by a test whose failure is triggered by a one-sided change
(direct equality assertion or a gate test the copy flows through), and
any copy known to remain outside the bound set MUST be named in the
source-of-truth documentation with a tracked follow-up.

#### Scenario: All six schema consts and the verifier oracle are bound to the shared source

- **WHEN** the drift-guard test compares the schema
  `recovery_target` consts, `decompress_return_relation`, and the
  verifier's recovery-target oracle against the shared contract
  constants
- **THEN** all six fields and the synthetic `chunk_schema.chunk_name`
  relation match field-by-field, and changing any one copy alone makes
  a test fail

#### Scenario: Supervisor argv and capture evidence derive from the source

- **WHEN** the supervisor builds its expected decompress argv and the
  capture producer emits its recovery-target evidence and preflight SQL
- **THEN** the chunk schema, chunk name, range start, and range end come
  from the shared constants and their rendered content is unchanged for
  the pinned values

#### Scenario: The capture catalog_post SQL derives from the shared source

- **WHEN** the capture producer renders its catalog_post SQL for the
  pinned recovery target
- **THEN** the six identity fields are interpolated from the derived
  recovery-target mapping, the rendered string is byte-identical to the
  pre-derivation literal, and the `capture:catalog_post` marker remains
  the first token

#### Scenario: The verifier decompress argv tail derives from its own bound constant

- **WHEN** the verifier validates a decompress invocation argv
- **THEN** the expected tail is built from the verifier's own
  recovery-target constant (itself guard-bound to the shared source),
  an argv derived from that constant is accepted, and an argv whose
  tail deviates in any single recovery-target field is rejected

#### Scenario: The replay producer's target derives from the shared source

- **WHEN** the drift-guard test compares the replay producer's module
  `TARGET` mapping and its synthetic `TARGET_RELATION` string against
  the shared contract constants
- **THEN** the six fields and the `chunk_schema.chunk_name` relation
  match field-by-field, the replay producer's own receipt test asserts
  the published target against the contract-derived expectation rather
  than the producer's own constant, and reverting the producer's
  derivation to an independent drifted literal makes a test fail

### Requirement: The schema-dump-list capture MUST refuse a docker CLI that deviates from the pinned host CLI unless an explicit self-test seam is enabled

The schema-dump-list capture SHALL enforce, in code, that the docker
CLI it executes is the same pinned host CLI its recorded forensic
argv attests (it is the capture kind that records docker invocation
argvs into the bundle): when the injected docker executable differs
from the pinned constant and the explicit self-test opt-in flag is
not set, that capture SHALL fail closed before running any subprocess
or emitting any forensic document, and the error SHALL name the
observed docker value. The self-test opt-in remains a hidden
test-only flag; the production plan author SHALL NOT emit it. Capture
kinds that run docker only to record measured container facts (no
argv attestation pair) are outside this requirement.

#### Scenario: Deviating docker without the seam is refused before any bundle write

- **WHEN** the capture producer is invoked for the schema-dump-list
  kind with a docker executable different from the pinned host CLI and
  without the self-test opt-in flag
- **THEN** it exits non-zero with an error naming the observed docker
  value, emits no forensic document on stdout, and leaves the evidence
  directory empty

#### Scenario: Hermetic self-tests keep stub-docker injection via the explicit opt-in

- **WHEN** a hermetic test appends the self-test opt-in flag to the
  schema-dump-list capture argv and injects a stub docker
- **THEN** the capture succeeds, the recorded forensic argv still
  names the pinned host CLI, and the existing verifier literal pins
  accept the document unchanged

#### Scenario: The production default path is unaffected

- **WHEN** the capture producer runs with the docker executable equal
  to the pinned host CLI and no opt-in flag
- **THEN** behavior is unchanged from before the guard

### Requirement: The replay arm MUST have a committed pre-arm reset that archives residue without deleting and fails closed on unsafe conditions

The controlled-replay arm SHALL be preceded by a committed pre-arm
reset script that moves the previous arm's supervisor-owned residue
into a timestamped archive directory — move-only relocation that never
discards evidence content (a cross-device move copies to the archive
before the source is removed by the standard-library fallback) —
keeping only the two files the next arm requires in place (the run
plan and the expected-stale terminal receipt), and SHALL refuse to run
— before moving any file — when the replay unit is not
inactive/failed, when the pinned expected-stale digest is missing or
malformed, when an existing terminal receipt's digest does not match
it, when the failure-intent family is unresolved, when the run plan is
present but unreadable, or when any plan label used for archive naming
is not a single safe path component (escape/traversal refusal). The
residue swept MUST include the stale finalizer state and supervisor
ledger (each would abort the next arm on its exclusive-create
refusal), and the resolved intent-family residue is swept whole, never
partially. A failure in the middle of the sweep MUST surface as the
script's own refusal message and leave a manifest covering what
already moved. When the terminal receipt is absent the sweep proceeds
but the operator MUST be warned that the arm will refuse at the
supervisor's expected-stale gate. The supervisor's own
refuse-to-overwrite trust boundary stays unchanged.

#### Scenario: Residue is archived and the next arm stays viable

- **WHEN** the pre-arm reset runs over a working directory containing
  stale checkpoint artifacts, a stale finalizer state, a stale
  supervisor ledger, and an existing plan-associated schema-dump file,
  alongside the run plan and the expected-stale terminal receipt
- **THEN** the stale artifacts, finalizer state, ledger, and
  schema-dump file are moved — content-intact — into a new timestamped
  archive directory with a manifest, while the run plan and terminal
  receipt remain in place

#### Scenario: Unsafe conditions refuse before any move

- **WHEN** the replay unit reports any state other than inactive or
  failed (including activating), or the existing terminal receipt's
  digest does not match the pinned expected-stale digest, or the
  failure-intent family shows a pending or consuming intent, or the
  run plan exists but is not valid JSON
- **THEN** the pre-arm reset exits non-zero naming the reason and the
  working directory is left byte-identical, with no archive directory
  created

#### Scenario: A mid-sweep failure still leaves a refusal and a forensic record

- **WHEN** a move fails partway through the sweep (for example the
  archive volume runs out of space)
- **THEN** the script exits non-zero with its own refusal message
  rather than a raw traceback, and the archive directory contains a
  manifest recording the pairs that had already moved and the failed
  move

#### Scenario: Re-running is safe and prior archives are preserved

- **WHEN** the pre-arm reset runs again after a previous invocation
  already produced an archive directory
- **THEN** the previous archive directory is not swept into the new
  one and remains intact, and a clean working directory yields a
  successful no-op that still prints the next arm step

### Requirement: A bundle whose run plan carries a self-test seam MUST never verify as PASS

The live-evidence verifier SHALL reject, with its refusal error
naming the offending token, any bundle whose
`execution.run_plan.captures[*].argv` contains a token starting with
the `--self-test-` seam prefix — before any PASS verdict is
reachable — so that "this bundle is production forensics" is a
structural fact of the verifier rather than a convention. The
rejection covers every current and future `--self-test-*` flag by
prefix, and the producer's hidden-flag surface is pinned: every
suppressed capture-CLI flag must itself use the seam prefix.

#### Scenario: Docker-seam bundle is rejected

- **WHEN** a bundle's run-plan capture argv (and its equality-bound
  ledger event) carries `--self-test-docker-seam`
- **THEN** `verify_bundle` raises the verifier's refusal error with
  a message containing `--self-test-docker-seam`, and no PASS
  verdict is produced

#### Scenario: Free-bytes seam bundle is rejected

- **WHEN** a bundle's run-plan capture argv carries
  `--self-test-free-bytes` with an injected value
- **THEN** `verify_bundle` raises the refusal error naming
  `--self-test-free-bytes`, so a fabricated disk-headroom figure
  cannot satisfy the rollback-feasibility gate inside a PASS

#### Scenario: Future hidden flags cannot dodge the prefix

- **WHEN** a new suppressed flag is added to the capture CLI whose
  option string does not start with `--self-test-`
- **THEN** the structural registration test fails, forcing the flag
  onto the rejected prefix before it can become a new invisible seam

#### Scenario: Hermetic self-test coverage survives without a new seam

- **WHEN** the hermetic e2e exercises the real state machine with
  seam-carrying execution argv on CI
- **THEN** the bundle it verifies presents a seam-free production
  plan (seams live only on the execution side, ledger identities
  rewritten by the test's established production-identity pattern,
  with the executed argv asserted to have carried the seams), and
  the verifier gains no acceptance flag or bypass of its own

### Requirement: Run-plan capture argv MUST be anchored to the committed capture producer

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv does not name the committed capture producer — the
production capture script path in argv[1], a `--kind` binding in
argv[2:4] matching the capture's declared kind, and a
`--mutation-head-sha` token pair equal to the run plan's mutation
head SHA — and the supervisor SHALL refuse to validate or execute a
capture whose argv lacks the capture-script suffix or whose `--kind`
binding mismatches, so that "these snapshots were produced by the
committed capture producer" is a structural fact on both the
executor and the forensic verifier. Both anchored options (`--kind`,
`--mutation-head-sha`) SHALL be bound exactly once on the verifier
side (`--kind` also exactly once on the supervisor side), and both
gates SHALL reject any token that is an argparse-acceptable proper
prefix of an anchored option in plain or `=value` form, so a later
last-wins token cannot rebind what the anchor already validated.
The interpreter (argv[0]) is deliberately unpinned: it is an
environment fact (`sys.executable`), not a committed identity.

#### Scenario: A placeholder or rogue producer cannot verify

- **WHEN** a bundle's run-plan capture argv names any executable
  other than the production capture script in argv[1] (e.g.
  `["/usr/bin/printf", "{}"]` or a rogue docker binary)
- **THEN** `verify_bundle` raises the refusal error naming the
  expected producer script, and no PASS verdict is produced

#### Scenario: Capture argv is bound to its kind and mutation SHA

- **WHEN** a capture's argv carries the `--kind` of a different
  capture, omits the `--mutation-head-sha` pair, or carries a SHA
  (in either `--flag value` or `--flag=value` form) differing from
  the run plan's mutation head SHA
- **THEN** the verifier rejects the bundle with an error naming the
  mismatched binding

#### Scenario: The seam gate no longer depends on seam-count collision

- **WHEN** a capture argv token is any argparse-acceptable
  abbreviation of a self-test seam flag (any base token from
  `--s` up to the full `--self-test-` prefix, in plain or
  `=value` form)
- **THEN** the verifier rejects it even if only one seam flag were
  registered in the capture CLI, and a structural test pins that no
  legitimate capture flag ever enters the `--se` rejection domain

#### Scenario: The supervisor refuses a non-producer capture argv

- **WHEN** the supervisor validates a plan (or is asked to execute
  a capture step) whose capture argv[1] does not end with the
  capture-script suffix, whose argv[2:4] `--kind` binding names a
  different kind, or whose argv carries a later rebinding token —
  a second `--kind` or an argparse abbreviation of an anchored
  option
- **THEN** both the validate_run_plan gate and the
  run_capture_step gate refuse with an error naming the violation,
  before any subprocess is spawned (the `--mutation-head-sha`
  VALUE stays unchecked on the supervisor side — the plan SHA
  claim belongs to the verifier; abbreviation rejection there is a
  rebinding defense, not a SHA assertion)

#### Scenario: The supervisor stays hermetic-execution compatible

- **WHEN** the supervisor validates or executes a plan whose capture
  argv[1] is the capture script under a non-production checkout
  (with or without trailing self-test seam tokens)
- **THEN** the suffix-plus-kind anchor accepts it — the
  production-path claim is enforced only by the verifier, and the
  supervisor gains no seam check (the #1250 executor decision
  stands)

