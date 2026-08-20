# runtime-evidence-and-operations Specification

## Purpose
TBD - created by archiving change m20-production-multibasin-continuous-automation. Update Purpose after archive.
## Requirements
### Requirement: Structured scheduler evidence

Each scheduler pass SHALL emit structured evidence suitable for production operations and release review.

The evidence SHALL distinguish execution modes from readiness claims. Deterministic, dry-run, simulated, or production-like scheduler evidence MAY support review and readiness lineage, but it SHALL NOT mark final production readiness true unless accepted live proof receipts satisfy the readiness proof contract.

#### Scenario: pass summary evidence

WHEN a scheduler pass finishes
THEN evidence includes pass id, started/finished timestamps, execution mode, live proof receipt references when applicable, sources, cycle window, model count, candidate count, submitted count, skipped reasons, failed count, partial count, and artifact locations.

#### Scenario: model-run evidence

WHEN a model candidate reaches a terminal state
THEN evidence includes forcing station count, canonical product counts, SHUD output URI, parsed row count, segment count, display product state, quality flags, Slurm job/accounting details, and residual blockers.

#### Scenario: bounded redacted evidence artifacts

WHEN scheduler or readiness evidence is written or read
THEN the payload is bounded, redacted, and stored under the configured evidence or workspace root
AND malformed, oversized, stale, mismatched, or unsafe evidence is recorded as blocked/release_blocked evidence rather than accepted success.

#### Scenario: deterministic evidence consumed by readiness validation

WHEN readiness validation consumes scheduler evidence from deterministic, dry-run, simulated, or production-like execution
THEN the resulting readiness item is non-final deterministic review evidence
AND `final_production_readiness_claimed` remains false unless every required live proof item is accepted.

#### Scenario: live scheduler receipt binding

WHEN scheduler evidence is presented as live production proof
THEN the live receipt must bind to the readiness run id, target environment, producer artifact reference, checksum or receipt id, schema, and live execution mode
AND stale, mismatched, or deterministic receipts remain release blockers.

### Requirement: Operations controls and validation

The production automation SHALL expose or provide operator controls for dry-run planning, retry, cancellation, and fast validation without requiring full live multi-cycle reruns.

#### Scenario: dry-run planning

WHEN an operator runs dry-run mode
THEN the scheduler reports selected candidates and skip/block reasons
AND it does not download data, submit Slurm jobs, run SHUD, or mutate hydro/met result tables.

#### Scenario: fast regression lane

WHEN CI or PR validation runs
THEN it uses deterministic fixtures and focused tests for discovery, idempotency, Slurm preflight/export, array partial states, and evidence formatting
AND full live GFS/IFS/SHUD multi-cycle execution remains opt-in
AND final production readiness remains false unless accepted live receipts satisfy the readiness proof contract.

#### Scenario: dry-run no mutation

WHEN dry-run mode is executed
THEN tests prove it does not download data, submit Slurm jobs, run SHUD, or mutate hydro/met result tables.

#### Scenario: diagnostic qhh scripts remain non-production evidence

WHEN docs or runbooks reference `scripts/run_qhh_continuous.py` or qhh-specific cycle scripts
THEN they identify those scripts as diagnostic or reproduction evidence
AND they identify the backend scheduler/orchestrator path as the production automation surface.

### Requirement: Bounded evidence observability floor

When the scheduler pass evidence payload exceeds the configured size bound and the bounded fallback shape is emitted, the artifact SHALL preserve an operator-readable observability floor — the true computed pass status, per-candidate summary rows, and a compact restart-reconcile block — without weakening the fail-closed top-level status contract or the hard size bound.

#### Scenario: pre-limit status is preserved inside the limit block

- WHEN the evidence payload exceeds `max_evidence_bytes` and the bounded fallback payload is written
- THEN the top-level `status` remains `resource_limit_blocked` and `limit.reason` remains `evidence_size_limit_exceeded`
- AND `limit.pre_limit_status` records the pass status computed before the fallback (the key is omitted when the source payload carried no status)
- AND downstream consumers of the top-level status require no change.

