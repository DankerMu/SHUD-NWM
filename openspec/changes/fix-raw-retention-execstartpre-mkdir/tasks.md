# Tasks: fix-raw-retention-execstartpre-mkdir

## 1. Systemd unit + runbook + regression test

- [x] 1.1 Insert `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`
  into `infra/systemd/nhms-node27-raw-retention.service` between
  `WorkingDirectory=` and `ExecStart=`, byte-identical in shape to
  `infra/systemd/nhms-node27-timeseries-compression.service:7` and
  `infra/systemd/nhms-node27-autopipe.service:8`.
  Evidence floor: `uv run pytest -q tests/test_node27_raw_retention.py`
  passes with the new unit-file assertion; `uv run ruff check .` clean.

- [x] 1.2 Append `~/node27-raw-retention-logs` to
  `docs/runbooks/tier-node27-timeseries-storage.md` §Install step 1
  mkdir line so a clean-node bringup covers the log directory before
  the timer wakes.
  Evidence floor: runbook grep shows the token; the runbook cross-link
  targets touched by #857 remain intact.

- [x] 1.3 Add a pytest regression test to
  `tests/test_node27_raw_retention.py` asserting the unit file contains
  the `ExecStartPre` mkdir substring, the `StandardOutput=append:`
  substring, and enforces line-order `ExecStartPre` < `ExecStart`.
  Mirror
  `tests/test_node27_timeseries_compression.py::test_timeseries_compression_service_bootstraps_log_dir`.
  Evidence floor: test passes locally.

## 2. Change-level verification floor

- [x] 2.1 `openspec validate fix-raw-retention-execstartpre-mkdir
  --strict --no-interactive` PASS.
- [x] 2.2 `uv run ruff check .` PASS.
- [x] 2.3 `uv run pytest -q tests/test_node27_raw_retention.py` PASS
  including the new regression test.
