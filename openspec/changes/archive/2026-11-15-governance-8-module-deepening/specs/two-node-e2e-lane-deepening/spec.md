## ADDED Requirements

### Requirement: Two-node E2E validator entrypoint remains stable
The two-node E2E evidence validator SHALL keep
`validate_two_node_e2e_evidence(config)`, its CLI behavior, final summary
schema, lane summary shape, and final status ordering stable while lanes move
to owner modules.

#### Scenario: lane owner is extracted
- **WHEN** a lane moves out of `two_node_e2e_evidence.py`
- **THEN** the existing validator still discovers the same input aliases,
  returns the same lane summary fields, and writes the same final artifact
  shape for equivalent fixtures

#### Scenario: final aggregation is not extracted first
- **WHEN** lane result interfaces are not all stable
- **THEN** final aggregation remains behind the current validator entrypoint
  rather than moving coupling into a new module

### Requirement: Shared two-node evidence contracts are single-source
The two-node E2E extraction SHALL provide shared contracts for lane result
adapters, strict identity, current-run binding, producer/source artifacts,
redaction, approved-root path safety, and log URI safety instead of duplicating
those rules per lane.

#### Scenario: source lane consumes producer proof
- **WHEN** API, browser, logs, manual ops, readonly DB, Slurm, compute, display,
  or Docker lanes validate producer-backed evidence
- **THEN** they use the shared producer/source-artifact contract and preserve
  the same blocker/finding codes

#### Scenario: evidence is stale or unsafe
- **WHEN** nested evidence has a mismatched run id, unsafe path, unapproved root,
  private log URI, or non-authoritative wrapper-only proof
- **THEN** the extracted lane reports the same blocker/finding classification
  as the original aggregator

### Requirement: Every two-node lane has parity coverage
Each two-node lane extraction SHALL include focused tests for its lane and a
full-validator regression path before the lane is considered complete. Each PR
SHALL update
`docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` with the owner module,
retained aggregator surface, removal condition if any, and focused verification
command for the lane it moves.

#### Scenario: source-scope and cross-plane behavior is evaluated
- **WHEN** API, browser, logs, and cross-plane lanes are extracted
- **THEN** source-scope results, reduced-scope PARTIAL semantics, full GFS+IFS
  PASS requirements, and strict identity aggregation remain unchanged

#### Scenario: Docker preflight lane is extracted
- **WHEN** Docker preflight behavior moves to an owner module
- **THEN** current-run binding, disk/command/resource checks, approved-root
  rules, blocker namespaces, source artifact coverage, and focused
  `docker_preflight` verification remain equivalent

#### Scenario: two-node lane inventory is updated with the extraction
- **WHEN** a two-node lane changes ownership or retention classification
- **THEN** the two-node lane inventory records the owner module, retained
  aggregator surface, removal condition if any, and focused verification command
  in the same PR
