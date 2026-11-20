## MODIFIED Requirements

### Requirement: Chain facade keeps orchestration entrypoints stable
The chain facade SHALL keep `ForecastOrchestrator`, `OrchestratorConfig`, the
chain result/context types, `SlurmGatewayClient`, `HttpSlurmGatewayClient`, and
the `services.orchestrator.chain` import and monkeypatch surfaces stable while
implementation families live in owner modules. Owner modules SHALL keep reading
facade names at call time. The facade SHALL import each name it exposes
explicitly. It SHALL NOT install names programmatically (`globals().update` of
an installer's return value, or merged export dicts), and it SHALL NOT carry
`_*_COMPAT_*` self-check tables.

#### Scenario: legacy chain import remains in use
- **WHEN** callers import a chain symbol from `chain.py`
- **THEN** the import resolves through an explicit import or alias line in
  `chain.py`

#### Scenario: chain monkeypatch path is used
- **WHEN** a test monkeypatches a chain helper through
  `services.orchestrator.chain`, for example array accounting, manifest, tile
  publisher, source identity, or `_utcnow`
- **THEN** the owner implementation observes the patch, because it reads the
  name off the facade at call time
- **AND** the facade name-resolution test fails before merge if the name does
  not resolve on the facade

### Requirement: Chain owner families are completed rather than partially moved
Chain extraction SHALL move behavior by complete owner families: stage
catalog/type contracts, stage execution, array accounting, manifest assembly,
reservation, retry, tile publication, worker/source-identity adapters, and
time-consistency behavior. The file journal owns repository behaviour; the
chain keeps only the `OrchestratorRepository` protocol.

#### Scenario: repository behavior is not ready to move
- **WHEN** a reader looks for orchestration repository behaviour in the chain
- **THEN** `chain.py` holds only the `OrchestratorRepository` protocol and the
  implementation is `FileOrchestrationJournalRepository`; no
  `PsycopgOrchestratorRepository` exists

#### Scenario: owner family extraction changes facade surface
- **WHEN** a PR adds or removes a chain facade name
- **THEN** it adds or removes the explicit import line, and the facade
  name-resolution test stays green

#### Scenario: chain inventory is updated with the extraction
- **WHEN** a chain owner family changes ownership
- **THEN** no compatibility inventory is edited; the owner module, the explicit
  facade import, and its focused tests are the record, and
  `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` does not exist
