## Why

Four independent node-27 operations signals are broken in ways that were each observed on the live host (issues #1766, #1712, #1765, #1647):

- #1766: runbook §8.6 item 7 escalates "retention stopped progressing" by counting `lock-contention(` in `retention.log`, but the two real refused ticks (08-21, 08-22) were `57014` statement-timeout refusals that never carry that marker, so the criterion counts 0 while progress is 0.
- #1712: the retention unit's `OnFailure=` mail arrives without `refusal_reason`, because the wrapper swallows runner stderr into `retention.log` (`2>&1`) and the unit sends stderr to a file (`StandardError=append:`), so `journalctl -n 30` — the only thing the deliberately dumb handler quotes — has nothing lane-specific in it.
- #1765: a two-day pytest run filled `/tmp/pytest-of-nwm` (27 GB) on the 98 GB root volume; the resource-governance audit saw the critical root-free signal and still exited 0 with no `OnFailure=`; its own lock lives on the disk that was full; the human capacity check (`df -h /home`) never looks at `/`.
- #1647: every `psycopg2.connect` in `scripts/node27_autopipeline.py` has no `connect_timeout` and no `statement_timeout`, so a hung backend wedges the 10-minute tick under the flock forever; the compression runner interpolates a chunk name into `ANALYZE` with naive quoting; `NODE27_AUTOPIPE_STATS_GUARD` recognises only the literal `off`.

## What Changes

- #1766: the escalation criterion counts drop-phase refusals (`RETENTION_DROP_FAILED`) regardless of SQLSTATE; runbook §8.6 item 6/7 command + wording, and an anchor test that pins the counted line shape for 57014, 55P03 and 40P01 inputs. No new wire code.
- #1712: `scripts/node27_timeseries_retention_once.sh` pipes the runner through `tee -a "$LOG_FILE" >&2` and takes `RC` from `PIPESTATUS[0]`; `infra/systemd/nhms-node27-timeseries-retention.service` switches `StandardError=` to `journal` (stdout stays `append:`); the retention unit's `systemd.err` file lane is retired (no runbook/checklist `tail … systemd.err` command exists in the tree; the runbook gains the `journalctl --user -u` command); §8.6 item 8 KNOWN LIMITATION is rewritten.
- #1765: `pyproject.toml` gains `tmp_path_retention_policy = "failed"`; node-27 gets `TMPDIR=/home/nwm/tmp` as a documented host discipline (runbook + bringup checklist), never `--basetemp` in shared config; `scripts/node27_resource_governance.py` exits non-zero when any recommendation is `critical` and prints `RESOURCE_GOVERNANCE_CRITICAL:<code>` lines to stderr; its unit gains `OnFailure=nhms-node27-unit-failure-alert@%n.service` and the same tee/journal geometry; its lock moves out of `/tmp`; `/` joins the capacity-check discipline in `instructions/agents/shared.md` (regenerated CLAUDE.md/AGENTS.md), `docs/runbooks/current-production-ops.md`, `docs/runbooks/node-27-bringup-checklist.md`.
- #1647: the single `_connect` helper in `scripts/node27_autopipeline.py` applies `connect_timeout=10` (DSN `connect_timeout` wins when present) and a 600 s statement timeout (Python-caller override only; the stats guard keeps `STATS_GUARD_TIMEOUT_MS`) to every connection; `scripts/node27_timeseries_compression.py::qualified_chunk` refuses non-identifier chunk names fail-closed (byte-identical mirror of `_STATS_GUARD_IDENT_RE`, `^[A-Za-z0-9_]+$`); the stats-guard flag accepts `{0,false,no,off}` case/whitespace-insensitively.
- Workflow housekeeping: `.worktrees/` added to `.gitignore`.

## Capabilities

**Modified Capabilities**
- `timeseries-db-retention` — escalation criterion, journal-visible refusal reason, governance critical exit.
- `frontier-chunk-statistics-freshness` — autopipeline connection timeouts, stats-guard flag parsing.
- `hypertable-compression` — fail-closed chunk identifier helper in the compression runner (no interpolating consumer today; defence in depth).
- `python-environment-truth` — bounded pytest temporary directories.

## Impact

- Code: `scripts/node27_timeseries_retention_once.sh`, `scripts/node27_resource_governance_once.sh`, `scripts/node27_resource_governance.py`, `scripts/node27_autopipeline.py`, `scripts/node27_timeseries_compression.py`, `pyproject.toml`, `.gitignore`.
- Units: `infra/systemd/nhms-node27-timeseries-retention.service`, `infra/systemd/nhms-node27-resource-governance.service` (re-install + `daemon-reload` on node-27 is a post-merge manual step).
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md` §8.6, `docs/runbooks/current-production-ops.md`, `docs/runbooks/node-27-bringup-checklist.md`, `instructions/agents/shared.md` + regenerated `CLAUDE.md`/`AGENTS.md`.
- Tests: `tests/test_node27_timeseries_retention.py`, `tests/test_node27_resource_governance.py`, `tests/test_node27_autopipeline*.py`, `tests/test_node27_timeseries_compression.py`, wrapper shell tests.
- Behavior NOT changed: H5 fail-closed, `lock_timeout` values, timer schedules, the #1643 stats-guard observation semantics, receipt schemas.
