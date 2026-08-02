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

### Requirement: Run-plan capture argv tool-path values MUST match the committed production tooling

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv binds `--psql`, `--systemctl`, `--docker`,
`--journalctl`, `--git`, `--repo` or `--container` to anything other
than the committed production value (the `/usr/bin/*` host tools,
the production repo path, the production container name), or binds
`--database` to a value other than the run plan's validated
database — each binding present exactly once — and SHALL reject any
token that is an argparse-acceptable proper prefix of a pinned
option, so that an identity-anchored argv cannot point the committed
producer at substitute tooling. `--evidence-dir` is bound relationally
to the capture's own output path (see the residual-shapes
requirement); the `--schema-dump-*` options stay deliberately
unpinned (legitimately parameterized data paths); the supervisor
stays value-unpinned on all tool options (the executor legitimately
runs hermetic plans with stub tools; the forensic claim is
verifier-owned).

#### Scenario: Stub tooling cannot verify

- **WHEN** a bundle's run-plan capture argv binds any pinned tool
  option to a non-production value (e.g. `--psql /tmp/stub-psql`),
  omits a pinned option, or binds it more than once (in either
  `--flag value` or `--flag=value` spelling)
- **THEN** `verify_bundle` refuses with an error naming the option,
  the observed bindings and the expected value, and no PASS verdict
  is produced

#### Scenario: A pinned option cannot be rebound by abbreviation

- **WHEN** a capture argv carries a token whose base is a proper
  prefix of any pinned option (e.g. `--ps`, `--do`, `--rep=/x`)
- **THEN** the verifier rejects the argv even though the full-name
  exactly-once binding looks clean, closing the last-wins rebinding
  class for the tool options the same way it is closed for the
  identity anchor

#### Scenario: The hermetic e2e still executes stub tools and verifies PASS

- **WHEN** the end-to-end test runs the real state machine with
  stub tools under a test checkout
- **THEN** the recorded plan carries production tool values while
  only the executed plan variant carries the stub paths, the ledger
  maps executed argv back to the recorded argv, and the bundle
  still verifies PASS — hermetic execution needs no gate weakening

### Requirement: Run-plan capture argv MUST be free of help early-exit tokens and MUST bind `--evidence-dir` relationally to its own output path

