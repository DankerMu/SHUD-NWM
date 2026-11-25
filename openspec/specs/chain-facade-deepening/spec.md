# chain-facade-deepening Specification

## Purpose
Keep `services.orchestrator.chain` a stable import and monkeypatch surface while the implementation lives in owner modules: the facade binds its names explicitly, owner families move whole, and the facade imports nothing that has no reader.

## Requirements

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
