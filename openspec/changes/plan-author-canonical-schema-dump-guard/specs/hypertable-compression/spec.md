# Spec Delta: hypertable-compression (schema-dump-host joins the canonical guard)

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
and every comparison over it, the verifier's prefix/shape argv gates
and the supervisor's mirror gate and verbatim argv-tail extraction,
is textual with zero normalization on either side, so the
verbatim-vs-normalized false refusal this requirement exists to
eliminate cannot occur for it; a pinned adjudication test keeps this
ruling executable).

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
