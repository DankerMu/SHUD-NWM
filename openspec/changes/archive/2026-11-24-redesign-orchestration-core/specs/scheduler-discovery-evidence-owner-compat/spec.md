## MODIFIED Requirements

### Requirement: Discovery evidence extraction preserves owner compatibility

The scheduler SHALL permit source-discovery evidence implementations to be
moved into a focused module only when every historical helper name, callable
signature, facade identity, import seam, and owner-module dynamic binding
remains compatible with the pre-extraction behavior.

#### Scenario: Dependency-bearing helper observes current owner symbols

WHEN a caller replaces any dependency used by a historical source-discovery
helper on `services.orchestrator.scheduler_discovery`
THEN the helper MUST resolve that replacement at the same call boundary as
before extraction
AND it MUST NOT use a statically captured value or the extracted module's copy
instead.

#### Scenario: Composite helper preserves owner recursion and sibling calls

WHEN a composite evidence helper invokes a sibling helper, recursively sanitizes
nested evidence, or reads a sensitive-key constant
THEN each lookup MUST resolve through the current `scheduler_discovery` owner
module
AND replacing the corresponding owner symbol MUST produce the replacement's
distinguishing result.

#### Scenario: Historical facades and consumers remain compatible

WHEN existing code consumes discovery helpers or the resource-limit error
through `services.orchestrator.scheduler`, `scheduler_candidates`,
`scheduler_candidate_runtime`, `scheduler_runtime`,
`scheduler_backfill_predecessor`, `scheduler_core`, or `scheduler_models`
THEN every existing name MUST remain importable with its prior runtime signature
and import-time or runtime binding semantics, through an explicit import or
alias line
AND scheduler facade aliases MUST retain identity with the corresponding
`scheduler_discovery` owner object.
