# scheduler-evidence-retention Specification

## Purpose

Bounded retention of `NHMS_SCHEDULER_EVIDENCE_ROOT` artefacts on node-22 via a systemd user timer, so the scheduler evidence directory does not grow without limit as timing instrumentation increases per-pass artefact size.

## ADDED Requirements

### Requirement: A systemd user timer drives retention

A `nhms-scheduler-evidence-retention.timer` and matching `nhms-scheduler-evidence-retention.service` unit SHALL exist at `infra/systemd/nhms-scheduler-evidence-retention.{service,timer}` on git master (sibling of the existing `infra/systemd/nhms-node27-raw-retention.{service,timer}` template) and SHALL be deployed to `~/.config/systemd/user/` on node-22 and SHALL be enabled by the node-22 scheduler deployment procedure.

The timer SHALL fire at most once per 24 hours (default cadence), with an initial delay avoiding overlap with `nhms-compute-scheduler.timer` peak activity.

#### Scenario: Timer is active after deployment

- **WHEN** the deployment procedure (`systemctl --user daemon-reload && systemctl --user enable --now nhms-scheduler-evidence-retention.timer`) is executed
- **THEN** `systemctl --user is-active nhms-scheduler-evidence-retention.timer` returns `active`
- **AND** `systemctl --user list-timers` shows a next-fire timestamp within 24 hours.

### Requirement: Retention policy is age-then-size

The retention script SHALL delete artefacts older than `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` (default `90`).

After the age pass, if the directory total size still exceeds `NHMS_SCHEDULER_EVIDENCE_MAX_MB` (default `512`), the script SHALL delete the oldest remaining artefacts (by mtime) until the total size is within the cap.

Deletions SHALL be logged and summarised in a retention receipt JSON written to the same evidence root under `retention/retention-<utc-iso>.json`. The script SHALL create `<evidence-root>/retention/` if absent before writing the receipt.

#### Scenario: Age-based deletion under cap

- **WHEN** the evidence directory contains files with mixed ages and total size below the cap
- **THEN** the retention script deletes only files older than the retention-days threshold
- **AND** the retention receipt lists each deleted path with its pre-deletion mtime.

#### Scenario: Size-based eviction after age pass

- **WHEN** the directory total size still exceeds `NHMS_SCHEDULER_EVIDENCE_MAX_MB` after the age pass
- **THEN** the retention script deletes oldest-first until the total is within the cap
- **AND** the retention receipt records both the age-pass and size-pass deletion sets separately.

### Requirement: Retention MUST NOT touch in-flight or foreign artefacts

The retention script SHALL only consider files under `NHMS_SCHEDULER_EVIDENCE_ROOT` that match one of the following three explicit whitelist branches; every other file SHALL be logged as `skipped: unrecognised`:

