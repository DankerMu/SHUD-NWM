# Spec Delta: hypertable-compression (canonical plan-author roots)

## ADDED Requirements

### Requirement: The plan author MUST reject non-canonical repo and root paths at authoring time

`plan_author.build_run_plan` SHALL reject any `repo` or `root` value
that is not Path-normalization-stable (the value must equal its own
`str(Path(value))` rendering) or that ends in a slash — refusing
trailing slashes, interior duplicate slashes, `.` segments, and the
bare slash-roots `/` and `//` (the only normalization-stable strings
that end in a slash) with a `PlanAuthorError` that names the label,
the offending value and its canonical rendering — so that every path
the plan records is canonical byte-for-byte and the verifier's verbatim
plan-side comparisons (capture `output_path` equality and command
artifact-association equality) can never falsely refuse a legitimately
authored bundle whose ledger-side paths arrive Path-normalized. The
verifier itself stays verbatim: it judges the recorded bytes and
invents no normalization; the closure lives entirely at the producer
entrance.

#### Scenario: A trailing-slash root fails at authoring, not at the forensic gate

- **WHEN** `build_run_plan` is called with `root` (or `repo`) that is
  not Path-normalization-stable (trailing slash, interior `//`, `/./`)
  or is a bare slash-root (`/`, `//`) — while a LEADING double slash
  as in `//x` stays accepted: POSIX preserves exactly two leading
  slashes, so it is normalization-stable and expands symmetrically on
  both verifier sides
- **THEN** it raises `PlanAuthorError` naming the label and the
  canonical rendering, and no plan is produced — eliminating the
  previous behavior where the plan authored fine but its bundle
  deterministically failed verification with "supervisor capture
  output path differs"

#### Scenario: Canonical inputs and the module defaults are unaffected

- **WHEN** `build_run_plan` is called with canonical absolute paths
  (including the module's own `DEFAULT_ROOT`/`DEFAULT_REPO`, whose
  canonicality a structural test pins)
- **THEN** authoring succeeds exactly as before and the twelve-kind
  positive control stays green

#### Scenario: The verifier's verbatim textual posture is preserved

- **WHEN** a hand-crafted plan carries double-slash capture spellings
  (a shape the production author can no longer emit)
- **THEN** the relational `--evidence-dir` gate still round-trips the
  spelling textually (a normalizing derivation refactor still reddens
  the guard test), and the refusal that ends such a bundle remains the
  pre-existing verbatim ledger↔plan equality — the verifier gains no
  normalization anywhere
