# node22-unit-failure-alert Specification

## Purpose
A failed node-22 oneshot unit becomes a mail alert through a generic `OnFailure=` template with node-22 absolute paths, so timer-driven probes and retention lanes cannot fail silently and a broken mail channel is itself visible.

## Requirements

### Requirement: A generic node-22 unit-failure alert template SHALL exist with absolute paths
The repository SHALL track `infra/systemd/nhms-node22-unit-failure-alert@.service` (user scope, `Type=oneshot`, `WorkingDirectory=/scratch/frd_muziyao/NWM`, `EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env`, `ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i`, `TimeoutStartSec=120`) and the handler `scripts/node22_unit_failure_alert_once.sh`, which reads at most 30 journal lines of the failed unit named by `%i`, composes one message and hands it to `$NHMS_FRONTIER_SENDMAIL`. With no `NHMS_ALERT_EMAIL_TO` the handler SHALL log `SKIPPED` and exit 0; when the transport is configured and returns non-zero the handler SHALL exit non-zero. The tracked example `infra/env/node22-unit-failure-alert.example` SHALL define exactly `NHMS_ALERT_EMAIL_TO`, `NHMS_ALERT_EMAIL_FROM`, `NHMS_FRONTIER_SENDMAIL` (example value `/usr/sbin/sendmail`), `NHMS_SMTP_HOST`, `NHMS_SMTP_PORT`, `NHMS_SMTP_USER`, `NHMS_SMTP_PASS` and no other key; `infra/env/README.md` authority table SHALL gain a row for it, and `infra/systemd/node22/MANIFEST.json` SHALL gain the template's entry so the drift probe does not report it as `untracked`. No node-22 unit or template SHALL contain the literal `%h/NWM`.

#### Scenario: Failed unit produces one mail
- **WHEN** the handler runs with `%i` set to a failed unit name, `NHMS_ALERT_EMAIL_TO` set and a fake transport that exits 0
- **THEN** exactly one message naming the unit and containing its last journal lines is handed to the transport and the handler exits 0

#### Scenario: Unconfigured recipient does not add a second failure
- **WHEN** the handler runs without `NHMS_ALERT_EMAIL_TO`
- **THEN** it logs `SKIPPED` and exits 0 without invoking the transport

#### Scenario: Failing transport is visible
- **WHEN** the transport exits 1
- **THEN** the handler exits non-zero

#### Scenario: Example key set is exact
- **WHEN** the test reads `infra/env/node22-unit-failure-alert.example`
- **THEN** the set of defined keys equals the seven named keys, `DATABASE_URL` is absent, and the manifest lists the template with its sha256

### Requirement: Tracked node-22 oneshot units SHALL be wired to the alert
Each of `nhms-node22-refresh-timer-health.service`, `nhms-node22-scheduler-stall-health.service`, `nhms-scheduler-evidence-retention.service`, `nhms-scheduler-file-provider-refresh.service`, `nhms-scheduler-journal-retention.service`, `nhms-scheduler-state-index-capacity.service`, `nhms-scheduler-state-index-prune.service` and `nhms-w3-journal-backup.service` under `infra/systemd/` SHALL carry exactly one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line in `[Unit]`. A test SHALL assert all of the following:

- the line occurs exactly once in each of these eight files;
- it occurs zero times in `nhms-slurm-gateway.service` and in every file under `infra/systemd/node22/`;
- the `MANIFEST.json` sha256 entries for the eight were updated in the same change.

#### Scenario: Six oneshots wired, long-running units untouched
- **WHEN** the unit-wiring test scans `infra/systemd/` (scenario title kept for archive continuity; since #3359 the set is eight)
- **THEN** each of the eight named oneshot units contains the `OnFailure=` line once
- **AND** the gateway unit and `node22/**` contain none
- **AND** the manifest sha256 of the eight matches their new content

### Requirement: node-22 SHALL reach the SMTPS shim only through the exact-interpreter wrapper
The repository SHALL track `scripts/node22_smtp_sendmail.sh`, an executable sendmail-compatible transport that execs `/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py` with its own arguments, standard input and exit status passed through unchanged. When the interpreter is missing or not executable, or the shim file is missing, the wrapper SHALL write one `node22-smtp-sendmail:` line to standard error and exit 64 without invoking any other interpreter. The wrapper SHALL NOT invoke `python3`, `uv`, or read or print any `NHMS_SMTP_*` value.

#### Scenario: Message is forwarded to the shim
- **WHEN** the wrapper runs as `<wrapper> -t -i` with a message on stdin and the interpreter and shim exist
- **THEN** the interpreter receives the shim path followed by `-t -i`, the stdin bytes unchanged, and the wrapper exits with the interpreter's exit status

#### Scenario: Missing interpreter fails closed
- **WHEN** the interpreter path is absent or not executable
- **THEN** the wrapper prints `node22-smtp-sendmail: INTERPRETER_MISSING` to stderr and exits 64 without running anything else

#### Scenario: Non-executable interpreter fails closed
- **WHEN** the interpreter path exists but is not executable
- **THEN** the wrapper prints `node22-smtp-sendmail: INTERPRETER_MISSING` to stderr and exits 64 without running anything else

#### Scenario: Missing shim fails closed
- **WHEN** the interpreter exists but the shim file is absent
- **THEN** the wrapper prints `node22-smtp-sendmail: SHIM_MISSING` to stderr and exits 64 without running anything

#### Scenario: Shim constant names a tracked file
- **WHEN** the test strips `/scratch/frd_muziyao/NWM/` from the wrapper's `SHIM` constant
- **THEN** the remainder is a git-tracked, existing repository file (`scripts/node27_frontier_smtp_sendmail.py`)

### Requirement: A hang of the w3 journal backup SHALL become a unit failure, and a missing source SHALL stay a skip
`infra/systemd/nhms-w3-journal-backup.service` SHALL set `TimeoutStartSec=` in `[Service]` to a whole number of seconds greater than zero and small enough that three times the value stays below the period of `nhms-w3-journal-backup.timer` (one hour): systemd arms the start timeout anew for each of the unit's three commands (`ExecCondition=`, `ExecStart=`, `ExecStartPost=`), so only that bound makes an `rsync` blocked on the `hard` `/ghdc/data` mount end as a failed unit before the next tick. The unit SHALL keep its `ExecCondition=` on the local journal directory: when that directory is absent the unit is skipped, `rsync --delete` does not run, and no alert is sent. A test SHALL assert the timeout bound and that the unit's non-comment lines are exactly the lines deployed before this change plus the `OnFailure=` line in `[Unit]` and the `TimeoutStartSec=` line in `[Service]`.

#### Scenario: The timeout is bounded below the timer period
- **WHEN** the test reads `[Service]` of `nhms-w3-journal-backup.service`
- **THEN** it finds exactly one `TimeoutStartSec=` whose value is an integer number of seconds with `0 < 3 × value < 3600`

#### Scenario: The skip condition and the copy command are unchanged
- **WHEN** the test compares the unit with the copy deployed before this change
- **THEN** the non-comment lines of each section, in order, are the earlier lines with the `OnFailure=` line added to `[Unit]` and the `TimeoutStartSec=` line added to `[Service]`, so `ExecCondition=`, `ExecStart=` and `ExecStartPost=` are byte-identical
