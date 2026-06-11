## ADDED Requirements

### Requirement: IFS probe failure classification

The system SHALL distinguish IFS source-cycle absence from compute-node DNS, network, timeout, or equivalent probe failures during availability discovery and source-cycle download preflight.

#### Scenario: Published cycle discovered through a mirror

WHEN IFS availability discovery probes a configured mirror for forecast hour 0
AND the mirror confirms the object exists
THEN the returned discovery has `available=true`
AND `status=discovered`
AND existing forecast-cycle upsert behavior is preserved.

#### Scenario: Source cycle genuinely unpublished

WHEN every configured IFS mirror responds with a not-found/unpublished result for the same source cycle
THEN the returned discovery has `available=false`
AND `status=unavailable`
AND `reason=source_cycle_unavailable`
AND `classifier=unavailable`
AND no forecast-cycle row is created for that cycle.

#### Scenario: All mirrors fail due to DNS or network

WHEN every configured IFS mirror probe fails due to DNS/name-resolution, network, timeout, or equivalent connectivity errors
THEN the returned discovery has `available=false`
AND `status=probe_failed`
AND `reason=source_cycle_probe_failed`
AND `classifier=network_error`
AND `retryable=true`
AND evidence includes bounded attempted mirrors with redacted URI, source, concrete error class, and redacted error message
AND evidence reports the total attempted count and omitted attempted count when configured sources exceed the emitted evidence limit.

#### Scenario: All mirrors are rate limited

WHEN every configured IFS mirror probe is rate limited
AND no configured mirror confirms the object exists
THEN the returned discovery has `available=false`
AND `status=rate_limited`
AND `reason=source_cycle_rate_limited`
AND `classifier=rate_limited`
AND `retryable=true`
AND evidence includes bounded redacted attempted mirror evidence.

#### Scenario: Mixed not-found and network failures

WHEN at least one configured IFS mirror reports not-found/unpublished
AND at least one configured mirror fails due to DNS/name-resolution, network, timeout, or equivalent connectivity errors
AND no configured mirror confirms the object exists
THEN the returned discovery has `status=probe_failed`
AND `reason=source_cycle_probe_failed`
AND `classifier=network_error`
AND `retryable=true`
AND evidence preserves both not-found and network/probe attempts.

#### Scenario: Forbidden source remains non-retryable

WHEN an IFS availability probe is forbidden by a configured source
THEN the returned discovery preserves the existing forbidden classification
AND `retryable=false`.

### Requirement: IFS probe evidence consumers

The system SHALL propagate IFS probe-failure classification through operator-facing CLI and scheduler evidence without leaking secrets.

#### Scenario: CLI reports network probe failure

WHEN `nhms-ifs download --cycle-time <cycle>` cannot confirm availability because all configured mirrors fail due to DNS/name-resolution, network, timeout, or equivalent connectivity errors
THEN the JSON payload reports `status=probe_failed`
AND `reason=source_cycle_probe_failed`
AND `classifier=network_error`
AND `retryable=true`
AND `files=0`
AND `total_bytes_written=0`
AND it includes redacted attempted mirror/error evidence.

#### Scenario: Scheduler treats network probe failure as retryable

WHEN scheduler/readiness evidence consumes an IFS cycle discovery classified as a network/probe failure
THEN the evidence remains retryable
AND the source cycle is not labeled as definitive data unavailable or converted to manual-only terminal evidence
AND operator-facing evidence includes the attempted mirrors and concrete redacted root cause.

#### Scenario: Evidence redaction

WHEN attempted mirror evidence contains URLs or error messages
THEN credentials, tokens, signed URL query values, and private secrets MUST be redacted before being emitted in adapter discovery, CLI JSON, scheduler evidence, or runbook examples.

#### Scenario: Evidence bounding

WHEN attempted mirror evidence contains many configured fallback sources or long exception strings
THEN adapter discovery and CLI JSON MUST emit only bounded redacted attempt entries
AND MUST preserve operator fields `source`, `uri`, `status`, `error_class`, and `error_message` for emitted attempts
AND MUST include total and omitted attempt counts.

#### Scenario: QHH diagnostic runner preserves typed probe state

WHEN the QHH diagnostic single-cycle script invokes `nhms-ifs download --cycle-time <cycle>`
AND the CLI emits `status=probe_failed` or `status=rate_limited` with non-zero exit
THEN the diagnostic state file records the typed retryable status, reason, classifier, retryable flag, source, cycle, and run identity
AND the diagnostic continuous runner MUST NOT overwrite that typed state as a generic failed cycle.
