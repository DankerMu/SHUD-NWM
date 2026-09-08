# node-22 journal-retention production activation receipt

Date: 2026-09-08
Issues: [#2119](https://github.com/DankerMu/SHUD-NWM/issues/2119),
[#2146](https://github.com/DankerMu/SHUD-NWM/issues/2146)
Status: activated

This is the secret-safe durable receipt for the #2119 operator activation. It omits
private backup and evidence locations, DSNs, secrets, and role identifiers. No OpenSpec
change applies: this was a docs-only operator activation with no fixture or product
behavior change. The current operational procedure is
[current-production-ops §5.8](../current-production-ops.md).

## Hash index

```text
summary          c1f1d83fd7b3c79335f247e7e4c414bf50a28cdd1be3c4d61245cb8d51329bbf
observation      1bdbeae238b4f4eac6d25a26e0ce8b74b7d76cdd094e6238aeab92e1960566a2
live             059f3c5bd0970b1a454af2e3524e6fb8c47f696880170efdfa3acaf35ff7a915
timer            1a224719e650ce51cb96a45e9a5cde44631e7cee6abc03194c43972a712d03d0
final-authority  5bf2927e3016a07d57c9a99be5d78a66e1fc3ba124baf54f19e7d8850940dd14
parity-obs       be8b5fce340e6ea97719e6455783f145dff8db16ef4b5032dc37412e6618c8cb
parity-live      e64a540c2e96747a9700819c1d9866843fb7fa2bd69c7765a17683bc4543f054
parity-timer     391f05f2542e7fa13295614394b6a587cec2cca69eaf98ef77897fdcd9cfc403
initial-dryrun   e59bb30be2f46d41006285735375bbc1a5003217f143e5eae6101e222ba49c0d
initial-parity   46b1d573a0efbc9d2a5b1f87dcd9379a744e6b8ca89c28b43a87bc6680c406cb
archive          d671efb6dde3ea8809a720f29c2258f116a6478eb46a2b7e40b433622c180c44
marker           bc5155ebba2805c7acdf8d373f542d504ec5a3d89b7cb6a72582bdce58cb895e
provider-receipt 1330e5a006ae4e1bc05004737f16adba8fa4b44e6cd481e451fc836976ae0f2a
compute-receipt  99f0dac8a6a6e02369df2f2be84b5a9edd59c0b64345ca0c5f5d6d49f06aaed4
service-unit     65450d04577d99829b6dce4ebbdeadd408d6e6cb15c0185dedfa935d370eb130
timer-unit       4833c0d94751c07e995bc702cda752b652688f5da4b60fef49bcd9947c9b0c3b
```

## Frozen activation boundary

- The final active node-22 checkout SHA was
  `99bd9d22628e2edd7f985b78f4cb1501fca87f0e`.
- After `git fetch`, the origin pin was re-verified, then the checkout advanced with
  `git merge --ff-only` from `71fbfe4…` to that SHA. Runtime and unit hashes were
  unchanged across that update. This was not a `git pull`.
- The exact existing interpreter was
  `/scratch/frd_muziyao/NWM/.venv/bin/python` (Python `3.12.7`). No `uv sync`, bare
  `uv run`, or environment rebuild occurred.
- The installed unit uses the fixed production envfile and entrypoint:

  ```text
  EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
  ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/NWM/scripts/node22_scheduler_journal_retention.py
  ```

- Exact content comparisons confirmed the installed units match their validated source
  units. SHA-256 values are `service-unit` and `timer-unit` in the hash index.
  `systemd-analyze verify` passed.
- Activation remained DB-free. The activation command did not submit Slurm work, rebuild
  an environment, or mutate Basins or quarantine. The scheduler service nevertheless
  remained necessary to publish stable snapshots.

## Initial read-only preflight and safe configuration

Before activation, the checkout had zero tracked changes and 25 untracked entries, all
under `.nhms-work/scheduler/basins-file-registry-publish/`. The existing envfile was
mode `0600` and owned by its service account; no owner UID is recorded here. All four
retention keys were initially absent.

|Surface|Observed state|
|---|---|
|`latest/`|6,682 files|
|`journal/`|293 files|
|`pipeline-events/`|absent|
|`pipeline-jobs/`|15,244 files; out of scope|
|`reconcile-inventory/`|3 entries; out of scope|
|`.locks/`|293 entries; out of scope|
|quarantine|196 files and 91 directories; not deleted|
|cold archive root|absent before setup|

GNU tar was `1.35`, zstd was `1.5.5`, and the disk check found sufficient capacity.
The first controlled configuration used `ENABLED=false`, `DRY_RUN=true`, `DAYS=90`, and
an approved archive root; the units were installed but disabled.

The initial production dry-run completed at `2026-09-07 13:45Z`. Its receipt is
`initial-dryrun` in the hash index. It found `candidates=294`, `skipped=294`,
`planned=0`, `archive=0`, `blocked=0`, `partial=0`, and `enforcement=false`. The
scheduler authority count was 23,532 before and after; `initial-parity` was identical.
The full initial authority digest is not recorded here. The count later changed through
normal production evolution, so it is not expected to equal the final activation count
below.

An extra full seven-day diagnostic was not required for #2119. It timed out after the
safe dry-run stage while the configuration remained `false/true`; it was not used as a
PASS result and created no drill. This receipt does not claim, and did not perform, a
seven-day production mutation.

## Disposable archive and restore drill

At `2026-09-08 00:12Z`, an isolated disposable copy of `IFS` cycle `2026062600` was used
for the required enforce/restore drill. It contained 14 members totaling `4,997,585 B`.
The archive and atomic-publication marker SHA-256 values are `archive` and `marker` in
the hash index.

- Enforce reported `archived=1` and removed 14 members from the disposable copy only.
- An idempotency rerun reported `archived=0`.
- Restore recovered 14 members. Cycle-query parity and per-member digest parity held.
- A `restore_clobber` attempt was refused by the no-clobber guard.
- Production hot-tree removal remained zero throughout the drill.

## Final preconditions and failure history

A latent provider-timer drift made the provider manifest stale and the retention frontier
missing once. That condition failed closed and was corrected by a DB-free provider refresh
from `05:47:49` through `05:53:40`. The refresh published successfully with 76 unchanged,
zero added, removed, package-changed, or refused entries; strict validation, mirror
equality, and readiness parity all held. Its state count was 6,806 and its receipt is
`provider-receipt` in the hash index. The provider timer was `enabled` and `active`; its
captured next run was `2026-09-09 02:39:52 UTC`. A later randomized next-run value can
differ, so that timestamp is evidence for this capture only. Long-term drift prevention is
tracked by [#2146](https://github.com/DankerMu/SHUD-NWM/issues/2146).

The fresh compute receipt was `scheduler_2026090805_7777971aa183.json`
(`compute-receipt` in the hash index), starting at `05:53:41` and finishing at
`08:08:43`. It recorded `status=submitted`,
`execution_boundary=slurm_gateway_orchestration`, `retention=completed`, frontier
`2026-09-03T12`, `source=window_floor`, and `protected=0`. That submission was ordinary
scheduler business through the Slurm gateway, not a submission by the activation command.
Later, in the final activation window, recent scheduler passes were `planning_only` and
the Slurm queue was empty.

The following attempts are failure history, not PASS evidence. Reviewers and verifiers
corrected moving-reference, marker, timezone, Persistent-stamp, and drain-budget handling
before the final successful run.

|Attempt|Outcome and containment|
|---|---|
|Stage 1|Natural-idle wait timed out; no writes occurred.|
|Stage 2|The production dry-run completed; the extra seven-day diagnostic then timed out under safe `false/true` settings.|
|Live attempt 1|All proofs passed, then the CST parser triggered rollback.|
|Live attempt 2|Proofs passed, but Persistent-stamp metadata was misdetected; receipt delta was zero and rollback followed.|
|Live attempt 3|The just-staled provider manifest left the frontier missing; rollback followed.|
|Long attempt|The first systemd failed-ledger state blocked execution before writes; only `reset-failed` bookkeeping occurred.|

## Successful live activation

The successful activation bundle basename was
`issue-2119-live-activate-20260908T110412Z-0c1c794c0558`. Its summary is `summary` in the
hash index and it contained 16 files.

|Receipt|Hash-index label|Enforcement|Dry run|
|---|---|---:|---:|
|Observation|`observation`|false|true|
|Live|`live`|true|false|
|Timer|`timer`|true|false|

Each receipt reported `candidates=298`, `skipped=298`, `planned=0`, `archived=0`,
`blocked=0`, `partial=0`, `members=0`, and `member_bytes=0`. Each had
`preflight_blockers=[]`, complete discovery, and a usable frontier. Every
`removed_paths` value was empty. All 298 skipped cycles were classified
`within_retention_window`. Live enforcement was therefore enabled but deleted nothing.

Scheduler-authority evidence is four snapshots, not six measurements: one before the
observation run, then one after observation, one after live, and one after timer. The
authority count was 23,868 in all four snapshots. The digest is `final-authority` in the
hash index and was identical across those four snapshots. Independent parity evidence is
`parity-obs`, `parity-live`, and `parity-timer`.

The final production state was:

- `ENABLED=true`, `DRY_RUN=false`, `DAYS=90`, with the approved archive root retained in
  the existing 0600 envfile;
- retention timer `enabled` and `active`, captured next run
  `2026-09-09T04:59:59Z`;
- retention service `inactive (success)` with exit status 0 after its oneshot run;
- compute and provider timers both `enabled` and `active`; and
- no fallback or residual retention process.

The installed service and timer still matched the exact validated unit contents noted
above, and `systemd-analyze verify` passed.

## Recorded rollback procedure

Rollback was documented but deliberately not executed after the final activation:

1. Stop and disable `nhms-scheduler-journal-retention.timer`.
2. Stop `nhms-scheduler-journal-retention.service`.
3. Atomically update the existing 0600 envfile, preserving all unrelated keys and the
   archive root while setting `ENABLED=false` and `DRY_RUN=true`; retain `DAYS=90`.
4. Manually run one dry-run with the fixed `.venv/bin/python` entrypoint and inspect its
   receipt before considering further action.
5. Do not delete cold archives or quarantine. Restore a selected cycle only through the
   offline, verify/stage/no-clobber/query-parity procedure in the current runbook.

Failure paths above exercised rollback multiple times. They do not establish that a final
post-activation rollback dry-run was run.

## Acceptance verdict

Activation evidence for #2119 is met. This tracked receipt is the durable docs delivery;
issue closure occurs when its PR merges. Provider-timer drift hardening remains tracked by
[#2146](https://github.com/DankerMu/SHUD-NWM/issues/2146).

|#2119 activation criterion|Evidence|Activation evidence|
|---|---|---|
|Production unit, exact interpreter, envfile, and daily Persistent timer|Validated unit hashes, `systemd-analyze verify`, active final timer|Met|
|DB-free activation command; no Basins/quarantine mutation|No DB access, environment rebuild, activation-command Slurm submission, Basins mutation, or quarantine mutation|Met|
|Safe production observation before enforcement|Initial dry-run receipt and authority parity|Met|
|Contained enforce/restore proof|Disposable IFS drill, archive/member hashes, no-clobber refusal, and query parity|Met|
|Fresh scheduler/provider safety inputs|Provider refresh and fresh compute receipt|Met|
|Live enforcement and timer safety|Three final receipts, empty removals, `within_retention_window` skips, and four authority snapshots|Met|
|Operator recovery contract|Recorded disable/stop/atomic-safe-config/dry-run and offline restore procedure|Met|