1. **Scheduler pass evidence** — matches the actual on-disk names produced by `services/orchestrator/scheduler_evidence.py:294-295,356`: `<pass_id>.json` and `<pass_id>.pre_execution.json`, where `pass_id` is the string constructed at `services/orchestrator/scheduler_runtime.py:387` `f"scheduler_{format_cycle_time(started_at)}_{uuid4().hex[:12]}"` (always begins with the literal prefix `scheduler_`). In practice the two on-disk shapes are `scheduler_<cycle>_<hex12>.json` and `scheduler_<cycle>_<hex12>.pre_execution.json`; both SHALL be equally subject to the age + size policy. Fallback `evidence_write_error.json` artefacts emitted at `services/orchestrator/scheduler_runtime.py:991` on evidence-write failure are declared **out of scope** for retention (kept indefinitely so operators always have a trail of write-side incidents).
2. **Retention receipts** — files matching `retention/retention-*.json` (this subdirectory holds this script's own receipts, see the "Retention receipt is emitted every run" Requirement). Receipts SHALL be subject to a separate longer retention (default 180 days) so retention actions remain auditable. If receipts themselves are older than 180 days the retention script SHALL delete them oldest-first during a receipt-specific pass recorded in the same run's receipt (`policy.receipt_retention_days`).
3. **Explicitly whitelisted names via env override** — `NHMS_SCHEDULER_EVIDENCE_RETENTION_WHITELIST_GLOBS` (colon-separated `fnmatch` patterns) lets operators opt additional artefact families into the retention passes without redefining hardcoded classes.

The script SHALL skip any file that has a sibling `.tmp` or `.lock` file (indicating an in-flight write), and any file whose age is under 1 hour regardless of policy thresholds.

#### Scenario: In-flight `pre_execution.json` write is preserved

- **WHEN** the retention script encounters `scheduler_<pass_id>.pre_execution.json` alongside `scheduler_<pass_id>.pre_execution.json.tmp`
- **THEN** neither file is deleted regardless of age
- **AND** the retention receipt records the file as `skipped: in-flight`.

#### Scenario: Newer-than-safety-window is preserved

- **WHEN** the retention script encounters an artefact with mtime younger than one hour
- **THEN** the file is not deleted regardless of size cap
- **AND** the retention receipt records the file as `skipped: safety-window`.

#### Scenario: Foreign file untouched

- **WHEN** the evidence root contains a file not matching any whitelist branch (e.g. an operator's `notes.txt`)
- **THEN** the file is not touched
- **AND** the retention receipt records the path as `skipped: unrecognised`.

#### Scenario: `pre_execution.json` artefacts are subject to retention

- **WHEN** a `scheduler_<pass_id>.pre_execution.json` file is older than `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` and has no sibling `.tmp` and is not younger than 1 hour
- **THEN** the retention script deletes it during the age pass just like its sibling `scheduler_<pass_id>.json`
- **AND** the retention receipt records both paths under `deleted_paths`.

#### Scenario: Retention receipts have a longer window

- **WHEN** a retention run finds a `retention/retention-*.json` receipt older than 180 days
- **THEN** the retention script deletes it during a receipt-specific pass
- **AND** the current run's receipt records the deletion under a `receipt_pass` bucket separate from `deleted_paths` and reports the resolved `policy.receipt_retention_days`.

#### Scenario: `evidence_write_error.json` is out of scope

- **WHEN** the retention script encounters a fallback `evidence_write_error.json` artefact emitted by the scheduler on evidence-write failure
- **THEN** the file is not deleted regardless of age or size cap
- **AND** the retention receipt records the path as `skipped: unrecognised` (or an equivalent out-of-scope reason, per the whitelist rule above).

### Requirement: Retention receipt is emitted every run

Every retention timer fire SHALL produce a receipt JSON at `NHMS_SCHEDULER_EVIDENCE_ROOT/retention/retention-<utc-iso>.json` containing: `started_at`, `finished_at`, `total_before_bytes`, `total_after_bytes`, `deleted_count`, `deleted_paths` (list with pre-deletion mtime), `receipt_pass` (list of retention-receipt files deleted this run under the 180-day rule, empty on most runs), `skipped_count`, `skipped_paths_by_reason` (grouped by reason including `in-flight`, `safety-window`, `unrecognised`), `policy` (the resolved `retention_days`, `max_mb`, `receipt_retention_days`, and whitelist glob values).

If no deletions occurred, an empty-diff receipt SHALL still be written so operators can verify the timer ran. The retention script SHALL create the `retention/` subdirectory if it does not exist before writing the receipt so the very first fire on a fresh node-22 succeeds.

#### Scenario: Empty pass still emits receipt

- **WHEN** the retention timer fires and no artefact is over the age or size threshold
- **THEN** a retention receipt is written with `deleted_count: 0`, `total_before_bytes == total_after_bytes`, `receipt_pass: []`
- **AND** `finished_at` is set.

#### Scenario: First fire on fresh evidence root creates `retention/` subdir

- **WHEN** the retention timer fires for the first time on a node whose `NHMS_SCHEDULER_EVIDENCE_ROOT` exists but has never contained a `retention/` subdirectory
- **THEN** the script creates `<evidence-root>/retention/` before writing the receipt
- **AND** the receipt is written successfully with no `FileNotFoundError`.

### Requirement: Retention is independently disabling and rollback-safe

The retention timer SHALL be `systemctl --user disable --now nhms-scheduler-evidence-retention.timer` -disable-able without affecting `nhms-compute-scheduler.timer` or `nhms-compute-scheduler.service`.

#### Scenario: Disabling retention leaves scheduler running

- **WHEN** an operator runs `systemctl --user disable --now nhms-scheduler-evidence-retention.timer`
- **THEN** `nhms-compute-scheduler.timer` remains active
- **AND** no scheduler pass is interrupted
- **AND** the evidence root continues to grow with subsequent pass artefacts (retention having been the only cap).
