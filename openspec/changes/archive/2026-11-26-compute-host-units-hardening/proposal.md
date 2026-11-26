# Compute host hardening: journal-backup alert, gateway drop-in in the manifest, system-level files versioned (#3359 items 3–5)

Fixture level: `standard`. Risk packs: deployment/ops (units deployed on the compute control host), config contract
(two exact-set specs move from seven to eight).

## Why

Issue #3359 lists six hardening gaps left by the 2026-11-23 migration of the compute role to `w3` / `w2`. The owner
decided on 2026-11-26 to do all six.

- Items 1 and 2 changed hosts only and are done (receipt:
  `docs/runbooks/receipts/2026-11-26-issue-3359-compute-host-hardening.md`).
- Item 6 (cold-boot drill) needs a window with console access and stays on the issue.
- **This change is items 3, 4 and 5**: the three that need the repository.

What is wrong today, read on master and on the hosts (2026-11-26):

| Item | Fact |
|---|---|
| 3 | `infra/systemd/nhms-w3-journal-backup.service` has no `OnFailure=` and no `TimeoutStartSec=`. A failed hourly mirror of the scheduler journal sends nothing; an `rsync` blocked on the `hard` NFS mount never ends, so the timer never fires again. |
| 4 | `nhms-slurm-gateway.service.d/20-runtime-path.conf` is deployed on `w3` and not in `infra/systemd/node22/MANIFEST.json`. The drift probe exits 1 on a healthy host, so its exit code says nothing. |
| 5 | The system-level files the compute role installed by hand are in no repository: `nhms-mounts-ensure.service` / `.timer` and the `slurmd` drop-in on both hosts, and since #3384 `/etc/tmpfiles.d/nwm-smt-off.conf` on `w2`. Nothing can show that they drifted. |

## What changes

### D1 — journal backup: wired to the alert, bounded (item 3)

`infra/systemd/nhms-w3-journal-backup.service` gains two lines and comments:

- `[Unit]`: `OnFailure=nhms-node22-unit-failure-alert@%n.service` (the line the seven wired oneshots carry).
- `[Service]`: `TimeoutStartSec=900`. The copy takes about 11 s today (2026-11-26 12:40Z run). systemd arms the start
  timeout anew for each command of the unit (`ExecCondition=`, `ExecStart=`, `ExecStartPost=`), so the worst case is
  three times the value: 45 minutes, below the hourly period. Read from systemd's source by the fixture reviewer, not
  measured on `w3`.

`ExecCondition=`, `ExecStart=` and `ExecStartPost=` are not touched (must preserve: `rsync --delete` never runs when the
local journal directory is absent).

The exact set of wired units moves from seven to eight (`WIRED_UNITS`, the spec, the manifest sha256).

**How each silent case is found after this change**

| Case | Found by |
|---|---|
| `rsync` exits non-zero | unit fails → alert mail |
| `rsync` hangs on `/ghdc/data` | timeout after 15 min → unit fails → alert mail |
| a hang on `/scratch/frd_muziyao` (a loopback NFS mount on `w3`) | the unit times out and fails, but no mail: the alert handler, its mail wrapper and the interpreter live under `/scratch`. Same blind spot as the skip case below. |
| skipped by `ExecCondition` | not a failure and not alerted, on purpose. The directory is absent only when `/scratch/frd_muziyao` is not mounted on `w3` (or the journal was moved by hand). With `/scratch` gone the alert handler cannot run either: it lives in the checkout under `/scratch`. The scheduler cannot run in that state, and node-27's freshness alerts report the missing cycles. The age of `LAST_OK` stays the direct check. |
| the timer does not fire (user manager down, timer disabled) | not found by this change; `LAST_OK` age. |

Rejected: a `LAST_OK` freshness check on `w3`. Every health unit on `w3` lives under `/scratch` and would be blind in
the same outage; a check that sees it has to run on node-27 and is a feature of its own.

### D2 — the gateway runtime-path drop-in is tracked (item 4)

