# Unit descriptions redeployed on the compute control host (issue #3427, slice 9.2; 2026-11-28)

Operator receipt for one edit of the production user's systemd directory on `w3`, the compute control host: 17 installed
unit files were replaced by the tracked copies of commit `ddd6bb959`, in which `Description=` and comment lines name the
compute role where they named "node-22". No directive other than `Description=` differs in any of the 17 files. Nothing
was started, stopped or restarted; no test mail was sent; the production checkout was not moved.

The owner's go-ahead of 2026-11-28 covers this step once (recorded on #3427).

## Before the step

- The 17 files were final before the host was touched: on `ddd6bb959` the hard gate's finding multiset equals master's,
  the tests that read the unit files passed locally and on node-27 (68 test files, `7687 passed, 3 skipped`), two review
  seats read the bytes, and CI's full sharded suite was green on the pull request.
- Read-only on 2026-11-28T07:32Z: the 24 installed files equal the manifest of master, and `infra/systemd` does not
  differ between the production checkout's commit (`f5c3b487e`) and master. The install therefore carried the text of
  this slice and nothing else.
- The step's script was rehearsed three times on a private copy of the unit directory under the production user's
  scratch tmp (removed after each run), with `daemon-reload` left out: twice the success path (08:36Z, 08:48Z) and once
  with the rollback forced after the renames (08:38Z; the probe with the production checkout's manifest read 24 `same`
  afterwards). After the rehearsals the 24 real files were byte-identical to the reading of 07:32Z.

## What was done (2026-11-28 08:49:11Z to 08:49:23Z)

The scheduler service was `inactive` (no pass running) and none of the oneshot services being replaced was running or
starting; no timer was stopped.

1. Export of `ddd6bb959` (`git archive` of `infra/systemd/` and `scripts/node22_unit_drift_check.py`, unpacked under
   `/tmp`, removed afterwards). Pins: the commit id; the sha256 of its `MANIFEST.json`
   (`e00e7f462d05701bc1aaa9b4e1cec90295bafedc3d20797957a8142cc94b8b29`); the entries whose sha256 differs from the
   production checkout's manifest are exactly the 17 below; each of the 17 export files equals its manifest sha256.
2. Drift probe from the production checkout (manifest `f33f0d12…`): exit 0, 24 `same`, `untracked` empty.
3. State read as the baseline: nine timers `active`; `nhms-slurm-gateway.service` `active`, main PID 305926, active since
   2026-11-26 00:19:37 UTC, `/api/v1/slurm/health` 200; no user unit in the failed state.
4. Every target is a regular file, not a symlink. The 17 installed files were copied to
   `backup-3427-20261128T084911Z/` inside the systemd user directory (neither `systemd` nor the probe reads that
   directory); each copy has the sha256 of its source.
5. All 17 were staged next to their targets under dot-prefixed `.tmp` names with the mode of the target, and each
   compared with its manifest sha256, before any was replaced.
6. 17 renames over the targets (08:49:17Z), then `systemctl --user daemon-reload`: exit 0.
7. Drift probe with the manifest of `ddd6bb959`, run with the production interpreter from the export: exit 0, 24 `same`,
   `untracked` empty.
8. State after: identical to the baseline (the nine timers `active`; the gateway the same process, PID 305926, same
   start time, health 200); no user unit in the failed state; `NeedDaemonReload=no` for the 16 loaded units.

## Installed files

`Description` as `systemctl --user show` reports it after the reload (the alert template is read from the file: a
template has no loaded instance). Every mtime is 2026-11-28 08:49 UTC.

