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
either side: the verifier's containment/shape argv gates, the
whole-capture-argv exact-equality gate over the schema-dump-list
capture that also carries it, and the supervisor's mirror gate,
pre-spawn capture-argv gate and verbatim argv-tail extraction — so
the verbatim-vs-normalized false
refusal this requirement exists to eliminate cannot occur for it; a
pinned adjudication test keeps this ruling executable). That
authoring ruling is orthogonal to prefix containment: the five
consuming gates additionally refuse `..` components under the
requirement "The container dump path gates MUST refuse `..`
traversal before any container side effect" — including the
`schema_dump_list` capture argv's `--schema-dump-container` value,
now judged pre-spawn for containment — a containment judgment at
the gates, still textual, still rewriting nothing — so the
symmetry rationale and the adjudication stand unchanged.

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
  verifier's containment gate (e.g. `/var/lib/postgresql//evidence/…`)
- **THEN** authoring succeeds and the value lands verbatim as the
  pg_restore list argv's final element — the pinned executable form
  of the ruling that every comparison over this path is
  verbatim-symmetric (verifier and supervisor gates alike), so the
  false-refusal disease cannot reach it; a future change that guards
  it must consciously flip this scenario

### Requirement: The container dump path gates MUST refuse `..` traversal before any container side effect

The five container dump path gates SHALL judge containment in the
pinned container dump path prefix with one shared predicate — the gates being the verifier's
pg_restore list argv gate and its captured schema-dump-list listing
gate, the supervisor's mirror argv gate and
`resolve_container_pg_restore_identity`, and the supervisor's
pre-spawn capture-argv gate (`_assert_capture_producer_argv`, which
for a declared `schema_dump_list` kind judges every value bound to
`--schema-dump-container` in either argparse form, and whose
anchored-option tuple — mirrored across both planes under a pinned
cross-plane equality — gains `--schema-dump-container` so
abbreviation spellings cannot smuggle the binding past the
exact-base scan on either side); the
predicate being exported by the lane's cross-plane contract module
(`packages.common.node27_container_contract`):
the value must start with `/var/lib/postgresql/` AND contain no `..`
component (`PurePosixPath(value).parts`). What that prefix is, stated
only as far as a cited command determines it — this requirement
exists to stop gates from overclaiming, and the record about the
prefix has to hold itself to the same rule: `docker inspect nhms-db`
(read-only, node-27) shows the host bind mount
`/home/nwm/nhms-evidence` landing at the prefix's `evidence` subtree
(RW) and the DB's own data directory living at a different mount
entirely, so the host-writable region is a subtree of the prefix; the
requirement asserts nothing further about the parent directory. The
four pre-existing refusal messages keep their bytes, including the
phrase "DB container data mount", which denotes this prefix. String
prefix alone is not containment: `/var/lib/postgresql/../../../etc/shadow` satisfies the
prefix yet normalizes to `/etc/shadow`, and before this requirement it
passed every gate, after which the supervisor really executed
`docker exec <container> /usr/bin/sha256sum` against it and recorded
the digest as `dump_sha256` in the forensic bundle — while on the
capture-argv route the spawned capture producer really executed
`docker exec pg_restore --list` against it. The predicate
judges and never rewrites: admitted values keep being recorded and
compared as the plan's original strings — no normalization anywhere,
preserving the verbatim forensic posture. Each gate refuses on its
own; no gate may delegate refusal to an upstream gate.
`resolve_container_pg_restore_identity` SHALL refuse before spawning
any container probe, so its "pg_restore dump path is outside the DB
container data mount" message states a property the check actually
enforces; the pre-spawn capture gate SHALL refuse before the capture
producer process exists. The four pre-existing refusal messages stay
spelled as they are; the pre-spawn value refusal is a new message and
SHALL name the pinned prefix the predicate actually enforces rather
than inherit the older phrasing. An automated source scan SHALL back the
predicate's single-source status by refusing the old inline
mount-prefix spellings in either gate module; the load-bearing
guarantee remains the per-gate behavioural refusal tests, since a
source scan can only refuse the spellings it enumerates.

#### Scenario: A traversal path is refused at every gate independently

- **WHEN** a run plan or bundle carries
  `/var/lib/postgresql/../../../etc/shadow` or
  `/var/lib/postgresql/evidence/../../../../etc/passwd` as the
  pg_restore list argv tail
- **THEN** the verifier's argv gate and captured-listing gate each
  raise `EvidenceError`, and the supervisor's mirror gate and identity
  resolver each raise `SupervisorError`, each proved by a test that
  reaches that gate directly with hand-crafted input rather than
  relying on an upstream refusal

#### Scenario: Identity resolution refuses with zero container side effects

- **WHEN** `resolve_container_pg_restore_identity` receives a
  traversal path
- **THEN** it raises the containment refusal without invoking
  `docker` at all — proved hermetically by a stub arrangement in which
  any docker invocation would surface as a distinguishably different
  failure

#### Scenario: The capture-argv route is refused before the capture producer spawns

- **WHEN** a plan's `schema_dump_list` capture argv binds
  `--schema-dump-container` to a traversal value — in either argparse
  form, via an abbreviation spelling such as `--schema-dump-c=…`, as
  a dangling flag, or as a late second binding after a clean first
  one
