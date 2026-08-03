# hypertable-compression — delta for container-dump-path-traversal-guard (#1269)

## ADDED Requirements

### Requirement: The container dump path gates MUST refuse `..` traversal before any container side effect

The five container dump path gates SHALL judge mount containment
with one shared predicate — the gates being the verifier's
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
component (`PurePosixPath(value).parts`). String prefix alone is not
containment: `/var/lib/postgresql/../../../etc/shadow` satisfies the
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
producer process exists. Existing refusal messages stay spelled as
they are (the pre-spawn value refusal is a new message, its claim
truthful from birth). An automated drift guard SHALL keep the
predicate single-source: no gate module retains an inline
mount-prefix `startswith` of its own.

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
- **THEN** it raises the mount-containment message without invoking
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

#### Scenario: In-mount values, including the interior-double-slash shape, stay admitted verbatim

- **WHEN** the container dump path is the default
  `/var/lib/postgresql/evidence/schema-before.dump` or an in-mount
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

## MODIFIED Requirements

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
pinned adjudication test keeps this ruling executable). That
authoring ruling is orthogonal to mount containment: the five
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
  verifier's prefix gate (e.g. `/var/lib/postgresql//evidence/…`)
- **THEN** authoring succeeds and the value lands verbatim as the
  pg_restore list argv's final element — the pinned executable form
  of the ruling that every comparison over this path is
  verbatim-symmetric (verifier and supervisor gates alike), so the
  false-refusal disease cannot reach it; a future change that guards
  it must consciously flip this scenario
