# job-retry-mechanism Spec Delta

## MODIFIED Requirements

### Requirement: Artifact existence probes SHALL witness directory-shaped package URIs via a derived manifest file key and SHALL fail closed with distinguishable evidence when no object-store root is configured

The failure-state artifact existence probe SHALL never hand a
package-prefix-shaped object URI to the object store directly. A recorded
forcing package URI counts as prefix-shaped whenever the closed-world
object-path validator does not admit it as a FILE key — with or without a
trailing `/` (the producer's directory URI carries one; the handoff lane's
normalized copy of the same reference does not). That admissibility question
SHALL be asked about the **same key the probe will resolve**: the recorded
reference SHALL first be normalized exactly as the store normalizes it
(deployment object-store prefix stripped, and percent-encoding decoded on the
`s3://` form exactly as the store does — the bare-key form's existing
non-decoding behaviour SHALL NOT change) and the
validator SHALL then be consulted on the normalized key. The classifier and the
probe SHALL NOT frame the same reference differently, and the normalization
SHALL be a single shared derivation rather than two implementations that can
drift. Deployments whose object-store prefix carries a path segment SHALL be
served identically to bare-bucket deployments. Because this classification runs
outside the probe's own containment, it SHALL NOT construct an object store or
otherwise touch the filesystem, and it SHALL NOT raise.