- **THEN** the supervisor's pre-spawn capture gate raises
  `SupervisorError` before any capture process exists (so the
  in-container `docker exec pg_restore --list` on the escaped path
  never runs), while an absent option and the committed capture argv
  shapes stay admitted

#### Scenario: In-prefix values, including the interior-double-slash shape, stay admitted verbatim

- **WHEN** the container dump path is the default
  `/var/lib/postgresql/evidence/schema-before.dump` or an in-prefix
  value with an interior double slash
  (`/var/lib/postgresql//evidence/…`)
- **THEN** every gate admits it, recorded and compared values stay
  byte-identical to the plan spelling, and both the existing
  identity-resolution positive control and the recorded authoring
  adjudication for `schema_dump_container` stay green unchanged

#### Scenario: The containment predicate has a single source

- **WHEN** the two gate modules are scanned for the inline pattern
  `startswith("/var/lib/postgresql/")`
- **THEN** no gate site carries its own copy — all five call the
  shared predicate, and the automated drift guard fails if a future
  edit reintroduces an inline prefix check

### Requirement: Contract-module measured records cite repo-resolvable determining sources

Measured records in the lane's cross-plane contract module SHALL cite
repo-resolvable artifacts whose recorded commands determine each claim
the record states — a `Source:` that resolves to nothing in the
repository (such as a gitignored `.workplans/` path), an event cited
in place of a command, or a claim broader than what the cited command
can determine (an exec-behavior claim on a `readlink` citation, an
"always" on a single snapshot) disqualifies the record; a clause
nothing determines is deleted or narrowed to the citation's coverage,
never re-asserted; and a narrative contradicted by a committed
measured snapshot is replaced by the snapshot's fact. Constant values
themselves stay under the snapshot drift lock and are not evidence
for prose claims about live behavior.

#### Scenario: Dangling source is replaced by a committed artifact

- **WHEN** a measured record's `Source:` points at an artifact that
  resolves nowhere in the repository
- **THEN** the record cites a committed artifact instead (such as the
  external-contract snapshot fixture entry with its
  `_provenance.command` and date), and tracked production code
  contains no `.workplans/` references

#### Scenario: A claim beyond its command is narrowed or deleted

- **WHEN** a record's clause asserts something its cited command
  cannot determine — exec behavior from a path-resolution command, a
  universal from a single snapshot, a version with no citation
- **THEN** the clause is deleted or narrowed to exactly the cited
  command's coverage; design rulings that are not measurements are
  stated as the plane's decision, carrying no measurement authority

#### Scenario: A falsified narrative yields to the committed snapshot

- **WHEN** a record's narrative about live behavior is contradicted
  by a committed measured snapshot entry
- **THEN** the record states the snapshot's measured fact (citing the
  entry) and cross-references the tracked issue for any runtime
  consequence, instead of retaining the falsified narrative

### Requirement: Compression runner timeout budget chain MUST be operator-configurable and fail closed

The node-27 timeseries compression runner SHALL derive its per-chunk `statement_timeout`, its wrapper wall, and its declared systemd wall from operator-configurable environment variables sourced from the single compression env file, with defaults of 3600000 ms (per-chunk statement timeout), 3900 s (wrapper wall), and 3940 s (declared systemd wall) — recalibrated by #1352 from the former hardcoded 840000 ms / 900 s / 940 s against measured steady-state chunk compression rates — and SHALL reject any configuration that violates either leg of the budget-chain invariant — per-chunk timeout in seconds (rounded up) plus the fixed cleanup margin must not exceed the wrapper wall, and the wrapper wall plus the fixed kill-after margin must not exceed the declared systemd wall — before opening any database connection. The invariant bounds a single chunk's budget, not a whole tick.

#### Scenario: defaults unchanged

WHEN none of the timeout environment variables is set, or any of them is set to the empty string
THEN the runner uses 3600000 ms as the per-chunk statement timeout, 3900 s as the wrapper wall, and 3940 s as the declared systemd wall
AND runtime behavior and the receipt schema are identical to the no-override configuration.

#### Scenario: override propagates to the database session

WHEN `NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS` is set to a valid value satisfying the budget-chain invariant
THEN every `compress_chunk` call session issues `SET statement_timeout` with exactly the overridden value.

#### Scenario: budget-chain violation is rejected before any DB call

WHEN the configured per-chunk timeout plus the cleanup margin exceeds the configured wrapper wall, or the wrapper wall plus the kill-after margin exceeds the declared systemd wall, or any of the three variables fails positive-integer validation
THEN the runner raises a fail-closed configuration error naming the violated invariant
AND no database connection is attempted.

#### Scenario: wrapper wall guard is fail-closed

WHEN the wrapper wall environment variable is present, non-empty, and not a positive integer
THEN the shell wrapper exits non-zero with a structured error before executing the runner
AND when the variable is absent the wrapper uses the 3900 s default.

### Requirement: Mutation-window checkpoints MUST gate the recurring unit on current-activity facts, never on boot history