#### Scenario: candidate lists degrade to bounded summaries before being dropped

- WHEN the bounded fallback payload is constructed
- THEN `candidates`, `blocked_candidates`, and `skipped_candidates` are populated row-for-row with fixed-key summary rows carrying candidate identity (including the readiness-reader identity keys `source_id`, `cycle_time_utc`, `scenario_id`, and for admitted candidates `run_id` and `forcing_version_id`), status, reason, and the incident-critical candidate state-evidence subset (scheduler decision, missing-forcing repair status, journal-predecessor quarantined skip reason), each value passed through from the already-redacted source payload with keys absent from a row when the source value is absent or null
- AND `limit.candidate_lists` is `summarized`
- AND only if the summarized payload still exceeds the bound does the existing droppable tier empty the lists, progressively in field order and stopping as soon as the payload fits, so a partial drop can leave the later lists as summaries
- AND `limit.candidate_lists` is set to `dropped` only when that tier empties a candidate list that still held rows; emptying an already-empty candidate list drops nothing and the marker stays `summarized`
- AND the marker is monotone: once `limit.candidate_lists` is `dropped`, a later summarize pass SHALL NOT downgrade it back to `summarized` — empty candidate lists under a `dropped` marker mean rows were cut, and the marker keeps saying so
- AND the artifact never exceeds `max_evidence_bytes`, and a payload that cannot fit even after all degradation tiers still fails closed with the existing write error.

#### Scenario: restart-reconcile incident evidence survives the fallback compactly

- WHEN the source evidence payload carries a `restart_reconcile` block and the bounded fallback payload is constructed
- THEN the fallback retains a compact `restart_reconcile` block exposing its status, `reserved_unbound_error`, `inflight_error`, and per-outcome summary rows limited to job identity, action, status, reconciliation reason class, `identity_blocked_streak`, `quarantine_reason`, and `quarantine_field`
- AND when the source payload has no `restart_reconcile` block the fallback omits the key.

#### Scenario: within-limit evidence is byte-identical to the pre-change contract

- WHEN the evidence payload fits within `max_evidence_bytes`
- THEN the artifact carries full candidate detail and contains neither `limit.pre_limit_status` nor `limit.candidate_lists`.

#### Scenario: terminal limit compaction remains the fail-closed floor

- WHEN even the summarized-and-dropped payload exceeds the bound and the existing terminal limit-compaction tier rewrites the `limit` block to its reason-only form
- THEN `limit.pre_limit_status` and `limit.candidate_lists` are permitted to disappear with the rest of the compacted `limit` block, preserving the pre-existing fail-closed behavior unchanged.

### Requirement: No-progress convergence facts SHALL be readable from scheduler evidence

Scheduler evidence SHALL expose, for each reserved-unbound reconcile outcome, the identity-blocked consecutive-pass counter and the `identity_mismatch_released` action when a release occurs, in both the full-fidelity `restart_reconcile` block and the bounded (size-limited) compaction of that block. The reconcile proof aggregation SHALL count a release as a reserved-status durable write. The budget-demoted decision `blocked_strict_warm_start_init_state_mismatch` SHALL remain readable from the bounded candidate summary through the existing `decision` key. These are per-job convergence facts; cross-reason no-progress aggregation and alerting remain out of scope (tracked separately).

#### Scenario: Convergence facts survive bounded-evidence compaction

- **WHEN** a pass records identity-blocked outcomes (or a release) and the evidence payload exceeds the size limit so the bounded compaction applies
- **THEN** the compact `restart_reconcile` outcome rows still carry the consecutive-pass counter and the release action, and demoted candidates still show `blocked_strict_warm_start_init_state_mismatch` under the `decision` key

### Requirement: Bounded-evidence last-line invariants MUST have regression coverage

