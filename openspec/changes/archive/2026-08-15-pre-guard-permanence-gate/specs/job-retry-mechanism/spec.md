# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Pre-Guard Evidence Channels Consult Permanence

Every db-free decision-ladder evidence channel that can emit an automatic-retry decision before the permanent-failure guard — except the output-absence recompute channel, recorded exempt below — SHALL consult a single shared permanence judgement before overwriting a permanent failure classification, and SHALL refuse the overwrite when the failure's classification proves the channel's remedy cannot address the cause.

The permanent-failure guard remains consulted at emitting return points
(never as an unconditional pre-pass). The recorded-code scoping governs the
downstream-resume channel's unknown-code clause only: reader-synthesized
placeholder codes (defaults fabricated when the state records no error code)
are not evidence under that clause and keep their existing behavior,
including the existing classifier-based refusals; the raw-manifest and
model-package channels consult the judgement for every permanent
classification, recorded code or not.

This requirement carves out deliberate, recorded exceptions to "Retry
Guard — Non-Transient Error Exclusion" (and, where noted, to the
unknown-code default and max-retries clauses) for three geometries whose
structural evidence proves the remedy causal — these are recorded
exceptions to those clauses' blanket prohibitions, not reinterpretations:

- **Output-absence recompute** (ruled in #1161): when durable forecast
  output is absent, the recompute channel may schedule an automatic restart
  from the forecast stage for its approved code set (including
  `OUT_OF_MEMORY`), including with an exhausted retry budget (the channel
  carries no budget gate), and is exempt from the consultation obligation
  above (it gates on its own approved code set instead).
- **Raw-manifest repair and post-repair downstream retry**: when the
  geometry itself evidences an input defect (a manifest probed missing
  after a previously successful download, or a repair download newer than
  the failure), the channels may re-emit their repair/retry decisions for
  any recorded code outside the remedy-non-causal classes — input-defect
  codes (e.g. `INVALID_MANIFEST`), unknown-default codes (e.g.
  `SLURM_JOB_FAILED`), and other listed non-transient codes (e.g.
  `OUTPUT_INCOMPLETE`) — including with an exhausted retry budget.
- **Model-package refresh** (ruled in #1161): when the model package
  genuinely changed, the refresh channel may claim codes outside its own
  refusal set (e.g. `TEMPLATE_NOT_ALLOWED`), because the changed package is
  itself the causal remedy for policy/template rejections, including with
  an exhausted retry budget.

Manual-retry paths are out of scope: their emitted decision, reason, and
retry policy are unchanged (the `failure.retryable` evidence field narrows
with the shared classification).

#### Scenario: Raw-manifest repair channels refuse remedy-non-causal permanent codes

- **WHEN** a candidate's failure state matches the missing-raw-manifest
  repair geometry (or the repaired-raw-manifest downstream geometry) and the
  recorded failure classifies as permanent with a classification proving the
  input-repair remedy non-causal (resource/configuration or policy/permission
  class — at minimum `OUT_OF_MEMORY`)
- **THEN** the channel SHALL NOT emit a retry decision — the ladder falls
  through to the remaining channels and, absent another legitimate claim,
  to the permanent-failure guard with `automatic_retry_allowed: false`

#### Scenario: Raw-manifest geometry evidence keeps other codes repairable

- **WHEN** the same raw-manifest geometries match with any other recorded
  code — input-defect codes (e.g. `INVALID_MANIFEST`) or unknown codes
  defaulted non-transient (e.g. `SLURM_JOB_FAILED`), including with an
  exhausted retry budget
- **THEN** the repair/downstream-retry decision SHALL be emitted exactly as
  before — the geometry itself (a manifest probed missing after a previously
  successful download, or a repair download newer than the failure) is the
  causal evidence that re-ingesting input is on point, and the repair remedy
  SHALL NOT be retired for production code shapes

#### Scenario: Downstream resume refuses recorded permanent and unknown-default codes

- **WHEN** a candidate with durable SHUD output fails a downstream stage
  with a genuinely recorded code that is non-transient (e.g.
  `OUTPUT_INCOMPLETE`) or unknown and defaulted non-transient (e.g. a
  recorded `PARSE_FAILED`, `SLURM_JOB_FAILED`), or whose retry budget is
  exhausted
- **THEN** the downstream-resume channel SHALL NOT emit a resume decision;
  a recorded transient code within budget SHALL keep the existing resume
  behavior unless the state explicitly marks the failure permanent
  (top-level `permanent: true`, which forces permanence — see the top-level
  key scenario below)

#### Scenario: Synthesized placeholder codes keep existing downstream behavior

- **WHEN** a downstream failure state records no error code and the reader
  synthesizes a stage-derived placeholder for classification
- **THEN** the downstream-resume decision SHALL behave exactly as before
  this change — the unknown-code clause governs recorded codes, not
  reader-fabricated defaults

#### Scenario: Top-level state retryable cannot whiten a permanent code

- **WHEN** a candidate state carries a top-level `retryable: true` key while
  its failure classifies as permanent (e.g. `OUT_OF_MEMORY`)
- **THEN** the permanence classification SHALL stand and the decision falls
  to the permanent-failure guard; the top-level key MAY only reassert
  retryability for codes whose classification is already retryable, and an
  explicit top-level `permanent: true` still forces permanence

#### Scenario: Model-package refresh and output-absence recompute rulings unchanged

- **WHEN** a candidate matches the model-package refresh geometry (permanent
  failure + changed package, refusing the resource-configuration class and
  `OUT_OF_MEMORY`), or the missing-forecast-output recompute geometry with a
  code in its approved recompute set
- **THEN** both channels SHALL behave exactly as before this change — the
  refresh channel's refusal list moves to the shared judgement source with
  zero semantic change, and a code refused by the raw-manifest channels MAY
  still be legitimately claimed by the refresh channel when the package
  genuinely changed