The supervisor checkpoint and its live-evidence verifier counterpart SHALL judge the recurring compression unit (`nhms-node27-timeseries-compression.service`) safe for a mutation window using only current-activity and identity facts — fragment path, `ActiveState`, `SubState`, and `MainPID` — with both planes applying an identical predicate. Fields that record boot history (`ExecMainStartTimestamp`, `ExecMainStartTimestampMonotonic`, and `InvocationID`, which systemd retains on a loaded unit after it returns to inactive — measured on node-27 2026-08-14) SHALL remain captured in the checkpoint evidence document but SHALL NOT participate in the gating decision. Predicate failures SHALL name the diverging fields; a `SubState=failed` unit SHALL produce a distinct message naming `reset-failed` as the remedy, and no failure text SHALL describe boot history as concurrent activity.

#### Scenario: unit ran earlier this boot and is now inactive

- **WHEN** the recurring unit reports `ActiveState=inactive`, `SubState=dead`, `MainPID=0`, the pinned fragment path, and boot-history fields retained from an earlier timer tick (non-unset timestamps, non-empty `InvocationID`)
- **THEN** both the supervisor checkpoint and the live-evidence verifier SHALL pass the recurring-unit gate, and the evidence document SHALL still carry all three boot-history fields verbatim

#### Scenario: unit is currently active, failed, or identity-drifted

- **WHEN** the recurring unit reports `ActiveState` other than `inactive`, or `SubState` other than `dead`, or a non-zero `MainPID`, or a diverging fragment path
- **THEN** both planes SHALL fail closed with an error that names the diverging field(s), and the `SubState=failed` case SHALL name `reset-failed` as the operator remedy

#### Scenario: evidence document omits boot-history fields

- **WHEN** a checkpoint show document lacks `ExecMainStartTimestamp`, `ExecMainStartTimestampMonotonic`, or `InvocationID`, or carries them with wrong types
- **THEN** the live-evidence verifier SHALL reject the document as malformed evidence

### Requirement: The compression per-tick bound MUST be a capacity-derived target consistent across template, live env, and receipts

The per-tick bound SHALL be a decided capacity target derived from
measured inputs (steady-state terminal-chunk arrival rate, the
retention-window backlog ceiling, and the relation between observed
per-chunk compression duration and the wrapper wall — the wall bounds
the WHOLE tick, so the bound is a throughput ceiling, not a redeemable
single-tick capacity; catch-up under backlog follows the runbook's
catch-up recipe rather than relying on the bound), not an arbitrary
default or an unrecorded live retune: the
committed env template SHALL carry the target value with a comment
identifying it as a capacity conclusion and pointing at the recorded
derivation in the operator runbook, the deployed node-27 env SHALL carry
the same value, and every receipt echoes the effective bound via its
existing `per_tick_bound` field. The variable remains mandatory with no
in-code default.

#### Scenario: Template carries the pinned capacity target

- **WHEN** `infra/env/node27-timeseries-compression.example` is read
- **THEN** it SHALL contain the uncommented line
  `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4` with a
  capacity-conclusion comment, and an enforced test SHALL pin that exact
  assignment line so silent drift back to a stale value fails CI

#### Scenario: Runbook records the dual-constraint derivation, not just the number

- **WHEN** the operator runbook's per-tick capacity section is read
- **THEN** it SHALL state BOTH capacity constraints — the throughput
  relation (bound × daily tick cadence versus steady-state chunk
  arrival) AND the wrapper-wall relation (the summed duration of
  selected chunks must fit the whole-tick wall, cross-referencing the
  catch-up recipe for backlog scenarios) — plus the measured inputs
  behind the current target, the derivation's invalidation conditions,
  and an explicit conclusion on timer cadence (no frequency change
  required: terminal-chunk count is time-partitioned and insensitive to
  ingest volume) so the next retune starts from the formula instead of
  incident-scene guesswork

#### Scenario: Live bound matches the target and is receipt-proven

- **WHEN** the deployed node-27 env and runner receipts are inspected
- **THEN** the env SHALL set the same bound value as the template, a
  receipt of any mode SHALL echo `per_tick_bound` equal to that value,
  and an enforce-mode receipt SHALL prove the clean outcome under that
  bound (the dry-run outcome field is constant and carries no signal)

### Requirement: Compression receipts MUST record the effective timeout/wall budget chain

压缩 receipt（schema_version "2.1" 起）MUST 携带 `budget` 对象
`{compress_timeout_ms, wrapper_wall_seconds, systemd_wall_seconds}`，数值等于本次运行
`CompressionConfig` 实际生效值，三字段 all-or-nothing。唯一合法缺省形态是
provenance-unavailable config tombstone（`outcome == "failed"` 且
`failure.stage == "config"` 且 `per_tick_bound` 缺失——结构性 config-absence 双判别）
——该路径上从未存在合法 config，禁止补发任何预算值。
schema_version "1.0"/"2.0" 的 receipt 禁止携带 `budget`；schema_version "2.1" 的非
failed receipt 仍 MUST 携带 `head_sha`（既有 provenance 钉随版本放宽同步保留）。
消费侧（live-evidence）双冻结契约保持硬编码：`EXPECTED_TIMEOUT_SECONDS = 900` 与
`verify_bundle` 对 #1069 冻结 bundle 的 `schema_version == "2.0"` 语义钉，
均禁止改为跟随新字段/新版本。

#### Scenario: 非默认预算如实落 receipt