| relative path under `~/.config/systemd/user/` | sha256 | mode | bytes | `Description` |
|---|---|---|---|---|
| `nhms-compute-scheduler.service` | `5fbd228b11eb44160dcd66115035a25243047d6fc68fbad1c08ff7941a6445a5` | `0600` | 829 | NHMS production scheduler one bounded continuous pass (compute role user) |
| `nhms-node22-refresh-timer-health.service` | `988307aaee68cb058d017a5b98bf2fda9ee79d0138e4d6032fda476d87244c33` | `0600` | 2277 | NHMS compute file-provider refresh timer health probe (read-only) |
| `nhms-node22-refresh-timer-health.timer` | `ada4fe4dc1bf45d5cb9e06c3031ba7e73e5b2660bf5954c40242c8e5ea1a5eda` | `0644` | 889 | Hourly health probe for the NHMS compute file-provider refresh lane |
| `nhms-node22-scheduler-stall-health.service` | `04d39227dbd640b2314d1470925178dad23169fb02ae1f264bd11ca134e8b20d` | `0600` | 2842 | NHMS compute DB-free scheduler stall health probe (read-only) |
| `nhms-node22-scheduler-stall-health.timer` | `dec01a1ea2c05bdd0a80dfca18c334394bd5ddbcc1076caeee9a014dd30e2309` | `0644` | 1389 | Quarter-hourly health probe for the NHMS compute DB-free scheduler lane |
| `nhms-node22-unit-failure-alert@.service` | `f55fd54ffee0f97fd143000d83a72b4bfe63862ee60af457053d17dc833a8496` | `0600` | 1477 | NHMS compute unit failure alert for %i |
| `nhms-scheduler-evidence-retention.service` | `0efa90eb3b8151a23270316da8e4781a84db0e1d77941621dbd8a846cfd117fb` | `0600` | 508 | NHMS compute scheduler evidence retention |
| `nhms-scheduler-evidence-retention.timer` | `b7e434bb933d823c26262edfd1a838de096dbcbaacef3b09458f00d87f8b4ebe` | `0644` | 231 | Run NHMS compute scheduler evidence retention daily |
| `nhms-scheduler-file-provider-refresh.service` | `ad1ab16b9a6e99e6139e1993b2c149e91cb7665f61491f82defc8755ccd4739d` | `0600` | 1908 | NHMS compute DB-free scheduler file-provider refresh |
| `nhms-scheduler-journal-retention.service` | `270918fac4aa64cc61bfe6a4118c91a3f795c1dab79f437fc1ca987f26823ccb` | `0600` | 518 | NHMS compute scheduler journal-cycle cold retention |
| `nhms-scheduler-journal-retention.timer` | `11b8cd03e869259234b01d823376421c7634ea6522c908aeccf2d94ce21d0a2b` | `0644` | 235 | Run NHMS compute scheduler journal-cycle retention daily |
| `nhms-scheduler-state-index-capacity.service` | `65a2798e10f88a6db81cf686d52bce509d7c1ca1d0f4eb97d9de2003ef84719b` | `0600` | 642 | NHMS compute state-index capacity watch (prune-retention dry-run only) |
| `nhms-scheduler-state-index-capacity.timer` | `48c29a3f921a612f8a5dc57b3d291885d15560ad82d59e9eabf5eeaf1f150058` | `0644` | 231 | Run NHMS compute state-index capacity watch daily |
| `nhms-scheduler-state-index-prune.service` | `ab1ea1764b94b1aa0686750225071a1678f0fe1bf0cb7651554bcff54f50b2cc` | `0644` | 1171 | NHMS compute state-index prune-retention auto-enforce at the scheduler pass boundary |
| `nhms-scheduler-state-index-prune.timer` | `a3aa40176b302a7d1277b8f1b4182cbdc2aa55c8523c620907158f527e5e575a` | `0644` | 224 | Run NHMS compute state-index auto-prune daily |
| `nhms-slurm-gateway.service` | `0aaa07e91044ce07f573c097588c5cf515ae643d5a7de85bdcd54b3bee5df862` | `0600` | 805 | NHMS standalone Slurm gateway (compute role user) |
| `nhms-w3-journal-backup.service` | `9ae90b791637c293817ce1b97bfb9108e6af820c72dd22729d328de1555601d2` | `0644` | 1036 | w3: mirror the scheduler journal to NFS operator-archives (disaster-recovery copy) |

`nhms-w3-journal-backup.service` changes by one comment only; its `Description=` names the host and stays.

## Until the production checkout is updated

The production checkout is at `f5c3b487e`. Until it is fast-forwarded to the merge:

- the drift probe run from it reports these 17 entries `differs` (exit 1; observed at 08:49:23Z: 17 `differs`, 7 `same`,
  `untracked` empty). No timer runs the probe and nothing mails on it;
- `scripts/install_node22_refresh_timer_health.sh` and `scripts/install_node22_scheduler_file_provider_refresh.sh` copy
  units from the checkout they run in: run from the production checkout before that update, they would put the old
  `Description=` of their units back.

## Rollback

Copy the 17 files back from `backup-3427-20261128T084911Z/`, `systemctl --user daemon-reload`; the probe from the
production checkout then reads 24 `same` again (the path the forced rehearsal exercised). The backup directory can be
removed once the production checkout has been updated and the probe from it reads 24 `same`.

## Probe receipts

Under the production user's scratch tmp, `unit-drift/`: `20261128T084911Z-before.json`, `20261128T084911Z-after.json`,
`20261128T084911Z-interim.json`; the rehearsals' receipts carry `rehearsal-` in their names.