Two terminal bounded-evidence semantics SHALL be pinned by unit tests —
the terminal limit compaction retaining `limit.reason`, and the hard
evidence-size bound's exact boundary — exercising the real
production functions: a payload penetrating to the terminal
limit-compaction tier must still carry
`limit.reason == "evidence_size_limit_exceeded"` in the emitted
artifact, and the size-limit serializer must accept a payload of exactly
the configured byte bound while refusing one byte more. Weakening either
construct — dropping `reason` from the terminal keep-set, widening the
bound by one byte, or narrowing the acceptance to strictly below the
bound — SHALL fail the scheduler evidence test suite.

#### Scenario: Terminal compaction keeps the truncation marker

- **WHEN** a payload degrades past every earlier tier and the terminal
  limit-compaction rewrites the `limit` block to its reason-only form
- **THEN** the emitted artifact still carries
  `limit.reason == "evidence_size_limit_exceeded"`, and a keep-nothing
  terminal compaction fails the suite

#### Scenario: Hard bound accepts exactly the bound and refuses one byte more

- **WHEN** a payload serializes to exactly `max_evidence_bytes` bytes
- **THEN** the size-limit serializer accepts it, while a payload
  serializing to exactly one byte more is refused, so both an
  off-by-one widening and a `>=` narrowing of the comparison fail the
  suite

### Requirement: Slurm preflight tilde expansion never raises

The Slurm preflight storage-root checks SHALL expand a leading tilde in
received root values (in both the allowed-roots walk and the per-root
storage check) without ever letting the expansion escape the preflight as
an exception. This is a defence-in-depth guarantee at the helper level:
today's configuration layer cannot deliver a leading-tilde value to these
helpers (the db-backed arm fails earlier during configuration construction
and the db-free arm anchors the value at the working directory first), so
the requirement hardens the preflight against future callers and against
changes in that layer rather than closing a currently live escape.
Specifically: when the home directory cannot be determined (an unknown `~user`
prefix, or a plain `~` with no usable home-directory source), the unexpanded
value SHALL flow on as an ordinary path into the existing arms — the
allowed-roots walk's ENOENT tolerance arm (which admits a cwd-anchored
root with no blocker) and the per-root storage check's structured
containment/visibility verdicts — so the preflight always returns its
structured result instead of aborting the scheduling pass. Values whose tilde does expand, and values
without a tilde, keep their existing verdicts byte-for-byte — except the
recorded `./~/x` acceptance (a `.`-prefixed value whose first surviving
component is a tilde now takes the fail-closed cwd-anchored verdict at the
str-input storage check instead of expanding) — and no new blocker reason
is introduced.

#### Scenario: unexpandable tilde in allowed storage roots is tolerated without crashing the preflight

WHEN the allowed-roots walk receives a root value of `~nosuchuser/roots`
(or a plain `~/…` with no determinable home directory)
THEN `_slurm_preflight` returns its structured status/blockers result — no
RuntimeError escapes — and the affected root flows through the existing
ENOENT tolerance arm and is admitted as a cwd-anchored containment root
with no blocker (the existing arm never produces a blocker for
not-yet-existing roots; the resulting phantom-root geometry on this preflight
leg is tracked by #1627, the family-level ruling on whether an ENOENT
non-strict fallback must be loop-filtered, and is documented, not changed,
here — #1427 fixed the same geometry on the retry selector leg only)

#### Scenario: unexpandable tilde in a storage root field yields the existing check verdict

WHEN the per-root storage check receives an unexpandable tilde value for a
storage root field (workspace/object-store/log/runtime)
THEN the per-root storage check produces its existing structured
configured/contained/visible verdict without raising

#### Scenario: expandable and tilde-free roots keep their behavior

WHEN a configured root has no tilde or its tilde expands normally
THEN the preflight verdict is byte-for-byte identical to the pre-change
behavior, except the recorded `./~/x` acceptance at the str-input storage
check (fail-closed, pinned in the byte-compatibility carve-out)