- **WHEN** operator 以非默认预算运行（如 1800000 ms / 1900 s / 1940 s，bound=1）
- **THEN** 当次 receipt `budget` 三字段逐一等于该非默认值，`schema_version == "2.1"`，
  与默认预算 receipt 字节可区分

#### Scenario: 半截 budget 被 schema 拒绝

- **WHEN** receipt 携带只含一或两个字段的 `budget` 对象
- **THEN** schema 校验失败（all-or-nothing 由 `budget` 定义的 required 全列 +
  additionalProperties:false 强制）

#### Scenario: config tombstone 是唯一合法缺省

- **WHEN** `config_from_args` 抛错且存在 stale receipt，early tombstone 被写出
- **THEN** 该 receipt `schema_version == "2.1"`、无 `budget`，schema 校验通过；
  任何其它 2.1 形状缺 `budget` 均校验失败

#### Scenario: 历史 receipt 保持可验证

- **WHEN** live-evidence 用更新后的 schema 校验历史 1.0/2.0 receipt（无 budget）
- **THEN** 校验通过；同版本 receipt 若被注入 `budget` 则校验失败

### Requirement: Raising the compress timeout above default MUST fail closed unless per_tick_bound is 1

`config_from_args` MUST 在 `compress_timeout_ms > 默认值（3600000 ms）` 且
`per_tick_bound > 1` 时抛 `CompressionConfigError`（pre-connect，零 DB 调用），
错误文案指向 runbook §4.5 的追赶配方（抬墙必须 `PER_TICK_BOUND=1`）。
等于或低于默认值的 timeout 不触发本约束。本约束**只守 §4.5 追赶窗口的显式抬墙操作**；
默认 timeout 下 bound=4 遇 ≥2 river chunk 的撞墙险 config 时刻不可见（chunk 尺寸未知），
仍按 runbook §4 的 operator 检测权威处置——不得宣称本约束覆盖该残差。

#### Scenario: 抬墙未降 bound 被拒

- **WHEN** env 设 `COMPRESS_TIMEOUT_MS=7200000`、`WRAPPER_WALL_SECONDS=7500`、
  `SYSTEMD_WALL_SECONDS=7540`（外层两墙同步抬高——否则既有 leg 1 先拒）且 `PER_TICK_BOUND=4`
- **THEN** runner 在任何 DB 连接前以 `CompressionConfigError`（leg 3）退出，文案含 §4.5 指针

#### Scenario: 合法追赶组合与默认组合不受影响

- **WHEN** env 设抬 timeout + 外层两墙按 §4.5 配方同步抬高 + `PER_TICK_BOUND=1`
  （如 7200000/7500/7540/1），或默认 timeout + `PER_TICK_BOUND=4`
- **THEN** config 构造成功，既有 budget-chain 两腿不变量行为不变

### Requirement: Run-plan capture argv MUST parse as the closed production grammar

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv, beyond the anchored `argv[0:2]` (interpreter and
committed producer script), does not consume left-to-right as a
sequence of pairs — a registered production capture flag in its exact
full spelling (in `--flag value` or `--flag=value` form) followed by
its value — against a closed-world flag set restated as literals
inside the verifier (the module's non-derived-oracle posture, pinned
against the real capture CLI parser by a structural test rather than
imported from it). A bare argparse `--` separator token SHALL be
rejected position-independently — flag position or value position
alike — with its own refusal wording naming the token and its argv
index, and this check SHALL run before the option-value equality
gates (a value-position `--` that survived to a stop-at-`--` scanner
would blind the exactly-once tool-value pins to bindings after it — a
net regression the ordering forbids). Any other token at a flag
position — a single-dash cluster such as `-xh`, an unregistered long
option such as `--evidence-dirx`, or a dangling unpaired flag — SHALL
be rejected with an unregistered-token wording, and a value token
beginning with `-` SHALL be rejected with a value-position wording
(closing the exit-2 family's last survivors on the deliberately
value-unpinned `--schema-dump-*` options); all wordings SHALL name
the offending token and stay distinct from the established
seam/help/abbreviation refusal wordings, which fire before the pair
grammar and are not subsumed. The verifier's option-value scanner
SHALL stop scanning at the first bare `--` token — a
definition-consistency measure (one meaning of "binding" across the
grammar gate, the equality gates and the real parser), not a
load-bearing defense, since the pre-posed `--` rejection fires first.
The supervisor gains no grammar gate (hermetic execution
compatibility stands), and the capture CLI parser itself is
unchanged.

#### Scenario: An unparsable argv cannot back a PASS

- **WHEN** a run-plan capture argv satisfies every identity anchor,
  exactly-once binding, tool-value pin and per-token family scan, but
  additionally carries `-xh` or `--evidence-dirx`
- **THEN** `verify_bundle` refuses with the unregistered-token wording
  naming that token, and no PASS verdict is produced

#### Scenario: The `--` separator family is refused, both shapes

- **WHEN** a capture argv carries a trailing `-- /tmp/whatever`, or
  carries its entire correct `--evidence-dir <expected>` pair moved
  after a `--` separator
- **THEN** the pre-posed position-independent `--` check refuses both
  with the `--`-specific wording, the reported argv index
  distinguishing the two placements, and the refusal fires before any
  equality gate reads the argv

