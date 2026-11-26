# Tasks: retire the compute API unit

## Must preserve

- The drift probe's behaviour and every malformed-manifest case it refuses: each case of
  `tests/test_node22_unit_drift_check.py` still proves what it proved, on another entry.
- The tracking test still fails when a tracked file's bytes differ from the manifest or from the receipt
  that records it, when a file under `node22/` is not a manifest entry, and when a manifest entry under
  `node22/` has no file.
- `nhms-slurm-gateway.service` still fixes `NHMS_SERVICE_ROLE=slurm_gateway`, pinned by a test.
- The authority table of `infra/env/README.md` still resolves every non-empty `EnvironmentFile=` of the
  remaining tracked units.
- The `compute_control` role and its tests.
- The two receipts under `docs/runbooks/receipts/` are not edited.

## 1. Fixture

- [x] 1.1 Proposal, tasks and four spec deltas; `openspec validate retire-compute-api-unit --strict --no-interactive`.

## 2. Unit and manifest

- [ ] 2.1 `git rm infra/systemd/node22/nhms-compute-api.service`; remove its entry from
  `infra/systemd/node22/MANIFEST.json` (24 entries, order of the others unchanged, no other byte of the
  remaining entries changed).
- [ ] 2.2 Search table: every remaining hit of `nhms-compute-api`, `:8001`, `nhms-api-8001`,
  `compute API`, `diagnostic API`, `诊断 API`, `uvicorn apps[.]api[.]main` outside
  `openspec/changes/archive/`, `docs/plans/`, `docs/runbooks/receipts/`, `docs/bugs.md` and the
  2026-10-16 entropy audit, each with the reason it stays.

## 3. Tests

- [ ] 3.1 `tests/test_node22_unit_tracking.py`: seven deployed files; the receipts record the seven
  tracked files plus exactly the retired `nhms-compute-api.service`; the retired name is not a file
  under `node22/` and not a manifest entry; docstring and test name say seven. The comparison of the
  receipts' names with the tracked names is a module-level function, and a synthetic case shows it
  refuses a receipt that records any other untracked name (the receipts themselves cannot be edited to
  show it).
- [ ] 3.2 `tests/test_node22_unit_drift_check.py`: the cases that named the retired unit use the entry
  that is first in the manifest now; cases that address an entry by index and mean a particular file
  (the `DROPIN` cases at index 2 today) find it by name, so that removing entry 0 does not silently move
  them to another file; docstring count equals the number of tracked files the fixture copies; a case deploys an extra top-level `nhms-compute-api.service` and expects it under `untracked`
  with exit 1 (if a top-level untracked case exists already, it is extended, not duplicated).
- [ ] 3.3 `tests/test_role_boundary_static.py`: the explicit-role test keeps the gateway unit. New static
  test: no file under `infra/systemd/` both starts `apps.api.main` in an `ExecStart=` line and sets
  `NHMS_SERVICE_ROLE=compute_control` (the scheduler unit sets that role and starts the scheduler, which
  the test must accept).
- [ ] 3.4 Red runs, each restored byte-identical: the unit file put back without a manifest entry; the
  manifest entry put back without the file; the retired name added to the tracked tuple; the gateway
  unit's role line removed; the retired unit file put back (the new static test of 3.3 goes red).

## 4. Docs

- [ ] 4.1 `infra/env/README.md`: the unit's table row; the two sentences that count "the first five
  rows" / "the five deployed units in the first five rows" as verified on 2026-09-02 (with the row gone
  the fifth row would be the alert template, deployed later: the sentences name the four units that
  remain and say the fifth unit of that reading is retired); the note that names "the gateway and the
  compute API" as loaders of `compute.host.env`; the note that describes what the "live" compute API unit
  loads.
- [ ] 4.2 `docs/runbooks/production-ops/overview.md` (two lines) and
  `docs/runbooks/production-ops/gateway-and-services.md`: the check under "确认 node-22 Gateway 与诊断 API"
  loses the `uvicorn apps[.]api[.]main` branch of its `pgrep` and the `8000` of its `ss` pattern, its
  heading names the gateway only, and the note about bare `/health` says that route belonged to the
  retired unit. The compute host hosts no API besides the gateway.
- [ ] 4.3 The dated 2026-06-22 observations (`gateway-and-services.md`, `operating-scope.md`) keep their
  text and get one clause: the unit was retired by #3394.

## 5. Evidence

- [ ] 5.1 Local: the three test files of section 3, `tests/test_node22_unit_failure_alert.py` and every
  other test file that reads `infra/systemd/node22/`, `MANIFEST.json`, `infra/env/README.md` or the
  edited runbooks; whole-tree
  collection; `ruff check --no-cache .`; `markdownlint-cli2` on the edited docs; entropy hard gate
  (247 findings, 0 failing, same ids) before and after.
- [ ] 5.2 node-27, detached worktree at the PR head, no database: the same files; hard gate.
- [ ] 5.3 Review: fixture review; one code review seat (tests still prove what they proved; docs make no
  new claim about production).
- [ ] 5.4 CI green on the PR.

## 6. Host (compute control host, before the PR's last push)

The runbook rule (`docs/runbooks/node22-control-plane-manual-recovery.md`, "改单元前后") wants a probe
before and after any edit of the installed units, with both receipts in an operator receipt under
`docs/runbooks/receipts/`. The receipt is part of this PR, so the host step is done before the last push
and after the code review, at a scheduler pass boundary (scheduler service inactive, timer not stopped:
nothing here touches a unit that runs).

- [ ] 6.1 Before: drift probe from the production checkout (old manifest): 25 entries `same`,
  `untracked` empty, exit 0; the installed `nhms-compute-api.service` has the sha256 of the tracked copy.
- [ ] 6.2 Move the installed file into `retired-20261126/` inside the systemd user directory (the probe
  ignores directories; the earlier `retired-20260630/` is the precedent); `systemctl --user
  daemon-reload`; `systemctl --user status nhms-compute-api.service` reports the unit not found; the
  scheduler timer is still active and the gateway still answers its health route.
- [ ] 6.3 After: drift probe with this PR's manifest, run with the production interpreter from a
  temporary clone of the PR head outside the production checkout: 24 entries `same`, `untracked` empty,
  exit 0. (From the production checkout, which keeps the old manifest until it is fast-forwarded to the
  merge, the probe reports that one entry `missing` in the meantime; no timer runs the probe.)
- [ ] 6.4 Operator receipt `docs/runbooks/receipts/2026-11-26-issue-3394-compute-api-unit-retired.md`
  with both probe receipts (JSON only, no diff output), in the PR's last push.

## Evidence Floor

- 5.1 to 5.4 and 6.1 to 6.4 above.
- After the merge (recorded on the issue, not a task of this change): the production checkout is
  fast-forwarded at a pass boundary and the probe run from it reports 24 `same`, exit 0.
