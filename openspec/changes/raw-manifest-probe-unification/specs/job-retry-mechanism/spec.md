# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Raw-Manifest Repair Legs Consult the Unified Artifact Probe

Both raw-manifest repair legs SHALL determine manifest presence through
the unified artifact probe rather than the bare object-manifest check —
this covers the missing-manifest repair channel and the downstream
retry-after-repair channel — and SHALL act only on a probe verdict whose
unsafe reason is null. When the probe reports a non-null unsafe reason
(no object-store root configured, or a contained probe fault), both legs
SHALL abstain: neither leg asserts manifest existence or absence, grants
an automatic retry, nor lets an exception escape; the candidate flows to
the remaining decision ladder, which alone determines the terminal — the
legs invent no manual channel of their own, a transient failure within
its retry budget whose state engages no higher ladder rung keeps its
automatic retry from the generic rung, and geometries the fail-open
verdict previously shielded from the ladder's own rungs take those
rungs' existing terminals (a native-SHUD restart stage whose forcing
reference the artifact guard fail-closes, a permanent failure code
including remedy-permitted ones, a cancelled run, or an exhausted retry
budget — each lands on the rung that already owned it) — and the rest
of the scheduling pass keeps running. When the probe determines
presence or absence with a null unsafe reason, both legs keep their
existing behavior byte-for-byte, including the recorded residual where
a reference the probe cannot resolve counts as absent for the repair
channel (a re-ingestion re-records it).

#### Scenario: An unconfigured store no longer vouches for manifest existence

- **GIVEN** a candidate whose resource profile carries no object-store
  root and no `OBJECT_STORE_ROOT` environment fallback, with a failure
  state that satisfies the downstream retry-after-repair structural gates
- **WHEN** the downstream leg evaluates the raw-manifest URI
- **THEN** no evidence claiming `manifest_exists: true` or granting
  `automatic_retry_allowed: true` is produced by the raw-manifest legs;
  the candidate's decision comes from the remaining ladder

#### Scenario: Abstention does not convert guard-free transient failures to manual outcomes

- **GIVEN** the same unconfigured-store geometry where the candidate's
  failure classification is transient and within its retry budget, its
  restart geometry engages no ladder guard of its own (a convert-stage
  restart with no forcing or copyback requirement), and no higher
  ladder rung (the permanent guard, the cancelled rung) claims the
  state
- **WHEN** the scheduler decides the candidate's state
- **THEN** the decision remains an automatic retry from the generic
  retry rung — the legs' abstention itself invents no manual channel

#### Scenario: Abstention un-shadows the ladder's own guards rather than overriding them

- **GIVEN** the unconfigured-store geometry where the fail-open verdict
  previously let the downstream leg claim the candidate, and either the
  restart stage is the native-SHUD forecast stage (so the artifact guard
  probes its forcing reference) or the failure code is permanent but
  remedy-permitted
- **WHEN** the scheduler decides the candidate's state
- **THEN** the terminal is whichever ladder rung owns the geometry — for
  these two pinned cases the guard's existing blocked outcome
  (`missing_forcing_package_uri` — carrying the unsafe reason when the
  fault is process-wide, an unconfigured root; a single-leaf probe fault
  leaves the guard's own probe verdict intact — or
  `permanent_failure_guard`; a cancelled run or an exhausted retry
  budget likewise keeps its own rung's terminal, the latter pinned by
  the pass-containment scenario) — the same terminal the identical
  candidate already received whenever the legs' structural gates did not
  hold, not a new terminal introduced by the legs

#### Scenario: An unconfigured store does not let the repair leg invent a verdict

- **GIVEN** the unconfigured-store geometry with a failure state that
  satisfies the missing-manifest repair structural gates and a manifest
  object that genuinely does not exist
- **WHEN** the repair leg evaluates the raw-manifest URI
- **THEN** the repair leg abstains without asserting the manifest
  present or absent; the repair channel stays untriggered until a store
  root is configured, which is the recorded limitation of the abstention
  design and identical to the pre-change outcome for this leg

#### Scenario: A probe fault degrades to abstention instead of aborting the pass

- **GIVEN** a raw-manifest probe whose object store raises the contained
  probe fault (a symlinked probe target or a stale filesystem handle)
- **WHEN** the scheduler evaluates a batch containing that candidate and
  a healthy sibling
- **THEN** no exception escapes the scheduling pass: the faulted
  candidate receives a terminal from the remaining decision ladder, and
  the sibling is still evaluated and submitted (a submitted count of
  exactly the sibling)

#### Scenario: Configured-store geometries keep their behavior

- **GIVEN** a candidate with a configured, resolvable object-store root
- **WHEN** either raw-manifest leg evaluates its URI
- **THEN** the produced evidence is byte-for-byte identical to the
  pre-change behavior for both the present and the absent manifest cases