#### Scenario: A value-position `--` cannot blind the tool-value pins

- **WHEN** a capture argv carries `--schema-dump-host -- --psql
  /tmp/stub` — the `--` sitting in value position so a
  flag-position-only grammar would consume it as a value while a
  stop-at-`--` scanner hides the trailing `--psql` rebinding
- **THEN** the pre-posed `--` check refuses the argv before the
  `--psql` equality gate runs, so the tool-value pin surface
  established by the anchoring series is never weakened

#### Scenario: A `-`-leading value token is refused

- **WHEN** a capture argv carries `--schema-dump-host -xh` (a
  deliberately value-unpinned option bound to a token the real parser
  would refuse with "expected one argument", exit 2)
- **THEN** the pair grammar refuses it with the value-position
  wording naming both the flag and the offending value token

#### Scenario: Production plans are never refused by the grammar

- **WHEN** the plan author emits its default capture argvs for all
  twelve kinds, including `schema_dump_list` with the
  `--schema-dump-host`/`--schema-dump-container` extra pairs
- **THEN** every argv parses under the closed grammar and the
  whole-capture-gate positive control stays green

#### Scenario: The grammar's flag set cannot drift from the real parser

- **WHEN** a flag is added to or removed from the capture CLI parser
  without updating the verifier's restated literal flag set
- **THEN** the structural premise test fails (the literal set plus
  argparse's auto `-h`/`--help` pair plus the seam-prefixed flags
  must equal the parser's registered surface — 17 option strings
  today), so drift reddens instead of being silently followed

### Requirement: The invocation-contract dead island MUST be removed rather than left as a drifted pseudo-oracle