`infra/systemd/node22/nhms-slurm-gateway.service.d/20-runtime-path.conf` is added as a byte copy (sha256 `ae59e26c…`,
78 bytes, identical to the scheduler's drop-in of the same name) and listed in `MANIFEST.json` (24 → 25 entries). Its
source is a new read-only snapshot receipt, `docs/runbooks/receipts/2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md`;
the 2026-10-16 receipt is history and is not edited. The tracking test reads both receipts: together they record exactly
the eight files under `node22/`.

Rejected: deleting the drop-in on the host. It costs a gateway restart, and whether the gateway reads
`NHMS_PYTHON_VENV_BIN` was not established; tracking what runs is the smaller step.

### D3 — system-level files in their own tree, with a checksum list per host (item 5)

```text
infra/systemd/compute-system/
  w3.sha256
  w2.sha256
  w3/etc/systemd/system/nhms-mounts-ensure.service
  w3/etc/systemd/system/nhms-mounts-ensure.timer
  w3/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf
  w2/etc/systemd/system/nhms-mounts-ensure.service
  w2/etc/systemd/system/nhms-mounts-ensure.timer
  w2/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf
  w2/etc/tmpfiles.d/nwm-smt-off.conf
```

- A tracked copy sits at the file's absolute host path below the host directory: one rule, no mapping table.
- `<host>.sha256` is what `sha256sum -c` reads, with absolute host paths. The comparison on the host is one read-only
  command and needs no new script:
  `sha256sum -c --strict /scratch/frd_muziyao/NWM/infra/systemd/compute-system/w3.sha256`, run as root (the checkout is
  readable by root and the production user only; the production user does not log in to `w2`). `--strict` because
  without it a malformed line is only a warning and its file is silently not compared; the test pins the line shape
  for the same reason.
- `nhms-mounts-ensure.*` is identical on both hosts and is tracked twice; a test pins the two copies equal.
- The copies are taken from the hosts as they are on 2026-11-26, after item 2 changed the `w3` drop-in. The bytes are in
  `.workplans/3359/host-files/` (fetched by the orchestrator, checksums compared with the hosts).

Not `infra/systemd/system/`: `tests/test_node27_raw_retention_canonical_deployment.py` asserts that directory holds
exactly the three node-27 units its installer installs. A separate tree leaves that assertion as it is.

Known limits: `sha256sum -c` proves the listed files are unchanged; it does not see an extra file dropped next to
them. The tree is not all hand-made host state: the mask on `w2` (item 1), `/etc/fstab`, `/etc/exports` and `slurm.conf`
are recorded in the runbook and receipts only.

## Must preserve

- `nhms-w3-journal-backup.service`: `ExecCondition=`, `ExecStart=`, `ExecStartPost=`, `Nice=`, `IOSchedulingClass=`
  byte-identical; the `.timer` untouched.
- The seven already wired oneshots, the alert template and every other file under `infra/systemd/` byte-identical.
- The 2026-10-16 receipt is not edited.
- `infra/systemd/system/` and its test untouched.
- The drift probe's code (`scripts/node22_unit_drift_check.py`) is not changed: item 4 is closed by the manifest.
- The entropy hard gate stays at 247 findings / 0 failing (unit files under `infra/systemd` are scanned since #3231).

## Not in this change

- Item 6 (cold-boot drill). It also carries what only a reboot can show: `w3`'s `slurmd` does not start while `w2` is
  down; `w2` comes back with SMT off.
- A `LAST_OK` freshness check on node-27 (see D1).
- `ConditionHost=` on the user units (item 1's alternative; the mask is in place).
- Installing the system-level files from the repository by a script: they are installed by hand as root, and the
  runbook says how.
- The #3384 production numbers and the runbook text about them (after the 22:00Z pass of 2026-11-26).

## Deployment (after the merge, on the issue)

On `w3`, at a pass boundary, D5 cadence:

1. Production checkout fast-forwarded to the merged head.
2. `nhms-w3-journal-backup.service` copied into the production user's unit directory, `systemctl --user daemon-reload`.
3. Drift probe: every entry `same`, `untracked` empty, exit 0 (closes item 4).
4. `sha256sum -c --strict` of `w3.sha256` on `w3` and of `w2.sha256` on `w2`, as root: all `OK` (closes item 5).
5. The next `:40` run of the backup succeeds and `LAST_OK` moves.
6. One transient unit with the `OnFailure=` line and `/bin/false` (the method of the 2026-11-12 receipt): the journal
   of the alert instance shows `SENT` (a missing env file makes the handler log `SKIPPED` and exit 0, so a successful
   alert unit alone proves nothing). This sends one real mail; the owner agreed to it on 2026-11-26.
