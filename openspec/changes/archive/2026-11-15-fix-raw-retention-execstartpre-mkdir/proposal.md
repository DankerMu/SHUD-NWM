# Fix nhms-node27-raw-retention.service missing ExecStartPre mkdir

## Why

`infra/systemd/nhms-node27-raw-retention.service:8-9` declares
`StandardOutput=append:/home/nwm/node27-raw-retention-logs/systemd.log`
and the equivalent `StandardError`, but the `[Service]` section has no
`ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`.

Per `systemd.exec(5)`, `append:PATH` opens the file with
`O_CREAT|O_APPEND|O_WRONLY` — the file is created if missing, but the
**parent directory is not**. If the parent directory does not exist,
systemd fails to open the standard-output fd before forking `ExecStart`,
so the unit fails with `Failed to open standard output` and never runs.
The wrapper `scripts/node27_raw_retention_once.sh:55` does contain
`mkdir -p "$LOG_ROOT"`, but it lives inside `ExecStart` — it runs after
systemd has already tried (and failed) to open the log fd. It cannot
rescue a pre-fork fd-setup failure.

Node-27 today does not exhibit the failure because operations manually
`mkdir`-ed the log directory during the #849 deployment window (tribal
knowledge, undocumented). Any clean-node bringup — DR restore,
migration, second-cluster deployment — will hit ENOENT on first `start`,
and when the timer wakes the failure surfaces as a silent systemd
journal entry.

The same latent trap on `nhms-node27-timeseries-compression.service`
was closed in PR #1059 (issue #853); `nhms-node27-autopipe.service`
followed the correct pattern from inception. `raw-retention` is the
last unclosed sibling.

## What Changes

- `infra/systemd/nhms-node27-raw-retention.service` gains an
  `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`
  line between `WorkingDirectory=` and `ExecStart=`, byte-identical
  in shape to the compression and autopipe siblings.
- `docs/runbooks/tier-node27-timeseries-storage.md` §Install step 1
  mkdir line gains `~/node27-raw-retention-logs` (operator convenience;
  the runbook is the de-facto node-27 user-level timer install
  entrypoint).
- `tests/test_node27_raw_retention.py` gains a unit-file regression
  test asserting the `ExecStartPre` mkdir substring, the
  `StandardOutput=append:` substring, and the line-order invariant
  `ExecStartPre` line-index < `ExecStart` line-index — mirrors
  `tests/test_node27_timeseries_compression.py::test_timeseries_compression_service_bootstraps_log_dir`.

## Out of Scope

- `scripts/node27_raw_retention_once.sh:55` in-`ExecStart` `mkdir`
  stays — it is a second line of defense for non-default
  `$NODE27_RAW_RETENTION_LOG_ROOT` overrides.
- Existing on-disk `/home/nwm/node27-raw-retention-logs/` on node-27 is
  not touched.
- No timer, unit binding, or wrapper semantics change.
- No systematic sweep of all `infra/systemd/*.service` for the same
  pattern — routed as a separate concern; #1060 is one observation,
  one fix (per issue-scribe discipline).
