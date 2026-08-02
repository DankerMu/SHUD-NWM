# Spec Delta: hypertable-compression (canonical plan-author roots)

## ADDED Requirements

### Requirement: The plan author MUST reject non-canonical repo and root paths at authoring time

`plan_author.build_run_plan` SHALL reject any `repo` or `root` value
that is not Path-normalization-stable (the value must equal its own
`str(Path(value))` rendering), or that ends in a slash, or that
contains a `..` component — refusing trailing slashes, interior
duplicate slashes, `.` segments, `..` segments, and the bare
slash-roots `/` and `//` (the only normalization-stable strings that
end in a slash) with a `PlanAuthorError` that names the label, the
offending value and its canonical rendering. Rationale, two layers:
(1) every path derived from `repo` or `root` is recorded canonical
byte-for-byte, so the verifier's verbatim plan-side comparisons
(capture `output_path` equality and command artifact-association
equality) can never falsely refuse a legitimately authored bundle
whose REPO/ROOT-derived ledger-side paths arrive Path-normalized;
(2) `..` segments, though Path-normalization-stable and textually
symmetric on both sides, are refused by the no-follow filesystem
primitives (`safe_fs` rejects any `..` component) that both the
supervisor's capture writes and the verifier's artifact reads use —
a `..` root would author fine and then abort inside the one-shot
replay window with an unrelated message, the exact failure mode this
requirement eliminates. The verifier itself stays verbatim: it judges
the recorded bytes and invents no normalization; the closure lives
entirely at the producer entrance. Known recorded residuals outside
this guard: `capture_repo` (hermetic-only kwarg, value-pinned by the
verifier) and `--schema-dump-host`/`--schema-dump-container` data
paths (recorded verbatim into command artifact associations and
compared at the same verbatim site without a canonicality guard —
routed to a follow-up issue, not silently covered by this
requirement).

#### Scenario: A non-canonical root fails at authoring, not at the forensic gate or mid-run

- **WHEN** `build_run_plan` is called with `root` (or `repo`) that is
  not Path-normalization-stable (trailing slash, interior `//`, `/./`),
  is a bare slash-root (`/`, `//`), or contains a `..` component —
  while a LEADING double slash as in `//x` stays accepted: POSIX
  preserves exactly two leading slashes, it is normalization-stable,
  expands symmetrically on both verifier sides, and its parts survive
  the no-follow walkers
- **THEN** it raises `PlanAuthorError` naming the label and the
  canonical rendering, and no plan is produced — eliminating both the
  authored-but-never-verifiable middle state ("supervisor capture
  output path differs") and the authored-but-aborts-mid-window state
  ("Unsafe path component: '..'")

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
