# Design: fix-raw-retention-execstartpre-mkdir

## Fixture Level

`low` — mechanical mirror of a landed, reviewed R1 fix (PR #1059) whose
shape, verification pattern, and behavior-lock test are already
established. No product-code behavior change, no schema, no runtime
path.

## Risk Triage

Selected risk pack: **systemd-install-invariant**.

- Established by #1059 R1 verifier finding: any `[Service]` block that
  writes `StandardOutput=append:PATH` or `StandardError=append:PATH`
  must be paired with `ExecStartPre=/usr/bin/mkdir -p <parent>` for
  every distinct parent, ordered before `ExecStart=`.
- Test evidence contract: unit-file `read_text()` substring assertion
  plus `startswith("ExecStartPre=")` / `startswith("ExecStart=")`
  line-index comparison, mirrored from
  `tests/test_node27_timeseries_compression.py:891-908`.

Reviewer packs (fixture=low reduction): **test-evidence** only.
Correctness / integration / spec-compliance / security-performance /
invariant-state packs are not selected because there is no code path,
no schema, no data flow, and no state transition — the change is a
pure systemd unit-file text delta plus its regression test plus a
runbook mkdir line addition. If the test-evidence reviewer surfaces a
correctness or integration concern from the diff, escalate at that
point.

## Must-Preserve Behavior

- `nhms-node27-raw-retention.service` `Type=oneshot`, `TimeoutStartSec=0`,
  `WorkingDirectory=/home/nwm/NWM`,
  `ExecStart=/home/nwm/NWM/scripts/node27_raw_retention_once.sh`,
  `StandardOutput=append:.../systemd.log`,
  `StandardError=append:.../systemd.err` all remain byte-identical.
- `nhms-node27-raw-retention.timer` is untouched.
- `scripts/node27_raw_retention_once.sh` is untouched; its internal
  `mkdir -p "$LOG_ROOT"` stays as a second line of defense for
  operator overrides of `$NODE27_RAW_RETENTION_LOG_ROOT`.

## Evidence Mapping

| Acceptance | Evidence |
|---|---|
| Service file has `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs` between `WorkingDirectory=` and `ExecStart=` | New pytest substring + line-order assertion |
| Runbook §Install step 1 mkdir line lists `~/node27-raw-retention-logs` | Runbook grep + strict `openspec validate` |
| No regression on wrapper or timer semantics | Existing raw-retention pytest suite passes unchanged |
| Ruff clean | `uv run ruff check .` |

## Non-goals

- No sweep of the other systemd units for the same pattern (though a
  future audit could be filed as a separate issue).
- No change to raw-retention wrapper, timer, or env catalog.

## Byte-Identity Discipline

The `ExecStartPre` line and log-dir path string
`/home/nwm/node27-raw-retention-logs` must be byte-identical across
three surfaces:

1. `infra/systemd/nhms-node27-raw-retention.service`
2. `docs/runbooks/tier-node27-timeseries-storage.md` §Install step 1
   mkdir line (as `~/node27-raw-retention-logs`, `~` expanded to
   `/home/nwm`)
3. `tests/test_node27_raw_retention.py` new regression test

The regression test asserts the code-side string, so the test acts as
the byte-identity lock for surface (1). Surface (2) is human-facing
runbook prose and does not need a programmatic lock beyond the runbook
grep in the manual verification floor.
