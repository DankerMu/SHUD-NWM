# Spec Delta: timeseries-db-retention

## ADDED Requirements

### Requirement: The §8.6 freed_bytes-0 triage procedure MUST select a unique tick bracket and its grep anchor MUST be derived

Runbook §8.6 item 5 SHALL instruct the operator to select the tick bracket
by correlating the receipt's `generated_at` timestamp with the bracket's
`start`/`done` timestamps (both are UTC ISO-8601 from the wrapper's `ts()`),
SHALL state that the shipped env pins the receipt path so the path alone
cannot discriminate ticks, SHALL warn that a `start` line without a matching
`done rc=` line (tick in flight, or wrapper died mid-tick) brackets a tick
that wrote no receipt and must not be read, SHALL record the
refuse-then-retry misread window (prior refused tick's warning vs this
tick's genuine 0) with its conservative direction scoped to that window, and
the test-side §8.6 grep anchor SHALL be derived from the grep token so a
rename campaign cannot leave the fence and the runbook stale but
self-consistent.

#### Scenario: Bracket selection is unique under the shipped env

- **WHEN** an operator follows §8.6 item 5 against a cumulative
  `retention.log` produced with the shipped fixed receipt path
- **THEN** the text SHALL direct them to the bracket whose `start`/`done`
  timestamps contain the receipt's `generated_at`, SHALL state that the
  receipt path is fixed by the shipped env and cannot be used alone as the
  tick key, and SHALL warn that a `start` without a matching `done rc=` (or
  a receipt-less `rc=2` config-refused tick) is not the bracket to read

#### Scenario: Refuse-then-retry window is documented

- **WHEN** a prior tick refused with `RETENTION_DROP_FAILED` after warning
  about a chunk that the current tick genuinely measures as 0 and drops
- **THEN** §8.6 item 5 SHALL state that the in-`dropped_chunks[]` criterion
  does not exclude that stale warning and that within THIS window the
  misread direction is conservative (an extra reconciliation)

#### Scenario: Grep fence derives from the token

- **WHEN** `_MEASURE_WARNING_GREP_TOKEN` is renamed as part of a warning
  rename campaign without touching §8.6's grep command line
- **THEN** `test_measure_warning_byte_identical_with_runbook` SHALL fail,
  because `_MEASURE_WARNING_GREP_FENCE` is an f-string derivation of the
  token rather than an independent literal
