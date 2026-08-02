# Spec Delta: hypertable-compression (capture producer identity)

## ADDED Requirements

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
