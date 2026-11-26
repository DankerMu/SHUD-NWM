# Retire the unused compute API unit (#3394, slice 2)

## Why

`infra/systemd/node22/nhms-compute-api.service` starts the full API in the `compute_control` role on
`0.0.0.0:8001` of the compute control host. Read on that host on 2026-11-26: the unit file is installed,
`disabled`, `inactive`, with no activation since the user manager started; nothing listens on 8001; no
other installed unit names it. Nothing calls an API on that host: the scheduler talks to the standalone
Slurm gateway (`nhms-slurm-gateway.service`, loopback 8090), and the display plane on node-27 reads NFS
and its own database.

After slice 1 (#3400) the only routes the `compute_control` role has and the display role lacks are the
four Slurm routes, which the gateway unit serves on its own. A tracked, installable unit that binds the
full API to every interface, for no caller, is a way to start something by mistake.

## What changes

- **Deleted**: `infra/systemd/node22/nhms-compute-api.service` and its entry in
  `infra/systemd/node22/MANIFEST.json` (25 entries to 24; `node22/` holds seven deployed files).
- **Followers**
  - `tests/test_node22_unit_tracking.py`: seven files. The 2026-10-16 receipt still records the retired
    unit (receipts are history and are not edited), so the comparison with the receipts becomes: the
    receipts record exactly the seven tracked files plus the one retired name. A new assertion says the
    retired name is neither a tracked file nor a manifest entry.
  - `tests/test_node22_unit_drift_check.py`: the cases that used manifest entry 0 (the retired unit) as
    their vehicle use the entry that is first now; the file's docstring count is corrected.
  - `tests/test_role_boundary_static.py`: the role pin keeps the gateway unit.
  - `infra/env/README.md`: the unit leaves the authority table and the two notes that describe what it
    loads.
  - Runbooks: the compute host no longer hosts a "diagnostic API" (`production-ops/overview.md`,
    `gateway-and-services.md`); the dated 2026-06-22 observations of `:8001` stay as history with a note
    that the unit is retired.
- **Host** (compute control host, at a scheduler pass boundary, before this PR's last push): the
  installed copy is moved aside, the drift probe is run before and after, and the operator receipt is
  part of the PR. The production checkout is fast-forwarded after the merge.

## Decisions

- **D1. The receipt is not edited.** `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`
  records seven files as they were on that day, the compute API unit among them. The tracking
  requirement therefore stops saying "the receipts record exactly the tracked files" and says which
  recorded file is no longer tracked and why.
- **D2. The role stays.** `compute_control` remains in `runtime_mode.py` with its tests (issue: out of
  scope). After this change no tracked unit starts the API in that role; the requirement that units set
  an explicit role keeps the gateway unit and the display env template.
- **D3. Host order.** The runbook asks for a probe before and after any edit of the installed units and
  for both receipts in an operator receipt under `docs/runbooks/receipts/`. To keep that receipt in this
  PR (no docs-only follow-up), the installed copy is moved aside before the last push, after the code
  review: probe from the production checkout (old manifest, 25 `same`), move the file into
  `retired-20261126/`, reload the user manager, probe with this PR's manifest from a temporary clone
  (24 `same`). Between that and the fast-forward of the production checkout its own manifest still lists
  the unit, so a probe from there would report one `missing` entry; no timer runs the probe, and no
  tracked unit orders itself after or wants the retired one (the dependency ran the other way).
- **D4. The log file stays.** `/scratch/frd_muziyao/nhms-prod/runtime/api/nhms-api-8001.log` (22 KB, last
  written 2026-11-22, before the compute role moved to this host) is evidence, not configuration. It is
  not removed.
- **D5. `compute.host.env` stays.** The gateway unit loads it.

## Not in this change

- Removing the `compute_control` role or `control_mutations_enabled`.
- `node22-db-retirement-cutover` and `production-topology-contract` still say that after the node-22
  database stop "compute API and Slurm gateway health" pass. That is the gate of a cutover completed on
  2026-06-27 on a host that has since left the compute role; its receipts record what was checked then.
  The two requirements are left as written and reported.
- `docs/runbooks/node22-db-retirement-runbook.md`, `qhh-22-business-bringup.md` and
  `two-node-deployment-overview.md` §8.2 mention a compute API of the retired host or of the retired
  docker lane; reported, not rewritten here.

## Impact

- Specs: `node22-unit-tracking`, `compute-scheduler-operationalization`, `runtime-service-role-boundary`,
  `production-ops-readiness`.
- Code: none. Tests, one unit file, the manifest, docs.
- Host: one file removed from the production user's systemd directory on the compute control host.

## Fixture

- Fixture level: `standard`.
- Risk packs: deployment/ops (a tracked unit and an installed file), docs-contract.
