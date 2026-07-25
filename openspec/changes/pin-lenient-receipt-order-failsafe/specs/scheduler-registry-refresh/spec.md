# Spec Delta: scheduler-registry-refresh

## ADDED Requirements

### Requirement: The lenient receipt-order reader SHALL fail safe to None on any malformed payload so a corrupted latest.json never bricks the next publish

`_lenient_receipt_order` SHALL return `None` — never raise — for any payload that is not a Mapping, lacks a non-empty string `run_id`, or carries a missing or unparsable `started_at`; for a valid payload it SHALL return a `(started_at, run_id)` tuple with a timezone-aware datetime; and `_publish_primary_receipt` SHALL treat a `None` order (including one caused by undecodable or non-JSON `latest.json` bytes) as `replace_latest = True`, publishing the new receipt successfully. These semantics are pinned by direct regression tests.

#### Scenario: Malformed payload shapes return None, not an exception

- **WHEN** `_lenient_receipt_order` is called with a non-Mapping payload, a
  Mapping whose `run_id` is missing, empty, or not a string, or a Mapping
  whose `started_at` is missing or unparsable
- **THEN** it SHALL return `None` without raising

#### Scenario: Valid payload yields a timezone-aware order tuple

- **WHEN** `_lenient_receipt_order` is called with a Mapping carrying a
  non-empty string `run_id` and an ISO-8601 `started_at` with timezone
- **THEN** it SHALL return `(started_at, run_id)` with the datetime
  timezone-aware (normalized to UTC)

#### Scenario: Corrupted latest.json does not brick the next publish

- **WHEN** `latest.json` on disk contains undecodable or non-JSON bytes and
  `_publish_primary_receipt` is called with a valid new receipt
- **THEN** the publish SHALL succeed and `latest.json` SHALL contain the new
  receipt's canonical bytes