For the forcing package legs —
the journal and direct tiers alike, matching the sidecar tier — the probe
target SHALL then be the package manifest FILE key derived from the recorded
package URI through the single producer-isomorphic derivation helper (package
URI joined with the producer's default package-manifest filename), and a
recorded URI the validator already admits as a file key SHALL be probed as-is,
never double-derived. The emitted blocker evidence SHALL keep the recorded
package URI as the artifact reference while surfacing the derived probe key as
provenance. The copyback leg, which has no canonical witness filename, SHALL
document its exemption from witness derivation at the decision site.

An artifact SHALL count as present only when the probe target is a **regular
file**. A probe target that exists but is not a regular file — a directory
squatting on a file key, or any other non-regular entry — SHALL be reported
missing with the distinguishable unsafe reason `artifact_target_not_a_file`,
on the object leg and the local leg alike. Being non-null, that reason is
refused by the authorized repair channel, which is the correct routing: a
rebuild cannot clear a directory occupying a file key, because writing the
file at that path fails. On the local leg the kind verdict SHALL be taken on
the followed target, matching that leg's existing containment, which is itself
computed on the fully resolved path; a symlink resolving to a regular file
inside its allowed root therefore stays present, as it is today. This
file-kind verdict SHALL be a positive
determination only — an absent target SHALL keep its existing "probed,
determined absent" verdict and SHALL NOT be reclassified, and no other outcome
of the file-kind check SHALL alter a verdict the probe would otherwise have
reached.

When no object-store root is configured (neither the candidate resource profile
nor the environment provides one), the object-URI branch of the probe SHALL
fail closed: the artifact is reported missing with the distinguishable unsafe
reason `object_store_root_unconfigured`, and no object URI — existent or bogus
— is ever silently reported as present. A store-side probe fault that the store
**raises** (a symlinked probe target or ancestor, a stale or unreadable
filesystem handle) SHALL be
contained fail-closed and SHALL never escape the decision path as an exception
— this includes faults raised while classifying the recorded URI's shape, not
only faults from the store probe itself. On the journal and direct tiers the
contained fault carries its own distinguishable unsafe reason
(`artifact_probe_error`); on the sidecar tier the same fault keeps that tier's
established no-witness contract (`forcing_version_row_absent` with a
`tier_status` read-fault detail, repair-eligible per the #1203 ruling — the
`tier_status` field, not `unsafe_reason`, is what tells the operator the
rebuild cannot clear it). Classification faults are contained by answering
"not prefix-shaped", so the reference is probed as recorded and the probe's own
unresolvable-reference leg yields the repair-eligible null-reason residual;
there is no route by which a classification fault becomes
`artifact_probe_error`. A non-regular target that the store does **not** raise
on is not such a fault and carries `artifact_target_not_a_file` instead, so an
operator can tell "the filesystem misbehaved" from "something is standing where
the file should be". Because a non-regular target is a determination rather
than an inability to determine, the #1203 sidecar carve-out SHALL NOT be
extended to it: on that tier it SHALL surface as the tier's ordinary
missing-package blocker carrying `artifact_target_not_a_file` as its unsafe
reason, which the authorized repair channel then refuses on the same
rebuild-cannot-clear-it basis as the other tiers. An "absent" verdict with a null unsafe reason SHALL therefore arise
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

#### Scenario: A path-segment object-store prefix does not fabricate a witness for a present file key

- **GIVEN** a deployment whose object-store prefix carries a path segment (not a
  bare bucket) and a candidate whose recorded forcing reference names a FILE key
  that is physically present in the store
- **WHEN** the failure-state recovery leg classifies and probes that reference
- **THEN** the classifier — consulted on the normalized key — does not treat the
  reference as prefix-shaped, no witness key is derived beneath the existing
  file, and the probe reports the artifact present with a null unsafe reason
- **AND** the same candidate under a bare-bucket prefix produces the identical
  verdict, so the deployment's prefix shape is not an unguarded constraint

#### Scenario: A directory standing on an artifact file key is reported missing with a distinguishable reason

- **GIVEN** a configured object-store root in which the probe target key resolves
  to a directory rather than a regular file, the key being deep enough that the
  closed-world validator admits it as a file key
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the probe reports the artifact missing with unsafe reason
  `artifact_target_not_a_file`, distinguishable from `artifact_probe_error`,
  `object_store_root_unconfigured`, and the null "probed, determined absent"
- **AND** the authorized repair channel rejects the resulting blocker as
  `forcing_artifact_reference_unsafe`, because a rebuild cannot write a file
  where a directory stands
- **AND** a local artifact path that resolves, inside its allowed containment
  root, to a directory produces the same verdict and the same reason
- **AND** a local artifact path that is a symlink resolving to a regular file
  inside its allowed containment root is still reported present

#### Scenario: The file-kind check only ever adds the non-regular verdict

- **GIVEN** a probe whose existence step has already determined the target
  present, and a file-kind check that cannot reach a positive non-regular
  determination — the target is absent by the time it looks, or the key cannot
  be resolved for the kind query at all
- **WHEN** the probe finishes evaluating that artifact
- **THEN** the verdict is the one the probe would have reached without the
  file-kind check at all — `(present, null reason)` — and in particular a
  failure inside the file-kind check never converts a present verdict into a
  missing one

#### Scenario: A directory on the sidecar tier's derived witness key raises the tier's ordinary missing-package blocker

- **GIVEN** a candidate on the sidecar tier whose derived package-manifest
  witness key resolves to a directory in a configured object store
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the tier emits its ordinary missing-forcing-package blocker carrying
  the recorded package URI and the unsafe reason `artifact_target_not_a_file`,
  rather than emitting no blocker at all as it does today
- **AND** it does NOT take the #1203 read-fault route
  (`forcing_version_row_absent` with a `tier_status` detail), because a
  directory is a determination and not an inability to determine
- **AND** the authorized repair channel refuses the resulting blocker

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

- **GIVEN** a candidate on the journal or direct tier with a configured
  object-store root whose derived witness manifest key hits a symlinked leaf, a
  symlinked ancestor, or a stale filesystem handle — or whose recorded URI is
  malformed enough to make shape classification itself raise
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the decision returns a fail-closed blocker (never an escaping
  exception), the scheduler pass continues evaluating other candidates, a
  store-probe fault carries the unsafe reason `artifact_probe_error`
  (distinguishable from both "probed, absent" and "store unconfigured") and is
  rejected by the authorized repair channel (a forcing rebuild cannot clear a
  filesystem fault), while a malformed unresolvable reference stays on the
  repair-eligible null-reason residual (re-recording via rebuild is its remedy)

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
