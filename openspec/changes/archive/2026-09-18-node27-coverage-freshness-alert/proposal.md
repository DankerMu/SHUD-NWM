## Why
`hydro.run_display_coverage` has no freshness observer anywhere in the repo (#2080). Both coverage-refresh call sites are non-fatal, the autopipe unit carries no `OnFailure=`, and the only stall alerter (`scripts/node27_frontier_stall_alert.py`) reads `hydro.hydro_run` alone — during a coverage stall the ingest frontier keeps advancing, so its directional-progress criterion is reset every tick and it is constructively silent. Since #2009 bounded national discharge discovery by `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`, a coverage stall no longer degrades to stale-but-online: the covered cycles age out of the window and the national layer goes dark (`default_cycle=null`, `valid_times=[]`) with zero alerts.

## What Changes
- Add `scripts/node27_coverage_freshness_alert.py`: a read-only, stateless oneshot that compares, per source, the display-ready `hydro_run` frontier against the national catalog's own newest listed cycle — obtained by calling `services/tiles/mvt.py` `national_discharge_cycles` rather than re-deriving its predicate, so the observer cannot drift from the surface it watches — and exits non-zero when the gap exceeds a threshold derived from `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`.
- Add `infra/systemd/nhms-node27-coverage-freshness-alert.{service,timer}`, carrying `OnFailure=nhms-node27-unit-failure-alert@%n.service` so the existing mail channel delivers the alert; reuse `infra/env/node27-frontier-alert.env`.
- Add the freshness-alert requirement to the existing `display-coverage-freshness` capability; no new capability.
- Document the lane and its division of labour with `frontier-stalled` in `docs/runbooks/current-production-ops.md`.
- Adjudicate the open question: both coverage-refresh call sites stay non-fatal and gain no new persisted failure signal (rationale in `design.md`).

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- `display-coverage-freshness`: gains a freshness-alert requirement on top of the existing refresh semantics.

## Impact
New script `scripts/node27_coverage_freshness_alert.py`, new tests `tests/test_node27_coverage_freshness_alert.py`, two new systemd units, one runbook section, one spec delta. No change to `services/tiles/mvt.py`, `packages/common/display_coverage.py`, `scripts/node27_autopipeline.py` or `scripts/node27_autopipe_cron.sh`. Installing/enabling the timer on node-27 is a deployment action performed as part of this issue's live receipt.

Issue type: feature
Fixture level: expanded
Upstream suggested level: absent (hand-written issue; expanded is mandatory — new script entrypoint plus production systemd config)
Repair intensity: high (production config + new alerting entrypoint + shared display discovery contract)
Blast radius: node-27 operator alerting lane; read-only against the production DB
Selected risk packs: Public API / CLI / script entry; Config / project setup; Schema / columns / units / field names; Auth / permissions / secrets; Concurrency / shared state / ordering; Resource limits / large input / discovery; Error handling / rollback / partial outputs; Documentation / migration notes; Domain: operator alerting lanes / display discovery contract parity
Evidence floor: targeted pytest on the injected observation seam (gap, no-gap, ingest-stall, no-covered-cycle, window filter, null-source exclusion, report truncation, config-invalid, observation-failure, DSN redaction), `uv run ruff check .`, `openspec validate node27-coverage-freshness-alert --strict --no-interactive`, node-27 live receipt (read-only run against the production DB + scratch-DB gap→alert→clear→recover through the real systemd `OnFailure=` wiring).