The live-evidence verifier SHALL reject any bundle whose run-plan capture
argv carries an argparse help early-exit token — `-h`, any single-dash
short-option cluster beginning with `-h` (e.g. `-hx`, `-hh`, which
argparse resolves to the same auto help action because `-h` is the only
registered single-dash flag), `--help` (either spelling, including
`--help=x`), or any unambiguous abbreviation of
`--help` (`--h`, `--he`, `--hel`) — because such an argv makes the
recorded producer exit before collecting anything (the bare spellings
print help and exit 0; the `--help=x` spelling is an argparse usage
error exiting 2 — every member of the family is non-production),
falsifying the forensic claim the identity anchor makes. The verifier
SHALL further require each capture argv to bind `--evidence-dir` exactly
once to the value derived from that capture's own verifier-bound
`output_path` (the directory containing `output_path`, suffixed
`/capture-artifacts` — the textual inverse of the production plan
author's same-root derivation), so the `os.statvfs` free-space
measurement feeding the `MIN_FREE_BYTES` hard gate is anchored to the
same volume the capture outputs claim, and SHALL reject
argparse-acceptable proper prefixes of `--evidence-dir` the same way it
does for every other value-pinned option. The argv[0] interpreter token
stays deliberately unpinned, with the residual trust root (argv[0] plus
the repo checkout) and its producer-side closure route recorded in the
verifier; no argv[0] gate is added.

#### Scenario: A help token cannot verify

- **WHEN** a run-plan capture argv that satisfies every identity anchor
  and value pin additionally carries `-h`, a `-h`-prefixed cluster form
  such as `-hx` or `-hh`, `--help`, `--help=x`, `--h`, `--he`, or
  `--hel` in any position
- **THEN** `verify_bundle` refuses with an error naming the offending
  token and the help early-exit refusal class, and no PASS verdict is
  produced

#### Scenario: `--evidence-dir` must be the output-path sibling

- **WHEN** a capture argv omits `--evidence-dir`, binds it more than
  once, binds it dangling, or binds it to any directory other than the
  one derived from that capture's `output_path`
- **THEN** the evidence-dir gate refuses with an error naming the
  option, the observed bindings and the derived expected value

#### Scenario: `--evidence-dir` cannot be rebound by abbreviation

- **WHEN** a capture argv carries a proper-prefix token such as `--ev`
  or `--e` that would rebind `--evidence-dir` last-wins
- **THEN** the existing pinned-option abbreviation branch refuses with
  its established wording (naming the token and the option it
  abbreviates) — the evidence-dir equality gate itself never fires on
  such a token, and the two refusal messages stay distinct

#### Scenario: Relational, not absolute — production and hermetic plans both pass

- **WHEN** a plan binds each capture's `--evidence-dir` to the
  `capture-artifacts` sibling of that capture's own `output_path` —
  whether the root is the production evidence root or a hermetic test
  tmp directory (the plan author derives both fields from the same
  `--root`, so all plan-author-authored plans satisfy the relation)
- **THEN** the bundle verifies exactly as before; no run-varying literal
  is pinned and the twelve-kind plan-author positive control stays green

#### Scenario: Structural premises are pinned against the real parser

- **WHEN** the capture CLI parser is inspected
- **THEN** tests pin that no registered business flag starts with `--h`
  (so rejecting the `--help` prefix family collides with nothing), that
  `--evidence-dir` is the only registered `--e*` flag, and that the
  existing anchored/pinned zero-collision premises still hold with the
  widened tuple

### Requirement: The plan author MUST reject non-canonical repo and root paths at authoring time

`plan_author.build_run_plan` SHALL reject any `repo`, `root` or
`schema_dump_host` value that is not Path-normalization-stable (the
value must equal its own `str(Path(value))` rendering), or that ends
in a slash, or that contains a `..` component — refusing trailing
slashes, interior duplicate slashes, `.` segments, `..` segments,
and the bare slash-roots `/` and `//` (the only normalization-stable
strings that end in a slash) with a `PlanAuthorError` that names the
label, the offending value and its canonical rendering. Rationale,
two layers: (1) every path derived from `repo` or `root`, and the
`schema_dump_host` data path recorded verbatim into the pg_dump
command's artifact associations, is recorded canonical
byte-for-byte, so the verifier's verbatim plan-side comparisons
(capture `output_path` equality and command artifact-association
equality) can never falsely refuse a legitimately authored bundle
whose ledger-side counterparts arrive Path-normalized; (2) `..`
segments, though Path-normalization-stable and textually symmetric
on both sides, are refused by the no-follow filesystem primitives
(`safe_fs` rejects any `..` component) behind both the supervisor's
writes/inspections and the verifier's artifact reads — for a `..`
host dump path the abort comes at the supervisor's produced-artifact
inspection the moment pg_dump exits, before any ledger ref exists —
so a `..` value would author fine and then abort inside the one-shot
replay window with an unrelated message, the exact failure mode this
requirement eliminates. The verifier itself stays verbatim: it
judges the recorded bytes and invents no normalization; the closure
lives entirely at the producer entrance. Known recorded residuals
outside this guard: `capture_repo` (hermetic-only kwarg,
value-pinned by the verifier) and `--schema-dump-container`
(deliberately not canonicality-guarded, on symmetry grounds alone:
it never enters artifact associations — its command records none —
and every comparison over it is textual with zero normalization on
either side: the verifier's prefix/shape argv gates, the
whole-capture-argv exact-equality gate over the schema-dump-list
capture that also carries it, and the supervisor's mirror gate and
verbatim argv-tail extraction — so the verbatim-vs-normalized false
refusal this requirement exists to eliminate cannot occur for it; a
pinned adjudication test keeps this ruling executable).

#### Scenario: A non-canonical root fails at authoring, not at the forensic gate or mid-run

- **WHEN** `build_run_plan` is called with `root`, `repo` or
  `schema_dump_host` that is not Path-normalization-stable (trailing
  slash, interior `//`, `/./`), is a bare slash-root (`/`, `//`), or
  contains a `..` component — while a LEADING double slash as in
  `//x` stays accepted: POSIX preserves exactly two leading slashes,
  it is normalization-stable, expands symmetrically on both verifier
  sides, and its parts survive the no-follow walkers
- **THEN** it raises `PlanAuthorError` naming the label and the
  canonical rendering, and no plan is produced — eliminating both the
  authored-but-never-verifiable middle state ("supervisor capture
  output path differs" / "supervisor observed artifact path differs
  from run plan output") and the authored-but-aborts-mid-window
  state ("Unsafe path component: '..'")

#### Scenario: Canonical inputs and the module defaults are unaffected

- **WHEN** `build_run_plan` is called with canonical absolute paths
  (including the module's own
  `DEFAULT_ROOT`/`DEFAULT_REPO`/`DEFAULT_SCHEMA_DUMP_HOST`, whose
  canonicality a structural test pins)
- **THEN** authoring succeeds exactly as before, a custom canonical
  `schema_dump_host` is recorded verbatim into the pg_dump artifact
  association (the guard refuses, never rewrites), and the
  twelve-kind positive control stays green

#### Scenario: The verifier's verbatim textual posture is preserved

- **WHEN** a hand-crafted plan carries double-slash capture spellings
  (a shape the production author can no longer emit)
- **THEN** the relational `--evidence-dir` gate still round-trips the
  spelling textually (a normalizing derivation refactor still reddens
  the guard test), and the refusal that ends such a bundle remains the
  pre-existing verbatim ledger↔plan equality — the verifier gains no
  normalization anywhere

#### Scenario: The container dump path stays outside the guard by recorded adjudication

- **WHEN** `build_run_plan` is called with a `schema_dump_container`
  carrying an interior double slash that still satisfies the
  verifier's prefix gate (e.g. `/var/lib/postgresql//evidence/…`)
- **THEN** authoring succeeds and the value lands verbatim as the
  pg_restore list argv's final element — the pinned executable form
  of the ruling that every comparison over this path is
  verbatim-symmetric (verifier and supervisor gates alike), so the
  false-refusal disease cannot reach it; a future change that guards
  it must consciously flip this scenario

