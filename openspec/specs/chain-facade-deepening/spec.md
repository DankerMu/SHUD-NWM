# chain-facade-deepening Specification

## Purpose
TBD - created by archiving change governance-8-module-deepening. Update Purpose after archive.

## Requirements

### Requirement: Chain facade keeps orchestration entrypoints stable
The chain facade SHALL keep `ForecastOrchestrator`, `AnalysisOrchestrator`,
`OrchestratorConfig`, chain result/context types, `SlurmGatewayClient`,
`HttpSlurmGatewayClient`, and legacy `services.orchestrator.chain` import and
monkeypatch surfaces stable while implementation families move to owner
modules.

#### Scenario: legacy chain import remains in use
- **WHEN** callers import inventoried chain symbols from `chain.py`
- **THEN** those imports keep working through compatibility re-exports until a
  caller-migration issue records safe removal

#### Scenario: chain monkeypatch path is used
- **WHEN** tests monkeypatch an inventoried chain helper such as array
  accounting, manifest, tile publisher, or source identity behavior
- **THEN** the moved implementation observes the patch or a compatibility test
  fails before merge

### Requirement: Chain owner families are completed rather than partially moved
Chain extraction SHALL move behavior by complete owner families: stage
catalog/type contracts, stage execution, array accounting, manifest assembly,
reservation, retry, tile publication, worker/source-identity adapters,
time-consistency behavior, and persistence/repository behavior.

#### Scenario: repository behavior is not ready to move
- **WHEN** `PsycopgOrchestratorRepository` or repository protocols remain in
  `chain.py`
- **THEN** the chain inventory records them as chain-owned local implementation
  and no PR classifies them as pure forwarding wrappers

#### Scenario: owner family extraction changes facade surface
- **WHEN** a PR adds or removes a chain facade wrapper, alias, or re-export
- **THEN** the chain compatibility inventory and guard expectations are updated
  in the same PR

#### Scenario: chain inventory is updated with the extraction
- **WHEN** a chain owner family changes ownership or retention classification
- **THEN** `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` records the owner
  module, retained facade surface, removal condition if any, and focused
  verification command in the same PR

### Requirement: Chain extraction preserves durable orchestration semantics
Chain extraction SHALL preserve reservation-before-submit, bind-after-submit,
retry identity, array accounting, manifest safe writes, published log behavior,
source identity, and persistence semantics.

#### Scenario: stage execution is extracted
- **WHEN** stage execution moves behind an owner module
- **THEN** reservation, Slurm submission, polling, timeout, terminal evidence,
  published log, and retry behavior match the pre-extraction behavior

### Requirement: The chain facade SHALL NOT keep imports that nothing reads

Every name bound by an import statement in `services/orchestrator/chain.py` SHALL have an in-repo reader: a use inside
`chain.py`, a read of the name through the facade by an owner module or a test, or a string patch target. The chain
facade's stable import surface is the set of names with such a reader; a name without one is not part of it.
- **Check:** a test SHALL collect the names bound by imports in `chain.py` and fail on any name that is neither used in
  `chain.py`, nor read through the facade according to the facade name-resolution scan, nor listed in a short literal
  list of names that tests read only through name lists.
- **Module loading:** an import kept only because other code depends on the module being loaded SHALL carry a comment
  naming that consumer, and is listed with it in the test.

#### Scenario: An unread import is rejected before merge

- **WHEN** `chain.py` imports a name that nothing in the repository reads
- **THEN** the test fails and names it

#### Scenario: A name read only through a test name list is kept

- **WHEN** a facade name is read by a test only through a list of names passed to `getattr`
- **THEN** the name is in the literal list and the import stays
