# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Artifact existence probes SHALL witness directory-shaped package URIs via a derived manifest file key and SHALL fail closed with distinguishable evidence when no object-store root is configured

The failure-state artifact existence probe SHALL never hand a
package-prefix-shaped object URI to the object store directly. A recorded
forcing package URI counts as prefix-shaped whenever the closed-world
object-path validator does not admit it as a FILE key — with or without a
trailing `/` (the producer's directory URI carries one; the handoff lane's
normalized copy of the same reference does not). For the forcing package legs —
the journal and direct tiers alike, matching the sidecar tier — the probe
target SHALL then be the package manifest FILE key derived from the recorded
package URI through the single producer-isomorphic derivation helper (package
URI joined with the producer's default package-manifest filename), and a
recorded URI the validator already admits as a file key SHALL be probed as-is,
never double-derived. The emitted blocker evidence SHALL keep the recorded
package URI as the artifact reference while surfacing the derived probe key as
provenance. The copyback leg, which has no canonical witness filename, SHALL
document its exemption from witness derivation at the decision site.

When no object-store root is configured (neither the candidate resource profile
nor the environment provides one), the object-URI branch of the probe SHALL
fail closed: the artifact is reported missing with the distinguishable unsafe
reason `object_store_root_unconfigured`, and no object URI — existent or bogus
— is ever silently reported as present. A store-side probe fault (a symlinked
or non-regular probe target, a stale or unreadable filesystem handle) SHALL be
contained fail-closed with its own distinguishable unsafe reason
(`artifact_probe_error`) and SHALL never escape the decision path as an
exception. An "absent" verdict with a null unsafe reason SHALL therefore arise
only from (a) a probe that actually ran against a resolvable file key in a
configured object store and determined absence, or (b) a recorded reference
that the closed-world validator rejects as unresolvable even after witness
derivation — a known residual where re-recording the reference via the
authorized rebuild remains an effective remedy, which is why such blockers stay
repair-eligible.

#### Scenario: Prefix-shaped forcing package URI with the package physically present is not reported missing

- **GIVEN** a candidate whose recorded `forcing_package_uri` is the canonical
  package prefix (`forcing/<source>/<cycle>/<basin_version_id>/<model_id>`,
  with or without the trailing `/`, bare key or `s3://` form) and an
  object-store root configured with the package manifest file present under
  that prefix
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the probe targets the derived package manifest file key, reports the
  artifact as present, and the decision does not emit
  `FORCING_PACKAGE_URI_MISSING`
- **AND** with the manifest file absent the decision emits the unchanged
  `missing_forcing_package_uri` blocker with a null unsafe reason (probed,
  determined absent) and the recorded package URI as `artifact_uri`

#### Scenario: Unconfigured object-store root fails closed with a distinguishable reason

- **GIVEN** a candidate with no object-store root in its resource profile and
  no `OBJECT_STORE_ROOT` in the environment
- **WHEN** the probe evaluates any object-shaped artifact URI, including a
  nonexistent bogus key
- **THEN** the probe reports missing with unsafe reason
  `object_store_root_unconfigured` (never a silent pass), and the resulting
  blocker evidence carries that reason so an operator can distinguish "no probe
  ran" from "probed, absent"

#### Scenario: Store-side probe faults are contained fail-closed and never abort the scheduler pass

- **GIVEN** a candidate with a configured object-store root whose derived
  witness manifest key hits a symlinked leaf, a symlinked ancestor, or a stale
  filesystem handle
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the decision returns a fail-closed blocker whose unsafe reason is
  `artifact_probe_error` (distinguishable from both "probed, absent" and
  "store unconfigured"), the scheduler pass continues evaluating other
  candidates, and the authorized repair channel rejects the blocker (a forcing
  rebuild cannot clear a filesystem fault)

#### Scenario: Root-unconfigured blockers are non-repairable via the authorized repair channel while probed-absent blockers stay repair-eligible

- **GIVEN** a missing-forcing blocker whose unsafe reason is
  `object_store_root_unconfigured`
- **WHEN** the operator-authorized single-cycle repair channel evaluates it
- **THEN** the repair is rejected as `forcing_artifact_reference_unsafe`
  (a forcing rebuild cannot cure a missing store configuration; the remedy is
  configuration)
- **AND** a blocker produced by a probe that ran in a configured store and
  determined the package absent (null unsafe reason) remains accepted by the
  repair channel for both existing blocker reason/classifier pairs
