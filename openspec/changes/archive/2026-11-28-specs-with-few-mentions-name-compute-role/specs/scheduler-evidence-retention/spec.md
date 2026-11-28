## MODIFIED Requirements

### Requirement: A systemd user timer drives retention

A `nhms-scheduler-evidence-retention.timer` and matching `nhms-scheduler-evidence-retention.service` unit SHALL exist at `infra/systemd/nhms-scheduler-evidence-retention.{service,timer}` on git master (sibling of the existing `infra/systemd/nhms-node27-raw-retention.{service,timer}` template) and SHALL be deployed to `~/.config/systemd/user/` on the compute control host and SHALL be enabled by the scheduler deployment procedure of the compute role.

The timer SHALL fire at most once per 24 hours (default cadence), with an initial delay avoiding overlap with `nhms-compute-scheduler.timer` peak activity.

#### Scenario: Timer is active after deployment

- **WHEN** the deployment procedure (`systemctl --user daemon-reload && systemctl --user enable --now nhms-scheduler-evidence-retention.timer`) is executed
- **THEN** `systemctl --user is-active nhms-scheduler-evidence-retention.timer` returns `active`
- **AND** `systemctl --user list-timers` shows a next-fire timestamp within 24 hours.

### Requirement: Retention receipt is emitted every run

Every retention timer fire SHALL produce a receipt JSON at `NHMS_SCHEDULER_EVIDENCE_ROOT/retention/retention-<utc-iso>.json` containing: `started_at`, `finished_at`, `total_before_bytes`, `total_after_bytes`, `deleted_count`, `deleted_paths` (list with pre-deletion mtime), `receipt_pass` (list of retention-receipt files deleted this run under the 180-day rule, empty on most runs), `skipped_count`, `skipped_paths_by_reason` (grouped by reason including `in-flight`, `safety-window`, `unrecognised`), `policy` (the resolved `retention_days`, `max_mb`, `receipt_retention_days`, and whitelist glob values).

If no deletions occurred, an empty-diff receipt SHALL still be written so operators can verify the timer ran. The retention script SHALL create the `retention/` subdirectory if it does not exist before writing the receipt so the very first fire on a fresh compute control host succeeds.

#### Scenario: Empty pass still emits receipt

- **WHEN** the retention timer fires and no artefact is over the age or size threshold
- **THEN** a retention receipt is written with `deleted_count: 0`, `total_before_bytes == total_after_bytes`, `receipt_pass: []`
- **AND** `finished_at` is set.

#### Scenario: First fire on fresh evidence root creates `retention/` subdir

- **WHEN** the retention timer fires for the first time on a node whose `NHMS_SCHEDULER_EVIDENCE_ROOT` exists but has never contained a `retention/` subdirectory
- **THEN** the script creates `<evidence-root>/retention/` before writing the receipt
- **AND** the receipt is written successfully with no `FileNotFoundError`.
