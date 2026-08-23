# scheduler-discovery-evidence-owner-compat Specification

## Purpose
TBD - created by archiving change scheduler-discovery-evidence-owner-compat. Update Purpose after archive.
## Requirements
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
`scheduler_candidate_runtime`, `scheduler_compat_runtime`, `scheduler_runtime`,
`scheduler_backfill_predecessor`, `scheduler_core`, or `scheduler_models`
THEN every existing name MUST remain importable with its prior runtime signature
and import-time or runtime binding semantics
AND scheduler facade aliases MUST retain identity with the corresponding
`scheduler_discovery` owner object.

### Requirement: Discovery evidence extraction is behavior neutral

The extracted implementation SHALL preserve source-cycle evidence schema,
redaction, horizon defaults, cycle ordering, UTC normalization, adapter fallback,
and resource-limit behavior for all previously accepted inputs and errors.

#### Scenario: Default discovery output is unchanged

WHEN representative available, unavailable, probe-failed, rate-limited,
forbidden, duplicate, deferred, allowed-hour, and disallowed-hour discoveries
are processed after extraction
THEN their evidence keys and values, ordering, horizon metadata, reason/status
classification, and retryability MUST equal the pre-extraction contract.

#### Scenario: Secrets remain redacted

WHEN probe URIs, nested evidence keys, or nested text contain credentials,
tokens, authorization data, passwords, signatures, API keys, or owner-configured
sensitive text
THEN the emitted discovery evidence MUST redact them exactly as the existing
owner helpers require
AND replacing either the owner redaction function or owner sensitive regex MUST
be observed by the historical helper at call time.

#### Scenario: Discovery limit preserves boundary and typed error

WHEN the inclusive date scan discovers no more than the current owner
`MAX_DISCOVERED_CYCLES`
THEN discoveries MUST remain in the same per-day order without an error
AND WHEN the next daily batch would exceed the current owner limit
THEN the current owner `SchedulerResourceLimitError` class MUST be raised with
reason `cycle_discovery_limit_exceeded` and the same source, date, limit, and
count details as before extraction.

#### Scenario: Legacy adapter fallback is unchanged

WHEN an adapter rejects the one-argument `discover_cycles(cycle_date)` call with
`TypeError`
THEN window discovery MUST retry the historical two-argument call
`discover_cycles(cycle_date, None)`
AND unrelated exceptions MUST retain their pre-extraction propagation behavior.

### Requirement: Extraction satisfies the existing source-file guard

The behavior-neutral extraction SHALL reduce both source modules to at most
1,000 lines under the repository's existing deterministic guard without adding
an exemption or weakening unrelated state-machine documentation and behavior.

#### Scenario: Structural guard passes without exemption

WHEN the final change is measured and compared with master
THEN `scheduler_discovery.py` and `scheduler_discovery_evidence.py` MUST each be
at most 1,000 lines
AND `.large-file-guard.json` MUST contain no new exemption for either module
AND the diff MUST contain no cohort init-state, completion-verdict identity,
§8.7, quarantine, breaker, journal, database, or Slurm behavior.
