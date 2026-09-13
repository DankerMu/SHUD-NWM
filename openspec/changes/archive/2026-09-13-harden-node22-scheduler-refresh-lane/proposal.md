## Why

Four open issues share one production lane — the node-22 DB-free scheduler
file-provider refresh — and each one leaves that lane able to fail silently:

- **#2041 / #2146**: `nhms-scheduler-file-provider-refresh.timer` can sit at
  `UnitFileState=enabled` + `ActiveState=inactive` with `NEXT=-`. It looks green,
  never fires, and nothing in the repo notices. That exact state ran from
  2026-08-28 00:11 CST to the 2026-09-08 operator recovery; at the 168-hour
  freshness line compute went `file_manifest_stale` -> `db_free_registry_blocked`
  and journal retention went `frontier_block_missing`. There is no detector.
- **#1926**: with direct-grid authority and a worker registry mirror configured,
  `--dry-run` reports the mirror as 0 entries, fails its own receipt
  self-validation, and exits `primary_receipt_failed`. The operator's read-only
  rehearsal path is unusable exactly on the production configuration.
- **#2075**: the only tracked source for the node-22 DB-free scheduler env
  omits `NHMS_ORCHESTRATOR_TERMINAL_STAGE`, which `infra/env/README.md:283-285`
  and `docs/runbooks/current-production-ops.md:168-172` both declare mandatory. Rebuilding
  from the template yields `terminal_stage=None`, which runs the chain into the
  `publish` stage and fails `DATABASE_URL_MISSING` every cycle on a DB-free node.

## What Changes

- Add a read-only, DB-free timer-health probe for the refresh lane that grades
  `is-enabled`, `is-active`, `list-timers NEXT`, and published-manifest age as
  four independent signals, writes a receipt, and exits non-zero well before the
  168-hour consumer limit. Detection only: the probe performs no unit mutation
  and no self-heal.
- Add the probe's user-scope service/timer units plus install/rollback steps.
- Fix the dry-run provider-evidence assembly so the worker mirror's
  `entry_count` carries the same prospective model count as the canonical
  registry, letting a successful dry-run produce a self-valid `outcome=dry_run`
  receipt.
- Add `NHMS_ORCHESTRATOR_TERMINAL_STAGE` and `NHMS_REQUIRE_FORECAST_WARM_START`
  to `infra/env/compute.scheduler-dbfree.env.example`, promote the docs' "must
  set" prose into a machine-readable required-key block, and add a guard test
  that the template satisfies every entry — key and pinned value.
- Settle #2075's gating question on #2069: because the live node-22 env already
  sets `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`, PR #2072 was a
  behaviour-equivalent move of the canonical-precip mirror gate, not its first
  activation.
- Record `enabled` + `inactive` as a failure state in the runbook, pair the
  #1104 manual-publisher `stop`/`start` window, and capture the 08-28/09-08
  episode as a worked counter-example.

## Non-Goals

- No change to the 168-hour freshness bound, `file_manifest_stale`,
  `db_free_registry_blocked`, or `frontier_block_missing` fail-closed semantics.
- No self-heal: the probe never runs `start`/`stop`/`enable`/`disable`/`restart`.
- No off-host alert routing: the alert is node-22-local (a failed user unit plus
  a local receipt). Mail/paging is a stated known limit, filed as a follow-up.
- No mutation of `nhms-compute-scheduler.{timer,service}`.
- No re-run of the completed 2026-09-08 one-time recovery; the refresh timer is
  already `enabled` + `active` and must stay that way.
- No change to non-dry-run publish ordering, CAS, rollback, or receipt semantics.
- No change to the cutover gate (#1720), the Python 3.11 pin (#1831), journal
  retention enablement (#2119), or the canonical-precip mirror gate (#2069).

## Capabilities

- `scheduler-registry-refresh`
