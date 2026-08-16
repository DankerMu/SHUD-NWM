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
the remaining decision ladder unchanged — a transient failure keeps its
existing automatic retry, a permanent or cancelled failure keeps its
existing blocked terminal — and the rest of the scheduling pass keeps
running. When the probe determines presence or absence with a null
unsafe reason, both legs keep their existing behavior byte-for-byte,
including the recorded residual where a reference the probe cannot
resolve counts as absent for the repair channel (a re-ingestion
re-records it).

#### Scenario: An unconfigured store no longer vouches for manifest existence

- **GIVEN** a candidate whose resource profile carries no object-store
  root and no `OBJECT_STORE_ROOT` environment fallback, with a failure
  state that satisfies the downstream retry-after-repair structural gates
- **WHEN** the downstream leg evaluates the raw-manifest URI
- **THEN** no evidence claiming `manifest_exists: true` or granting
  `automatic_retry_allowed: true` is produced by the raw-manifest legs;
  the candidate's decision comes from the remaining ladder

#### Scenario: Abstention does not convert transient failures to manual outcomes

- **GIVEN** the same unconfigured-store geometry where the candidate's
  failure classification is transient and within its retry budget
- **WHEN** the scheduler decides the candidate's state
- **THEN** the decision remains an automatic retry from the generic
  retry rung — abstention never downgrades a previously automatic
  recovery to a manual channel

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