The compression live-evidence module SHALL NOT carry the
production-dead invocation-contract island orphaned by the
supervisor-owned execution lane (#1069) and exposed by the aace0913
orphan-validator removal (#1239): the `INVOCATION_ARGV` mapping (a
second, already-drifted hand copy of the launch contract whose live
single source is the supervisor-ledger lane's `command["argv"]`
equality gates), the `_TIMEOUT_PREFIX` launcher-wall constant, and the
`_invocation_execution_identity` resolver. Test fixtures SHALL stop
stamping unverified provenance fields (argv, launcher identity,
resolved paths, artifact bindings) into invocation artifacts whose
content the verifier never parses, so no bundle artifact looks like a
provenance oracle that is in fact constrained only as
`{path, sha256, bytes}`. The live argv contract
(`_validate_exact_command_argv` / `_concrete_argv`) and the
`database_audit_proof` schema pins stay untouched, and the
content-is-not-truth sentinel test
(`test_legacy_authored_invocations_do_not_contribute_to_v3_truth`)
survives with its negative semantics intact.

#### Scenario: The island symbols are gone

- **WHEN** the repository's Python sources are scanned
- **THEN** `grep -rn --include="*.py"` for `INVOCATION_ARGV`,
  `_invocation_execution_identity`, and `_TIMEOUT_PREFIX` each return
  zero hits

#### Scenario: No unverified provenance fields remain in any fixture

- **WHEN** the repository's Python sources are scanned for the
  pseudo-provenance field names as whole words
  (`grep -rnE "\b(launcher_argv|resolved_interpreter|resolved_wrapper|resolved_env_file|resolved_repo_path|resolved_script|artifact_bindings)\b" --include="*.py"`
  — word-bounded because the bare substring `resolved_script` collides
  with the unrelated live CI-selection helper
  `_resolved_script_modules`, which stays untouched)
- **THEN** each returns zero hits, and the invocation fixtures carry
  no field asserting launcher provenance or artifact bindings

#### Scenario: The live contract and honest pins are untouched

- **WHEN** the deletion diff is inspected
- **THEN** `_validate_exact_command_argv` and `_concrete_argv` are
  unchanged, the `authorization.database_audit_proof` and
  `execution.database_audit_proof` schema pins remain
  `{"const": false}`, and the legacy-invocation sentinel test still
  passes with its `qualifies_task_4_5 is True` assertion intact

### Requirement: Committed historical compression receipts MUST stay schema-valid under test

The four committed historical compression runner receipts SHALL be validated
against `schemas/timeseries_compression_receipt.schema.json` by a committed
parametrized test that globs the receipts directory by runner filename prefix
(excluding the co-located live-evidence family) and asserts the glob is
non-empty, so schema tightening that invalidates the archive fails loudly.

#### Scenario: schema tightening goes red

WHEN the schema's 1.0 branch gains a new required field
THEN the committed 1.0 receipts fail the parametrized validation test

#### Scenario: mistyped glob cannot fake-green

WHEN the receipt glob matches fewer than four files
THEN the count-guard test fails

### Requirement: Every replace-path DELETE on a compression-capable hypertable MUST be bounded by the window its guard certified

A writer that replaces a lineage's rows in a hypertable eligible for compression SHALL NOT issue a DELETE without a `valid_time` bound, and the window it passes to `check_batch_targets_uncompressed` SHALL be the same window the DELETE targets — the union of the rows already stored for that
lineage and the rows in the incoming batch. A guard window narrower than
the DELETE's target set certifies rows it never inspected and makes the
fail-closed contract hollow; an unbounded DELETE is rejected outright by
TimescaleDB once any chunk of the hypertable is compressed, even when zero
rows match.

#### Scenario: Existing rows outside the incoming batch widen both windows

- **WHEN** `workers/forcing_producer/store.py::replace_forcing_timeseries`
  runs for a `forcing_version_id` whose stored rows extend beyond the
  incoming batch's `valid_time` range
- **THEN** `check_batch_targets_uncompressed` receives the union of the
  stored and incoming ranges, not the incoming range alone
- **AND** the emitted DELETE carries `valid_time >= %s AND valid_time <= %s`
  bound to that same union
- **AND** the guard's SQL is executed before the DELETE

#### Scenario: An empty batch with stored rows still purges within the stored window

- **WHEN** the same replace runs with an empty incoming batch for a
  `forcing_version_id` that has stored rows
- **THEN** the guard receives the stored rows' `valid_time` range
- **AND** a DELETE bounded to that range is executed, preserving the
  replace path's existing purge semantics
- **AND** no INSERT is executed

#### Scenario: No stored rows and an empty batch skip the DELETE

- **WHEN** the same replace runs for a `forcing_version_id` with no stored
  rows and an empty incoming batch
- **THEN** no DELETE statement is executed at all

#### Scenario: A guard refusal still precedes every write

- **WHEN** the guard reports a compressed chunk that lies inside the union
  window but outside the incoming batch's own range — the case that
  previously returned PASS while the unbounded DELETE still targeted it
- **THEN** `CompressedChunkWriteError` is raised, no DELETE and no INSERT
  are executed, and the transaction is rolled back

#### Scenario: The write-guard wire-site invariant remains intact

- **WHEN** `tests/test_timescale_write_guard_wire_site_invariant.py`
  inspects `workers/forcing_producer/store.py`
- **THEN** `replace_forcing_timeseries` is still defined, still makes
  exactly one `self._replace_values(...)` call, and still binds
  `pre_write_cursor_hook=` to a locally-defined function

### Requirement: The surviving `*_invocation` slots MUST name their real truth source

The evidence schema SHALL annotate every surviving `*_invocation` slot
with its real truth source. The five slots of
`timeseries_compression_live_evidence` — `recovery.invocation`,
`migration.first_invocation`, `migration.second_invocation`,
`receipts.dry_run_invocation` and `receipts.enforce_invocation` — SHALL
each carry a schema `description` stating what the slot actually is:
required by the verifier's exact-key check on the input bundle and by
the schema in a v3 qualifying (non-failure) terminal document; its
invocation semantics — argv, exit code, timings — never interpreted;
the terminal slot re-derived from `execution.ledger` rather than copied
from what was authored; and the authored value itself a
closure node, retained in the terminal `source_manifest`, only when it
is exactly a `{path, sha256, bytes}` mapping — with any well-formed
reference nested inside a value of another shape still closure-checked
in its own right. The runbook narrative describing these
referenced contracts SHALL name the five keys and SHALL NOT describe
them as optional.

The description SHALL NOT claim that the authored value is ignored,
unread, or absent from the terminal document; SHALL NOT claim that the
terminal slot always differs from the authored value; and SHALL NOT
claim that existence and hash enforcement applies unconditionally. All
three are false. Two live bundle shapes exist and an unqualified claim
must hold for both: the legacy hand-assembled shape, whose five slots
name five distinct files, and the committed bundle author's shape
(`scripts/node27_timeseries_compression_bundle_author.py`), whose five
slots are all the ledger reference itself.

Keeping the slots is the recorded decision; this requirement governs
what the contract says about them, not whether they exist. The live
argv contract (`_validate_exact_command_argv` / `_concrete_argv`), the
`database_audit_proof` `{"const": false}` pins, and the #1261 ruling
that launcher/interpreter identity is producer-side attestation rather
than a verifier gate all stay untouched, and no launcher/interpreter
identity gate is introduced.

#### Scenario: Every surviving slot is annotated with its truth source

- **WHEN** `schemas/timeseries_compression_live_evidence.schema.json`
  is loaded and every property declared under a `properties` map whose
  name ends in `invocation` is collected
- **THEN** the collected set is exactly the five known slots, and each
  carries a non-empty `description` naming `execution.ledger` as the
  source the verifier re-derives the slot from

#### Scenario: Authored invocation content is not v3 truth

- **WHEN** a bundle is verified whose `*_invocation` slots point at
  files whose content contradicts the run — a non-zero exit code, a
  wrong timeout, or the same invocation reused for both migration
  steps
- **THEN** verification still qualifies the bundle, because no code
  reads argv, exit codes or timings out of those files

#### Scenario: Enforcement applies only to a well-formed artifact reference

- **WHEN** a slot's authored value is exactly a
  `{path, sha256, bytes}` mapping naming a path that is absent, a
  symlink, or whose hash or size disagrees with the file
- **THEN** the run fails closed at the artifact-closure check

- **WHEN** a slot's authored value is any other shape that contains no
  nested reference — a mapping with an extra scalar key, a string, or
  `null`
- **THEN** no closure check reaches it and verification can still
  qualify, because the evidence schema is applied to the terminal
  document rather than to the input bundle

- **WHEN** a slot's authored value is a mapping of another shape that
  *wraps* a well-formed `{path, sha256, bytes}` reference
- **THEN** the nested reference is still collected as a closure node in
  its own right, and an unavailable or unsafe path inside it still
  fails the run closed

#### Scenario: A well-formed authored reference is retained in the terminal manifest

- **WHEN** a qualifying bundle whose five slots name five distinct
  well-formed artifact references is verified, and its terminal
  document's `source_manifest` is read
- **THEN** the five authored paths appear there with their authored
  `sha256`/`bytes`, distinct from the ledger reference that occupies
  the five slots themselves

- **WHEN** the bundle is instead one the committed bundle author
  produced, whose five slots are all the ledger reference
- **THEN** `source_manifest` carries that reference once rather than
  five times, because the closure deduplicates identical normalized
  paths

### Requirement: A recompute blocked by the compressed-chunk guard MUST reach a recorded terminal state instead of retrying forever

一次被压缩块守卫拒绝的重算 SHALL 达到一个被记录的终态，而不是无限重试。具体地：当 ingest tick 正确检出一次产物重算、而该重算的写入被 `check_batch_targets_uncompressed` 以 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 拒绝时，该 run SHALL 被记入一条终态 decline 记录并停止重投，tick SHALL 以 `rc=0` 结束。终态记录 SHALL 以 `(run_id, init_state_id, product_mtime)` 为键，使任何新的
重算证据自动重开该决定。记账 SHALL 是可查询的持久状态，而非仅一行日志。

#### Scenario: A compressed-chunk-blocked recompute is declined, not failed

- **WHEN** ingest tick 处理一个 run，其 forcing handoff 返回的 reason codes 含
  `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
- **THEN** `_process_run` 返回 `outcome="declined"`（不是 `"failed"`），
  `ops.ingest_recompute_decline` 新增一行 `(run_id, init_state_id, product_mtime,
  reason_code='HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED')`，
  tick 汇总的 `runs.declined_runs` 含该 run_id，且进程 `rc == 0`

#### Scenario: A transient forcing failure still fails the tick and retries

- **WHEN** forcing handoff 因任何非 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
  的原因失败（含通用异常路径与 `HANDOFF_APPLY_SQL_FAILURE`）
- **THEN** `_process_run` 返回 `outcome="failed"`，`ops.ingest_recompute_decline`
  不新增任何行，进程 `rc == 1`，该 run 在下个 tick 仍进入 pending

#### Scenario: The second tick does not retry a declined run, at any hydro_run status

- **WHEN** 一个 run 已有键完全匹配当前 manifest `initial_state.state_id` 与
  `product_mtime` 的 decline 记录，且下一个 tick 重新评估它
- **THEN** `_already_ingested_runs` 把该 run 放进返回集（与 `retired` 并列的
  状态无关排除项），该 run 不进入 pending，没有新的 handoff 尝试发生，tick `rc == 0`
- **AND** 无论该 run 的 `hydro_run.status` 是 `published`、`parsed` 还是
  `succeeded`，抑制都同样生效——抑制 SHALL NOT 依赖于该 run 是否进入
  `status IN ('parsed','published')` 的完备性查询

#### Scenario: A never-published run is suppressed too

- **WHEN** 一个 `hydro_run.status = 'succeeded'` 的 run（从未 parsed/published，
  因而从不出现在完备性查询结果里）被压缩块守卫挡住并写入 decline 记录
- **THEN** 下一个 tick 该 run 同样不进入 pending，且没有新的 handoff 尝试发生

#### Scenario: A newer regeneration reopens the declined decision

- **WHEN** 一个已被 decline 的 run 的产物被重新生成，使 `product_mtime` 变新
  （或其 `init_state_id` 变更）
- **THEN** 已有的 decline 记录不再匹配，该 run 不再被并入 `_already_ingested_runs`
  的返回集，于是重新进入 pending 并被重试

#### Scenario: A guard-internal failure is not a compressed-chunk block

- **WHEN** `check_batch_targets_uncompressed` 因自身原因失败——catalog 查询超时
  （它给自己设了 5s `statement_timeout`）、批次窗口只有单端点、目标 hypertable
  未注册——从而抛出**基类** `CompressedChunkGuardError` 而非子类
  `CompressedChunkWriteError`
- **THEN** handoff SHALL 报告一个与真实压缩块阻塞**不同**的 reason code
  （`HANDOFF_APPLY_COMPRESSED_CHUNK_GUARD_FAILED`），`_process_run` 返回
  `outcome="failed"`，进程 `rc == 1`，**不**写入任何 decline 记录，该 run 继续重试
- **AND** `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` SHALL 只由子类
  `CompressedChunkWriteError` 那条分支挂出，即它只表示"确实探测到压缩块"

#### Scenario: A decline record carries a diagnosable detail

- **WHEN** 一条 decline 记录被写入
- **THEN** 其 `detail` 列 SHALL 携带底层守卫消息（已经过 `redact_text`），
  而不是仅仅重复 reason code 本身——否则一条被误记的 decline 事后无法甄别

#### Scenario: A failed decline read degrades to no suppression, never to a crash

- **WHEN** 对 `ops.ingest_recompute_decline` 的读取失败（典型情形：代码已部署
  而迁移 `000055` 尚未 apply，或任何 `psycopg2.Error`）
- **THEN** 该读取 SHALL 被限定在自己的 savepoint 内，失败时降级为"不抑制任何
  run"，tick SHALL 正常完成并输出 JSON 汇总；抑制的缺失使被挡的 run 继续重试并
  以 `rc == 1` 报红——即退化为本变更之前的行为，而不是整个 tick 未捕获异常退出

#### Scenario: A genuinely unobtainable key fails closed

- **WHEN** 一次 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 发生，但 `product_mtime`
  取不到，或 decline 记录写入抛出异常
- **THEN** `_process_run` 返回 `outcome="failed"`（不终态化），进程 `rc == 1`，
  该 run 在下个 tick 继续重试
- **AND** `init_state_id` 缺失**不属于**本场景（见 D11）：它是"已知无 manifest"
  而非"证据不可知"，按 `''` 记账并正常终态化。fail-closed 只剩上述两条真不可知路径

#### Scenario: Declined runs stay visible after the tick that declined them

- **WHEN** 任意 ingest tick 结束并输出 JSON 汇总
- **THEN** 汇总含 `declines_active` 字段，其值为 `ops.ingest_recompute_decline`
  的当前行数；读取失败时为 `null`（`null` 本身即"计数未知"的信号，绝不省略该字段
  也绝不因此把一次成功的 ingest tick 判红）。这使一条长期存在的终态记录在每个
  tick 上都可被 grep 到

#### Scenario: A declined run does not inflate ingested or publish counters

- **WHEN** 一个 tick 内既有 declined run 也有正常 ingested run
- **THEN** `runs.ingested` 只计入真正写入的 run，declined run 既不计入
  `runs.ingested` 也不计入 `runs.failed`，也不参与 `_stats_guard`（后者钉的是
  本 tick 真正 ingest 的条数，不是 publish 判据）

#### Scenario: A blocked run with no manifest still reaches the terminal state

- **WHEN** 一个被压缩块挡住的 run 既没有 manifest、`hydro_run.init_state_id`
  也为 `NULL`，但产物 mtime 可取
- **THEN** 它同样被 decline，记录的 `init_state_id` 为空串 `''`（合法键值，
  含义是"已知无 manifest"），并在其后的 tick 中被抑制、不再发起 handoff 尝试。
  fail-closed 只保留给真正不可知的情形：`product_mtime` 取不到、或 DB 写入失败
- **AND** 写入侧与读取侧必须由**同一个**键计算逻辑得出该键——否则会出现
  "写得进、读不出"的半修复：每 tick 重新 decline 被 `ON CONFLICT DO NOTHING` 吞掉，
  `rc` 变 0 而 handoff 仍在永久重试
- **AND** 日后若出现带真实 `initial_state_id` 的 manifest，键随之改变、与记录失配，
  该 run 自动重开重新评估（manifest 被瞬时读坏的情形同理自愈）

#### Scenario: A standing decline counts as already-done, exactly like a retired run

- **WHEN** 一条已存在的 decline 记录在**其后**的某个 tick 上仍与产物证据相符
- **THEN** 该 run 落在 `_already_ingested_runs` 的返回集里，因而计入
  `already_count` 并可独立满足 `publish_eligible`——这与 `retired`
  （`status='superseded'`）在 #1781 之前就有的行为**完全同形**，是刻意的并列语义，
  不是回归。此时 `_publish_display_runs` 的 UPDATE 命中零行（无 `parsed` 可推进），
  不写行、不动 `updated_at`。注意由此 `already_ingested` 字段会计入从未 ingest 过
  的 run；判断"本 tick 真正写了多少"一律看 `runs.ingested`，不要看该字段

#### Scenario: The decline lookup is batched and its object-store reads are bounded

- **WHEN** `_already_ingested_runs` 为一个 tick 的 run_ids 集合评估完备性
- **THEN** decline 记录通过单次 `WHERE run_id = ANY(...)` 查询一次性取回
- **AND** object store 的 manifest/mtime 读取只对**有 decline 记录的 run** 发生，
  次数与 decline 行数同阶，SHALL NOT 与 pending 规模同阶

#### Scenario: An unmatched decline key does not suppress

- **WHEN** 一个 run 有 decline 记录，但当前 manifest 的 `initial_state.state_id`
  或 `product_mtime` 取不到，或与记录中的值不相等
- **THEN** 该 run 不被抑制，正常进入 pending 并被重试

### Requirement: The manual tiering procedure MUST check for pending recomputes before compressing a window

手工压缩一个 chunk 前，运维 SHALL 确认该 chunk 的时间窗口已脱离产物重算地平线：
窗口内不存在待重算的 run，也不存在指向该窗口的 decline 记录。tier runbook SHALL
提供可执行的检查清单，而非仅描述性建议。

#### Scenario: The tier runbook carries an executable pre-compression checklist

- **WHEN** 阅读 `docs/runbooks/tier-node27-timeseries-storage.md` 的压缩小节
- **THEN** 该小节含一份压缩前置检查清单，其中至少一项是对
  `ops.ingest_recompute_decline` 按目标窗口的可直接执行 SQL 查询，
  并说明命中时的处置（先排干或显式接受终态）

