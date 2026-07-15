# Runbook: Tier Node-27 Timeseries Storage

Operation, rollback, and cadence rationale for the node-27 archive lane
(product mover + storage inventory audit) delivered under
`openspec/changes/tier-node27-timeseries-storage`.

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
- Architecture record: `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md` (the
  archive resolver is never imported by `apps/api/**` or `apps/frontend/**`).

The mover and audit share the 1.7 TB volume that also backs
`/home/ghdc/nwm/object-store` and `/home/ghdc/nwm/archive`. Free-space
watermarks defend that shared volume — the mover refuses enforce before
touching any source when free bytes fall below the configured refuse
threshold.

## Install (node-27, `nwm` user)

All operations run as the `nwm` user under systemd `--user`. Do NOT install
system-level (root) units for this lane.

1. Create the runbook receipt directories:

   ```
   mkdir -p ~/node27-product-archive-logs ~/node27-storage-inventory-audit-logs ~/node27-timeseries-compression-logs ~/node27-archive-rebuild-drill-logs ~/node27-raw-retention-logs
   ```

2. Copy the env examples into place and lock them down:

   ```
   cp /home/nwm/NWM/infra/env/node27-product-archive.example \
      /home/nwm/NWM/infra/env/node27-product-archive.env
   cp /home/nwm/NWM/infra/env/node27-storage-inventory-audit.example \
      /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   chmod 0600 /home/nwm/NWM/infra/env/node27-product-archive.env \
              /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   ```

   Fill in `DATABASE_URL` for the audit env with a read-only role
   (`nhms_display_ro` or equivalent). A superuser or write-capable role in
   this env is a documented rollback / lint finding — the audit does not
   need write access and the receipt runbook must reject the file if the
   role is not read-only.

   The governance env now shares three archive vars with the mover and
   audit envs — `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES`,
   and `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES`. All three env files MUST
   declare identical values; the wrappers source only their own env file,
   so drift means governance reports a different band than the mover
   actually enforces. See "Free-space watermark tuning" below for the
   band semantics.

3. Install the four user units and enable the timers. Order matters only
   because the audit timer must see the mover's latest final leaves:

   ```
   systemctl --user daemon-reload
   systemctl --user enable --now nhms-node27-product-archive.timer
   systemctl --user enable --now nhms-node27-storage-inventory-audit.timer
   ```

4. Copy or update `infra/env/node27-resource-governance.env` from the
   extended `.example` so it declares `NHMS_ARCHIVE_ROOT` and the two
   `NHMS_ARCHIVE_FREE_SPACE_*_BYTES` vars — matching the mover and audit
   env files verbatim. Without this the governance receipt will report
   `archive_root.status = "skipped"` and the archive band will not
   appear. Then register the four new units with
   `nhms-node27-resource-governance` via the shared `DEFAULT_SERVICES`
   list — no code action required beyond deploying the updated
   `scripts/node27_resource_governance.py`. The next governance audit
   tick will report their service/timer state, and — once the governance
   env carries the shared archive vars — the archive root free-space
   band as well. See "Free-space watermark tuning" for band semantics
   and "Refuse-threshold behavior" for what a `refuse` band triggers.

## Timer cadence order (UTC)

The four related timers are staggered so each receipt is fresh when the
next tick runs:

| Order | Timer                                        | OnCalendar         | Rationale |
|-------|----------------------------------------------|--------------------|-----------|
| 1     | `nhms-node27-product-archive.timer`          | `03:20:00 UTC` daily | Mover finalizes leaves before audit scans them. |
| 2     | `nhms-node27-storage-inventory-audit.timer`  | `03:40:00 UTC` daily | Audit reads the mover's committed final leaves and emits the completeness receipt. |
| 3     | `nhms-node27-resource-governance.timer`      | `04:10:00 UTC` daily | Governance audit reports the four new units + archive-root free-space band. |
| 4     | `nhms-node27-timeseries-compression.timer`   | `04:25:00 UTC` daily | Terminal-chunk compression runs after governance so the previous-day receipt is already captured. Enablement is task §4.5 (requires migration `000047` applied first). |

### Cadence vs. retention-receipt validity window (design D6)

`#855` pins the retention runner's completeness-receipt validity
window at 24 h. The storage inventory audit fires every 24 h with the audit
tick preceding every planned retention tick (audit at 03:40 UTC, retention
by construction after the audit), so a fresh, schema-valid
archive-completeness terminal receipt is present when the retention gate
consults it whenever the configured destination remains writable. Publication
failures are instead explicit journal diagnostics and never recurse into a
second write. **Do not lengthen the audit cadence beyond 24 h without
extending the retention receipt validity window first.**

## Operation

### Reading receipts

- Product archive mover receipt:
  `/home/nwm/node27-product-archive-logs/receipt.json` (mode 0600).
  The receipt binds one `outcome`:
  - `success` — every selected candidate archived + verified + retired
    (enforce) or planned (dry-run).
  - `failed` / `indeterminate` — see per-candidate `terminals` and top-level
    `discovery_failures`.
  - `refused_free_space` — mover refused enforce because free space fell
    below the configured refuse watermark; sources untouched, `candidates`
    / `selected` / `deferred` / `terminals` / `events` /
    `discovery_failures` all empty. `free_space.band == "refuse"` and
    `free_space.free_bytes < free_space.refuse_bytes`. Non-zero exit.
- Storage inventory audit receipt (archive completeness):
  `/home/nwm/node27-storage-inventory-audit-logs/completeness-receipt.json`.
  Schema version `1.1` has four exact outcomes: `complete` and `incomplete`
  carry coverage windows/selectors; `blocked` carries a stable
  `refusal_reason`; `indeterminate` carries `UNEXPECTED_AUDIT_ERROR`.
  DB-export salvage accepts only the two coverage outcomes. Retention can
  distinguish an on-disk blocked terminal from a missing audit receipt; live
  downstream refusal behavior remains part of #856.

### States access precondition (`STATES_ACCESS_DENIED`)

The product-archive mover treats an `EACCES` while traversing one or more
`states` leaves as one lane-level operational precondition failure. It first
publishes a mode-0600 receipt whose only access entry has
`lane_hint=states`, `locator=states`, and
`STATES_ACCESS_DENIED count=N euid=UID egid=GID`; the CLI then writes one
compact JSON diagnostic with `exit_reason=STATES_ACCESS_DENIED` and exits `2`.
No candidate is selected and no archive or source mutation is attempted.
Other discovery failures remain per-locator failures and retain exit `1`.

Choose exactly one access model before changing anything. Adding `nwm` to the
`nfsdata` supplementary group is required only for the group model; the ACL
model does not require `id` to contain `nfsdata`. Group membership alone is not
sufficient for a leaf that remains mode `0700`. An authorized storage operator
must establish one complete access model across both existing and future
`states` content:

- With group access, every directory from the NFS root through each state leaf
  grants the chosen group read/write/search (`rwx`) access, state files grant
  group read access, and newly written directories/files inherit the intended
  group and compatible modes (for example, setgid parent directories plus a
  writer umask/default ACL that preserves group `rwx` on directories).
- With ACL access, every current directory grants the named `nwm` user
  effective `rwx` and every current state file grants effective read access.
  Writer parents also carry a default ACL so future state directories inherit
  `rwx`. Product-archive enforce renames each verified source leaf, creates a
  claim directory beside it, and recursively removes the tombstone, so `rx` is
  only sufficient for discovery/dry-run and MUST NOT be accepted as an enforce
  precondition. POSIX default ACLs cannot express different named-user entries
  for new directories and regular files: a default `nwm:rwx` may give `nwm`
  write on newly created files after the creator-mode mask is applied. If that
  extra file permission is unacceptable, the writer must apply a post-create
  ACL that leaves directories `rwx` and files read-only; do not weaken the
  directory permission to `rx`. The effective ACL mask must not remove the
  required permissions.

The storage and identity administrators own any group, ownership, mode, or ACL
mutation. This PR does not run or prescribe site-specific `usermod`, `chgrp`,
`chmod`, or `setfacl` commands. Before they choose a repair, capture the current
tree and both future-inheritance surfaces. Do not truncate `find` with a pipe:
that would replace its permission-error exit status with `head`'s success.

```bash
states_root=/home/ghdc/nwm/object-store/states
inspection_log=$(mktemp /tmp/node27-states-tree.XXXXXX.log)
set +e
find "$states_root" -xdev -maxdepth 4 \
  -printf '%M %u %g %p\n' >"$inspection_log" 2>&1
find_rc=$?
set -e
sed -n '1,200p' "$inspection_log"
printf 'complete find exit=%s log=%s\n' "$find_rc" "$inspection_log"
test "$find_rc" -eq 0

for path in \
  /home/ghdc/nwm/object-store \
  "$states_root" \
  "$states_root/gfs" \
  "$states_root/IFS" \
  "$states_root/gfs/basins_heihe_shud" \
  "$states_root/IFS/basins_qhh_shud"
do
  stat -c '%A %a %U %G %n' "$path"
  getfacl -cp "$path"
done
```

The complete `find` must exit zero. For the group model, the selected writer
parents must have the `nfsdata` (or explicitly selected equivalent) group,
setgid set, and group `rwx` after the ACL effective mask is applied. Their
default ACL/mode and the actual writer's umask must preserve group `rwx` on new
directories and group read on new files. For the ACL model, the writer parents
must have a default named-user `nwm` or selected-group entry and a
default/effective mask that preserves directory `rwx`; current files require
read. A plain access ACL on today's leaves is insufficient because tomorrow's
leaves would regress.

Identify the process that actually creates a recent state leaf on the node
where that process runs (normally the node-22 compute plane; do not infer its
umask from node-27's mover). During an authorized observation window, use the
real writer PID or service/job evidence and record its live supplementary
groups and umask:

```bash
# Obtain the cycle/PID from the active service/job and confirm it against the
# recent state leaf. fuser may report no PID when no writer is active.
cycle=2026050100
fuser -v "/home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/$cycle"
writer_pid=12345
grep -E '^(Name|Pid|Uid|Gid|Groups|Umask):' "/proc/$writer_pid/status"
sed -n '1,120p' "/proc/$writer_pid/cgroup"
ps -o pid,ppid,user,group,lstart,args -p "$writer_pid"
```

If `/proc/<writer-pid>/cgroup` binds the process to a systemd unit, also record
the configured value rather than assuming the process default:

```bash
writer_unit=replace-with-observed-unit.service
systemctl show "$writer_unit" \
  -p User -p Group -p SupplementaryGroups -p UMask -p MainPID
```

For a Slurm writer, bind the PID to its job and record `scontrol show job
<job-id>`/`sacct -j <job-id>` plus `/proc/<writer-pid>/status`; a login-shell
`umask` is not evidence for a running batch step. The repair is incomplete
until a newly created probe leaf from that real writer inherits the chosen
group/default ACL and effective mask.

After a group-membership change, every old login and the long-lived `nwm`
systemd user manager still has the old supplementary groups. Coordinate a
maintenance window with the ingest/display operators first: record enabled and
active `nwm` user units, wait for archive/audit work to finish, and announce
that refreshing the user manager terminates **all** `nwm` login sessions and
stops its user services/timers. An authorized login administrator then
terminates the old `nwm` session/user manager with the site's login-manager
procedure (for systemd-logind, `loginctl terminate-user nwm`). Reconnect as
`nwm`, restore only the previously enabled units, and verify service health.
Do not merely restart the archive service inside the stale user manager.

From that fresh `nwm` login, verify the effective identity and access without
changing the object-store:

```
id
namei -l /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100
getfacl -p /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100
test -x /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100
test -w /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100
test -r /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100/state.cfg.ic

mapfile -t manager_pids < <(pgrep -u "$(id -u)" -x systemd)
test "${#manager_pids[@]}" -eq 1
grep -E '^(Name|Pid|Uid|Gid|Groups|Umask):' \
  "/proc/${manager_pids[0]}/status"

# systemd-run uses the same user manager and supplementary groups as timer
# services. Both commands must succeed; the first output is retained as proof.
systemd-run --user --wait --pipe --collect /usr/bin/id
systemd-run --user --wait --pipe --collect \
  /usr/bin/test -r \
  /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100/state.cfg.ic
systemd-run --user --wait --pipe --collect \
  /usr/bin/test -w \
  /home/ghdc/nwm/object-store/states/IFS/basins_qhh_shud/2026050100
```

Repeat `namei`, `getfacl`, directory `test -x`/`test -w`, and file `test -r` for
`states/gfs/basins_heihe_shud/<cycle>` and
`states/IFS/basins_qhh_shud/<cycle>`, and run the complete logged `find` again.
Any permission diagnostic or non-zero `find` exit, failed `test`, or `---`
component in `namei` means the precondition is unresolved. Under the group
model, a missing selected group (for example `nfsdata`) in the fresh login,
`/proc/<user-manager-pid>/status`, or `systemd-run --user ... id` is also a
failure. Under the named-user ACL model, missing `nfsdata` is not a failure;
the effective/default ACL plus the successful timer-context tests are the
oracle. Only after the chosen model, current full tree, new writer-created
leaf, fresh user manager, and timer context all pass should the operator rerun
the mover dry-run and confirm the new receipt has no
`STATES_ACCESS_DENIED` entry.

### Selected-batch source-retirement preflight

The state discovery gate above is not the complete enforce permission gate.
Every product selected from `forcing`, `runs`, or `states` must also be
retirable by the effective mover identity. For every selected source, its
parent needs effective write/search (`wx`), its root and every internal
directory need effective read/write/search (`rwx`), and every regular file
must remain readable and bound to the validated preimage. Apply the chosen
group/default-ACL or named-user/default-ACL model to all selected product
lanes, including future writer-created content; fixing only `states` is not
sufficient when an eligible `runs` or `forcing` parent remains mode `0755`.

The mover checks these permissions through opened no-follow descriptors and
the actual effective uid/groups/ACL result; mode-bit inspection is only
operator context, not the runtime oracle. Dry-run performs the complete
read-only tree check and writes no probe path. A failed check produces a
non-zero `SOURCE_RETIREMENT_PREFLIGHT_FAILED` receipt, not a false `planned`
terminal. Enforce first completes that read-only check for the entire selected
batch, then creates, fsyncs, removes, and fsyncs one randomized hidden probe per
unique opened source parent. This happens before staging, archive publication,
quarantine, durable-guard creation, or source mutation. One failed check or
probe aborts every selected candidate. A probe that is certainly removed has
no residue; uncertain cleanup is `indeterminate` and names only its safe
object-store-relative residue. Do not manually remove an indeterminate probe
until its receipt and filesystem identity have been captured.

Run the selected-source audit as `nwm` after an ordinary 30-day dry-run. This
loop is NUL-safe through base64 and therefore also covers legal spaces in run,
model, or basin identifiers. It audits every selected lane rather than a
hand-picked state sample:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /home/nwm/NWM
NODE27_PRODUCT_ARCHIVE_ENV_FILE=/home/nwm/NWM/infra/env/node27-product-archive.env \
  ./scripts/node27_product_archive_once.sh --minimum-age-days 30

receipt=/home/nwm/node27-product-archive-logs/receipt.json
object_root=/home/ghdc/nwm/object-store
jq -e '.mode == "dry-run" and (.selected | length > 0)' "$receipt"
jq -r '.selected | group_by(.identity.lane)[] |
  "\(.[0].identity.lane) \(length)"' "$receipt"

jq -r '.selected[].source_path | @base64' "$receipt" |
while IFS= read -r encoded
do
  relative=$(printf '%s' "$encoded" | base64 -d)
  source=$object_root/$relative
  parent=${source%/*}
  printf 'selected=%s\n' "$relative"
  namei -l "$source"
  getfacl -cp "$parent"
  test -x "$parent"
  test -w "$parent"
  find "$source" -xdev -type d -exec sh -eu -c '
    for directory do
      test -r "$directory"
      test -w "$directory"
      test -x "$directory"
      getfacl -cp "$directory"
    done
  ' sh {} +
  find "$source" -xdev -type f -exec sh -eu -c '
    for file do test -r "$file"; done
  ' sh {} +
  # Sticky directories require the same ownership proof as rename(2); a
  # successful test(1) access check alone is not enough.
  euid=$(id -u)
  if [[ -k $parent ]] &&
     (( euid != 0 && euid != $(stat -c %u "$parent") &&
        euid != $(stat -c %u "$source") ))
  then
    printf 'unproved sticky parent: %s\n' "$parent" >&2
    exit 1
  fi
  find "$source" -xdev -type d -perm -1000 -exec bash -eu -c '
    euid=$1
    shift
    for directory do
      if (( euid != 0 && euid != $(stat -c %u "$directory") )); then
        foreign=$(find "$directory" -xdev -mindepth 1 -maxdepth 1 \
          ! -uid "$euid" -print -quit)
        test -z "$foreign" || {
          printf "unproved sticky directory: %s\n" "$directory" >&2
          exit 1
        }
      fi
    done
  ' bash "$euid" {} +
done
```

Any failed command, missing lane expected for the authorized batch, sticky
directory without an ownership proof for the child being renamed, or ACL mask
that removes `wx`/`rwx` keeps enforce blocked. Preserve this output with the
dry-run receipt. For future inheritance, capture the selected paths before a
real forcing/run/state writer cycle, then rerun the same loop against each new
writer-created leaf; record the writer PID/unit/job, effective groups and
umask as described above. Parent default ACLs and each new directory/file must
still satisfy the same parent `wx`, directory `rwx`, and file-read checks.
Do not use an operator-created dummy file as writer-inheritance evidence.

Only after both current-tree and real future-writer checks pass may the
authorized enforce command run. The mover itself performs the descriptor-bound
randomized parent probes; do not substitute a shell `touch` test for them:

```bash
cd /home/nwm/NWM
NODE27_PRODUCT_ARCHIVE_ENV_FILE=/home/nwm/NWM/infra/env/node27-product-archive.env \
  ./scripts/node27_product_archive_once.sh \
  --minimum-age-days 30 --enforce
jq -e '
  .mode == "enforce" and .outcome == "success" and
  (.selected | length > 0) and
  ([.terminals[].status] | all(. == "archived" or . == "retired-from-existing"))
' /home/nwm/node27-product-archive-logs/receipt.json
```

The first authorized 30-day enforce attempt on deployed head
`cec39013167bc7ce6585ed34e3a9194832f99900` is failed evidence for this gate:
the preceding dry-run discovered 320 candidates and selected eight, but
enforce published eight verified archives before all eight source tombstone
renames failed because `nwm` could not write the selected source parents. The
eight sources remained present and the verified archives/durable guards were
preserved as residue. This outcome is not a PASS and the old head must not be
used for another enforce run. On the repaired head, the same permission shape
must fail before candidate one with zero new archive publication and zero
source mutation. Existing verified archives remain governed by the normal
idempotent path after the preflight passes. Do not manually delete prior
`.archive-guards/*`: on retry the mover boundedly reconciles only an exact
two-file guard whose children are the same inode/signature pair as the current
verified canonical archive, preserves foreign/ambiguous guards, and fails
before source mutation with explicit safe residue if cleanup is uncertain.
The repaired retry ran on deployed head
`e130949c4f9d658d9e31251e5ced135147e18712` at
2026-07-15T05:40:15Z. Its controlled 30-day enforce receipt is committed as
`receipts/tier-node27-timeseries-storage/product-archive/controlled-enforce-20260715T054015Z.json`:
all eight selected sources passed the batch gate, the mover re-read and
checksum-verified the eight existing canonical archives, reconciled their
matching guards, retired all eight identical sources, and reported eight
`retired-from-existing` terminals with empty residue. The production env
remained at 45 days. The failed first attempt remains evidence for the missing
batch gate; it is not relabeled as PASS. The 228 audit gaps and the follow-up
complete audit remain owned by #1070, and no #856 cascade command was run.

Issue #849 task 2.5 PASS was recorded on deployed head
`c0778c37d5d1a16b374e3c0335c354e10891d537` for one explicitly authorized
30-day run with `per_tick_bound=17`, capped at 17 objects and 122,085,701
source bytes. The dry-run receipt SHA-256 is
`b4333336657dfdc4e8f96de4aab334b3bc6e52a0d92d8e2391d55fee75106ca9` and
the enforce receipt SHA-256 is
`096fdd5e060806833cb1ab210c81e6b09c152374b3f5c4d441ad47d798f1f17b`.
The dry-run found 128 `runs`, 224 `states`, and zero `forcing` candidates;
the first `states` candidate was index 16, so the bounded selection comprised
16 `runs` and one `states` object. Enforce succeeded with 17 `archived`
terminals, zero discovery failures, zero residue, 28,738,825 archive bytes,
and free-space band `clean`. Post-enforce verification confirmed all 17
sources absent and all 17 archives verified. The completeness baseline remains
`incomplete` with 228 selectors; #1070 still owns salvage and the complete
audit. Node-22 and the out-of-scope DB mutation, salvage, compression, drill,
retention, and timer-enablement surfaces were not touched.

Local evidence:

- [`issue849-selected-source-audit-20260715T070119Z-normalized.txt`](receipts/tier-node27-timeseries-storage/product-archive/issue849-selected-source-audit-20260715T070119Z-normalized.txt)
- [`issue849-authorized-dryrun-20260715T070211Z.json`](receipts/tier-node27-timeseries-storage/product-archive/issue849-authorized-dryrun-20260715T070211Z.json)
- [`issue849-authorized-enforce-20260715T070211Z.json`](receipts/tier-node27-timeseries-storage/product-archive/issue849-authorized-enforce-20260715T070211Z.json)
- [`issue849-post-enforce-verification-20260715T070448Z.json`](receipts/tier-node27-timeseries-storage/product-archive/issue849-post-enforce-verification-20260715T070448Z.json)
- [`issue849-terminal-receipt-20260715T070211Z-corrected.json`](receipts/tier-node27-timeseries-storage/product-archive/issue849-terminal-receipt-20260715T070211Z-corrected.json)

### Free-space watermark tuning

Initial values (in `infra/env/node27-product-archive.example`):

- `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES=322122547200` (300 GiB)
- `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES=161061273600` (150 GiB)

Config validation enforces `refuse < warn`, both `> 0`, both integer bytes.
If either env var is set, both must be set; empty/negative/non-integer
values fail closed at startup with no truthiness fallback.

Tune by watching the governance receipt `archive_root.band` field after
each daily tick:

- `clean` — `free_bytes >= warn_bytes` — steady state; no action.
- `warn` — `refuse_bytes <= free_bytes < warn_bytes` — review retention or
  archive backlog before the refuse gate fires. Governance receipt emits
  `ARCHIVE_FREE_BELOW_WARN` recommendation.
- `refuse` — `free_bytes < refuse_bytes` — mover WILL refuse next tick.
  Free space (drop retention, add capacity) before the next mover tick.
  Governance receipt emits `ARCHIVE_FREE_BELOW_REFUSE` recommendation.

If both watermark envs are unset, the mover runs without free-space
enforcement (backwards compatible) and the governance receipt reports
`archive_root.band = "unconfigured"`.

### Refuse-threshold behavior

When `free_bytes < refuse_bytes` at mover start:

1. Mover exits non-zero after publishing the receipt with
   `outcome = "refused_free_space"`.
2. No candidate discovery runs; no source is mutated; no staging tarball
   is created; the mover flock is released cleanly.
3. Receipt records exact measured `free_bytes`, configured `warn_bytes`
   and `refuse_bytes`, and `archive_root` path.

Dry-run mode still evaluates and reports the refusal terminal — the refuse
band is a governance signal, not a mutation-only gate.

## 3. DB-export salvage

The salvage lane covers a one-time historical operation: forcing / river
timeseries windows whose upstream product cycles never made it into either
the hot object-store or the archive (typical case: `forcing/` before
2026-06-16, where the object-store was reset before an archive lane
existed). The salvage exporter reads those rows straight out of the two
detail hypertables via `COPY (SELECT ... WHERE ...) TO STDOUT WITH
(FORMAT CSV, HEADER)`, compresses the CSV with zstd, and publishes the
object plus a `manifest.json` sidecar under
`NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/`.

Runner: `scripts/node27_db_export_salvage.py` (wrapper
`scripts/node27_db_export_salvage_once.sh`, env example
`infra/env/node27-db-export-salvage.example`).

### 3.1 Scope + provenance invariants

- Selector scope is the archive-completeness receipt's `salvage_selectors`
  array (design D6). Hardcoded selector lists are refused — the exporter
  is a downstream consumer of the audit contract, not a scope authority.
- Manifests carry `provenance: "db-export"` so downstream consumers
  (drill #854, retention gate #855) can permanently distinguish salvage
  objects from product-derived archive objects.
- The runner refuses at boot if its DSN maps to a role that can INSERT
  into either `met.forcing_station_timeseries` or `hydro.river_timeseries`
  (both `has_table_privilege` AND a rolled-back sentinel INSERT are
  checked). Wire the runner with `nhms_display_ro` or an equivalent
  explicit read-only role.

### 3.2 Salvage restore is manual — no automated restore lane

**Per ADR 0002 decision 3, `db-export` salvage objects have no automated
or steady-state restore lane.** The only restore path is the manual
`COPY FROM` procedure below. Retention (#855, forward cross-link to
section 6.2 of the retention runbook when authored) MAY drop
salvage-covered windows only with this documented manual recovery path
in place.

The archive rebuild drill (#854) verifies salvage objects by checksum
and manifest row-count parity — it does NOT reingest them.

#### 3.2.1 Checksum pre-check

Before any restore attempt, confirm the on-disk object matches the
manifest's recorded sha256:

```
cd ${NHMS_ARCHIVE_ROOT}/db-export/<lane>/<identity>/
manifest_sha=$(jq -r '.exports[0].object.sha256' manifest.json)
disk_sha=$(sha256sum data.csv.zst | awk '{print $1}')
[ "$manifest_sha" = "$disk_sha" ] || { echo "ABORT: checksum mismatch"; exit 1; }
```

If the pre-check fails, **do not restore** — treat the object as
corrupted evidence and escalate; the archive rebuild drill (#854)
verifies the same digest and will surface the corruption.

Also confirm the manifest matches the schema:

```
uv run python -c "
import json, jsonschema
schema = json.load(open('schemas/salvage_manifest.schema.json'))
manifest = json.load(open('manifest.json'))
jsonschema.validate(manifest, schema)
print('OK')
"
```

#### 3.2.2 Manual `COPY FROM` procedure

Salvage manifests record the exact column list used at export time (the
full DDL column set for the hypertable). The restore MUST reuse that same
list — do not paste a hand-typed column list.

For a forcing salvage object
(`db-export/forcing/<forcing_version_id>/data.csv.zst`):

```
zstd -q -d -c data.csv.zst | psql \
  -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms \
  -c "\copy met.forcing_station_timeseries (
        forcing_version_id, basin_version_id, station_id, valid_time,
        source_id, variable, value, unit, native_resolution, quality_flag
      ) FROM STDIN WITH (FORMAT CSV, HEADER)"
```

For a river salvage object
(`db-export/runs/<run_id>/data.csv.zst`):

```
zstd -q -d -c data.csv.zst | psql \
  -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms \
  -c "\copy hydro.river_timeseries (
        run_id, basin_version_id, river_network_version_id,
        river_segment_id, valid_time, lead_time_hours, variable, value,
        unit, quality_flag, created_at
      ) FROM STDIN WITH (FORMAT CSV, HEADER)"
```

The writer role MUST have INSERT on the target hypertable — the salvage
exporter's read-only role will not be able to restore. Verify the
restored row count against `manifest.exports[0].exported_row_count`
after the load:

```
psql -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms -c "
  SELECT COUNT(*) FROM met.forcing_station_timeseries
   WHERE forcing_version_id = '<forcing_version_id>'
     AND valid_time >= '<manifest.exports[0].selector.window.start>'
     AND valid_time <  '<manifest.exports[0].selector.window.end>';"
```

#### 3.2.3 No pipeline code path performs automated CSV import

`apps/api/**` and `apps/frontend/**` MUST NOT reference the salvage
runner or the `db-export/` prefix (ADR 0001 display carve-out; enforced
by `tests/test_node27_db_export_salvage.py::test_display_carve_out`).
The reingest pipeline covers only product-provenance archive rebuilds
(design D1). Salvage restore requires a human operator, deliberately.

Related documents:

- ADR 0002 decision 3 (salvage restore is manual): see
  [`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md#decisions).
- Retention runbook section 6.2 (forward reference; to be authored by
  #855): retention MAY drop a salvage-covered window only if the
  manifest here validates and the checksum pre-check above succeeds.
  When #855 lands section 6.2, it MUST cross-link back to this section
  3.2.

## 4. Hypertable compression

Native TimescaleDB compression is the sole mechanism this milestone applies
to shrink the two hot hypertables (`hydro.river_timeseries` and
`met.forcing_station_timeseries`). Compression is applied to terminal chunks
only (age older than the configurable lag, default 7 d) by the receipted
runner (`scripts/node27_timeseries_compression.py`, `#851`), never to the
active write-target chunk. This section covers the fail-closed write guard
and the manual decompress procedure that pairs with it.

### 4.0 Controlled initial live run (`#1069`)

The first production compression is a one-chunk controlled operation, not a
normal timer tick. The committed service is the only recurring mutation
entrypoint and therefore has the literal invocation
`node27_timeseries_compression_once.sh --enforce`. Direct operator invocation
of the wrapper **without** that flag remains dry-run. A contended wrapper
publishes a mode-0600, schema-valid `outcome=refused_lock` receipt with empty
`selected`/`deferred`/`skipped`, null/zero totals, no DB call, and a redacted
stderr diagnostic; this deliberately replaces a stale shared receipt.

Run this sequence only from an ff-only-synchronized, tracked-clean node-27
worktree whose SHA is the reviewed #1069 head. Keep all generated evidence
under `/home/nwm/NWM/.nhms-issue1069-live/` mode 0600. Never print or commit the
writer password/full DSN, run shell tracing, dump the environment, or place a
credential in process argv.

1. Capture preflight JSON binding node-27, repository path/SHA, UTC time,
   PostgreSQL/TimescaleDB versions, `dbname=nhms`, instance
   `node27-primary-pg15`, container/service state, exact pre-run unit state,
   and the three deployed #852 write-guard sites. Source the existing ingest
   writer credential into the canonical untracked
   `infra/env/node27-timeseries-compression.env` and require mode 0600. The
   evidence records only host/port/dbname/current user, redacted connection
   identity, and privilege booleans.
2. Record the role truth exactly: `current_user=nhms`, `rolsuper=true`,
   `rolcreaterole=true`, `rolcreatedb=true`, ownership of both target
   hypertables, and EXECUTE on installed
   `compress_chunk(regclass,boolean)`. Do not call it least privilege. Do not
   create/alter/grant a role and do not use `nhms_display_ro`.
3. Before migration, write a custom-format schema-only `pg_dump`, require
   `pg_restore --list` exit 0, and record the dump's absolute path/bytes/sha256.
   Also capture canonical JSON for the two target tables' pre-migration
   catalog. This dump is forensic DDL inventory, not a data backup, restore
   drill, or compressed-storage rollback.
4. Capture the original autopipe/compression timer+service enabled/active/sub,
   `MainPID`, result and bounded journal. Stop only the autopipe timer. Require
   `MainPID=0`, no activating/running autopipe process, and no live writer or
   conflicting lock on either target/chosen chunk. A pre-existing failed
   autopipe service with `MainPID=0` is preserved; do not `reset-failed` to
   manufacture a clean state.
5. Apply `db/migrations/000047_hypertable_compression_settings.sql` with
   `ON_ERROR_STOP=1`. Only after exit 0, apply the same file a second time.
   The two canonical post-apply catalog documents must be byte-identical and
   must contain exactly D3's indexed segment/order columns, both hypertables
   compression-enabled, and no compression-policy job. A nonzero first apply
   stops the run; repairing partial DDL is separately authorized.
6. Install the committed service/timer byte-for-byte under
   `~/.config/systemd/user/`, verify both file hashes, `daemon-reload`, then run
   `systemctl --user enable nhms-node27-timeseries-compression.timer` **without
   `--now`**. Require `is-enabled=enabled` while timer and service stay
   inactive throughout this issue. `Persistent=true` means starting the timer
   can catch up the missed 04:25 event and create an unauthorized second batch.
7. Independently reproduce the runner's exact catalog predicate/order with
   lag 604800 and bound 1. Freeze compact sorted JSON for the selected identity
   tuple `(hypertable_schema, hypertable_name, chunk_schema, chunk_name,
   range_start, range_end)` and its sha256. The selection must be one terminal
   `hydro.river_timeseries` chunk, more than ten minutes outside the cutoff,
   `pg_total_relation_size <= 8589934592`, with at least 322122547200 free
   filesystem bytes. Stop on any mismatch.
8. Invoke the wrapper once without `--enforce` using a task-specific receipt.
   Require a clean dry-run, exact bound-1 tuple, every `after_bytes=null`, no
   catalog mutation and no service activation. Immediately repeat the
   independent selector query and require the same selector hash.
9. The sole authorized mutation is one direct wrapper invocation with literal
   `--enforce`, the same env/lag/bound/lock, a distinct receipt, and an external
   900-second timeout. Do not use the timer or call `compress_chunk` manually.
   A timeout, partial result, scope mismatch or null/error `after_bytes` is
   terminal failed evidence and does not authorize a retry.
10. Capture both-table pre/post snapshots with `hypertable_size(regclass)`
    (acceptance size), parent `pg_total_relation_size` (diagnostic only),
    compressed/uncompressed counts, and compressed sibling names/sizes. One
    selected chunk must become compressed; selected and combined hypertable
    bytes must decrease. It is truthful and expected that the met table can
    remain settings-only with compressed count zero in this bounded batch.
    On node-27's TimescaleDB 2.10.2, resolve the sibling by joining origin and
    sibling rows in `_timescaledb_catalog.chunk` through
    `origin.compressed_chunk_id`; the 2.10 information view does not expose
    `compressed_chunk_schema` or `compressed_chunk_name` columns.
    The receipt and post snapshot are separate measurement instants:
    `pg_total_relation_size` includes one-page FSM/VM growth. Require exact
    sibling identity, both measurements below the origin size, and at most
    1 MiB absolute receipt-to-snapshot drift; do not require byte identity or
    rerun compression to chase an 8 KiB auxiliary-page change.

The representative performance proof uses production query construction, not
handwritten lookalikes. For the selected hydro chunk, freeze a nonempty
production-valid `q_down` identity. Curve capture calls
`PsycopgForecastStore.get_forecast_series`, records the exact statement/params
sent by `_fetch_forecast_segment_rows`, and hashes
`packages/common/forecast_store.py`. MVT capture imports
`postgis_tile_sql("hydro")`, uses the same parameter construction as
`hydro_display._postgis_tile_params` at deterministic z=9, and hashes both
source files. Curve result bytes are compact sorted UTF-8 JSON plus a trailing
newline; MVT result bytes are recorded as hex and hashed as decoded raw bytea.

For each query and each phase, use a new read-only connection: retain the first
execution as cold-biased information, perform two warmups (up to five while
reads remain), then record exactly seven `EXPLAIN (ANALYZE, BUFFERS, FORMAT
JSON)` samples. Before/after cache classes must match. The median is sorted
sample 4; p95 is sample 7. Gates are
`after_median <= max(1.5*before_median, before_median+100)` and
`after_p95 <= max(2*before_p95, before_p95+250)`. Result rows/bytes/hash must be
identical, concurrent-load sampling stable, and each after plan must recursively
contain `DecompressChunk` bound to the selected chunk identity.

#### 4.0.1 Independent terminal evidence bundle

`scripts/node27_timeseries_compression_live_evidence.py` has no DB connection
or mutation entrypoint. It reads one operator bundle, independently reopens
every referenced artifact, verifies exact byte counts/sha256, validates both
runner receipts, recomputes selector hashes, D3 settings, totals, size/count
deltas, raw query/result hashes, median/p95 thresholds, and plan binding, then
atomically publishes the terminal envelope against
`schemas/timeseries_compression_live_evidence.schema.json`.

Every bundle artifact reference is exactly
`{"path":"/absolute/path","sha256":"<lowercase-64hex>","bytes":N}` and
must name a regular non-symlink file. Canonical embedded JSON hashes are
`jq -cS` UTF-8 including its trailing newline. The bundle has these exact
top-level keys: `schema_version`, `issue`, `generated_at`, `node`, `head_sha`,
`database_identity`, `authorization`, `preflight`, `migration`, `selection`,
`receipts`, `sizes`, `catalog`, `benchmarks`, `cleanup`, `out_of_scope`.

Referenced JSON contracts are:

- `preflight.evidence`: the facts in steps 1–4, including exact role booleans,
  guard presence, quiescence and inactive compression units;
  `preflight.schema_dump` is the raw custom dump, and
  `preflight.catalog_before` its canonical catalog neighbor.
- `migration.catalog_after_first|second` and `catalog.post`:
  `{"hypertables":{"hydro.river_timeseries":true,
  "met.forcing_station_timeseries":true},"compression_settings":[...],
  "policy_jobs":[]}`. Each setting row has exactly schema/table/`attname`,
  `segmentby_column_index`, `orderby_column_index`, `orderby_asc`, and
  `orderby_nullsfirst`, in the D3 order pinned by the fixture.
- `selection.snapshot`: `cutoff`, `free_bytes`, and ordered `selected`; the
  sole selected row adds `before_bytes` to the six-field identity tuple.
- `sizes.pre|post`: `tables` keyed by both D3 hypertables. Each row has
  `hypertable_size`, `parent_relation_size`, `compressed_chunks`,
  `uncompressed_chunks`, and `compressed_relations`. Each compressed relation
  binds `origin_chunk_schema`/`origin_chunk_name` to its sibling
  `schema`/`name` and measured `bytes`.
- `benchmarks.evidence`: exactly `curve`, then `mvt`. Each stores source refs,
  exact `query_text` + sha256, non-secret parameters, before/after raw
  `result_payload` + hash/row/byte counts, cache/timing/buffer fields, seven
  samples, raw after plan, and concurrent-load verdict. Curve payload is a JSON
  row array; MVT payload is nonempty even-length hex.
- `cleanup.evidence`: autopipe restored, compression timer enabled/inactive,
  compression service inactive with activation count zero, and installed unit
  hashes matching the repository.

Example invocation (paths contain no credential):

```
uv run python scripts/node27_timeseries_compression_live_evidence.py \
  --bundle-path /home/nwm/NWM/.nhms-issue1069-live/bundle.json \
  --output-path /home/nwm/NWM/.nhms-issue1069-live/terminal.json
```

`PASS_TASK_4_5` is emitted only after all gates pass. On any failure, keep both
compression units inactive, restore the autopipe timer's exact prior state,
and preserve artifacts. Compression is not a transactional batch: a chunk
already compressed after a partial/timeout/regression remains compressed and
the outcome remains failed/partial. Do not rerun enforce, auto-decompress,
claim rollback from the schema dump, or relabel the evidence. Any later
`decompress_chunk` recovery is a separate authorization bound to the exact
successful receipt list, followed by fresh catalog/size/result/query checks.

### 4.1 Write guard overview

The three ingest write paths —
`workers/output_parser/parser.py::upsert_river_timeseries`,
`workers/forcing_producer/store.py::replace_forcing_timeseries`, and
`packages/common/forcing_domain_handoff_apply.py::
_replace_forcing_station_timeseries` — each call the shared helper
`packages.common.timescale_write_guard.check_batch_targets_uncompressed`
BEFORE their identity-scoped DELETE. The guard runs one catalog lookup
against `timescaledb_information.chunks` bounded by
`SET LOCAL statement_timeout = '5s'`, checking whether any compressed chunk
overlaps the batch's `[min(valid_time), max(valid_time)]` window. On
overlap, the guard raises `CompressedChunkWriteError` naming the chunk and
this runbook's decompress anchor. On catalog error, it fails closed with
`CompressedChunkGuardError` — no silent permit.

The guard is intentionally scoped to `hydro.river_timeseries` and
`met.forcing_station_timeseries` only. The archive rebuild drill (`#854`)
writes to an isolated staging schema and never trips the guard.

### 4.2 Residual reingest window mismatch

The guard's semantic scope is the batch time window, not the identity's
full history. A batch whose `valid_time` range is fully outside compressed
chunks BUT whose identity-scoped DELETE (`WHERE forcing_version_id = %s`
or `WHERE run_id = %s AND river_network_version_id = %s AND variable = %s`)
would touch older compressed rows falls through to TimescaleDB's raw
`cannot update/delete rows from chunk … as it is compressed` error. This
is a documented residual — not a guard bug. The response is identical to
the guarded case: use the decompress procedure below on the specific
chunk(s) TimescaleDB names.

### 4.3 Decompress procedure

#### 4.3.1 Operator triage codes

The compressed-chunk write guard surfaces via four caller-observable string
codes. Every one of them routes to the decompress procedure below. Grep
the DB / stderr / receipt surface for these literals when triaging a
reingest failure:

| Code (literal string) | Where produced | How to observe |
|---|---|---|
| `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` | `packages/common/forcing_domain_handoff_apply.py::apply_forcing_domain_handoff` — attached as `unavailable_report.unavailable_reasons[].code` (from `REASON_APPLY_COMPRESSED_CHUNK_BLOCKED`) when the guard raises inside `_replace_forcing_station_timeseries`. | Persisted on the apply report (DB or API response) that the caller inspects. |
| `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` | `workers/output_parser/parser.py::OutputParser.parse_run` — stamped on `hydro.hydro_run.error_code` via `mark_run_failed`, and emitted as the stderr prefix by `workers/output_parser/cli.py` when the guard escapes. | `hydro.hydro_run.error_code` column; parser CLI stderr line `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/cli.py` stderr prefix — emitted when `ForcingProducer.produce()` re-raises a `CompressedChunkGuardError` un-wrapped. | Forcing producer CLI stderr line `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/producer.py::ForcingProducer._mark_failed` — stamped on `met.forecast_cycle.error_code` when the dedicated `except CompressedChunkGuardError` arm fires. | `met.forecast_cycle.error_code` column (with `status = 'failed_forcing'`). |

For every code above, the operator response is the decompress procedure
in §4.3.2 below (identify chunk from the structured error message → run
`decompress_chunk(...)` → re-run ingest). Route on the code; do NOT paper
over with a generic ingest retry.

#### 4.3.2 Manual decompress steps

When a reingest surfaces `CompressedChunkWriteError` or TimescaleDB's raw
compressed-chunk error, follow this manual procedure. Do NOT introduce an
automated decompress-on-demand lane (ADR 0002 decision 3 — the manual
escape hatch is intentional; automated decompress-on-demand would
re-introduce write amplification the compression tier is meant to
prevent).

1. Identify the offending chunk from the error message. Example structured
   error:

   ```
   Reingest targets compressed chunk _timescaledb_internal._hyper_1_1_chunk
   in hydro.river_timeseries; run decompress procedure per
   docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure
   before retrying.
   ```

2. On node-27, connect to the active primary PG as a role authorized to
   decompress. Example:

   ```
   psql "postgres://nhms_owner@127.0.0.1:55432/nhms"
   ```

3. Confirm the chunk is currently compressed (belt-and-suspenders — the
   error already asserted this, but a manual re-run may already have
   decompressed it):

   ```
   SELECT chunk_schema, chunk_name, hypertable_schema, hypertable_name,
          is_compressed, range_start, range_end
   FROM timescaledb_information.chunks
   WHERE chunk_schema = '_timescaledb_internal'
     AND chunk_name = '_hyper_1_1_chunk';
   ```

4. Decompress the chunk:

   ```
   SELECT decompress_chunk('_timescaledb_internal._hyper_1_1_chunk'::regclass);
   ```

   `decompress_chunk` returns the fully-qualified chunk relation on
   success. If it errors with "chunk … is not compressed", the chunk was
   already decompressed by a prior manual step; move on.

5. Re-run the ingest / reingest that failed. The guard's next lookup on the
   same chunk range will now find `is_compressed = false` and permit the
   DELETE + INSERT.

6. After the reingest succeeds, plan a re-compression pass. The scheduled
   compression runner (`nhms-node27-timeseries-compression.timer`, cadence
   documented in `#851`) will pick the chunk up on its next tick provided
   the chunk's `range_end` is older than the configured lag. If the chunk
   is inside the lag window (i.e. still "warm"), let it age; do not force
   an out-of-cadence compression.

Rollback: none required — `decompress_chunk` is idempotent and can be
undone by the scheduled compression runner. If the reingest itself
completes but the operator wants to abandon the decompress state, force a
compression pass with the runner's enforce flag once the chunk falls
outside the lag window.

## 7. Archive rebuild drill (`archive-rebuild-drill`)

The drill (`scripts/node27_archive_rebuild_drill.py`, issue #854) proves that
products archived by the mover and salvage objects published by
`scripts/node27_db_export_salvage.py` are round-trippable back into an
ingest-shaped Postgres/TimescaleDB — without ever writing the production
hypertables (design D5, ADR 0002).

### 7.1 Isolation invariants (never bypass)

- **Staging DB is a SEPARATE PHYSICAL DATABASE.** Same-DB same-schema
  isolation is unachievable because every ingest SQL literal is
  `core.` / `met.` / `hydro.` / `ops.` qualified (`workers/output_parser/parser.py`,
  `packages/common/forcing_domain_handoff_apply.py`). The drill refuses at
  entry if the parsed `STAGING_DATABASE_URL` dbname equals
  `PROD_DATABASE_URL_RO` dbname.
- **Prod connection is SELECT-only.** The drill opens the prod DSN with
  `default_transaction_read_only = on` and asserts the setting via
  `SHOW default_transaction_read_only`. Even a role that accidentally has
  INSERT privilege cannot mutate prod inside a read-only transaction.
- **Staging DB is DROPped + CREATEd + migrated from zero per run.** Uses
  `POSTGRES_ADMIN_URL` (a superuser DSN whose dbname is `postgres`).
  Cleanup runs in a `finally:` block on both PASS and FAIL paths.
- **Archive files are read-only inputs.** The drill never rewrites, moves,
  or deletes tar.zst / manifest.json / db-export .csv.zst objects.

### 7.2 Wire-format codes

The drill emits structured `differences[]` on FAIL. Codes are
byte-identical across the code (`scripts/node27_archive_rebuild_drill.py`),
this runbook, and design.md #854 fixture block:

- `ARCHIVE_MANIFEST_MISMATCH` — manifest sha256/size does not match
  restored file.
- `ARCHIVE_TAR_CORRUPTED` — tarball truncated or extract-to-disk fails
  (includes malicious `../` path escape).
- `SALVAGE_SHA256_MISMATCH` — db-export object sha256 does not match its
  salvage manifest.
- `SALVAGE_ROW_COUNT_MISMATCH` — decompressed row count differs from
  manifest `exported_row_count`.
- `REGISTRY_CLOSURE_INCOMPLETE` — the ancestor row(s) needed for
  ingest FK checks are missing in prod (fail-closed; no vacuous PASS).
  Also fires on staging schema-drift: if any prod row carries a column
  the staging table lacks (e.g. a not-yet-migrated staging DB).
- `STAGING_COUNT_MISMATCH` — staging `COUNT(*)` differs from the
  file-derived expected count (archive manifests carry no row counts;
  parity oracle is the restored file itself).
- `DRILL_UNCAUGHT_ERROR` — any downstream fault outside the enumerated
  codes (psycopg2 disconnect, filesystem I/O error, unexpected
  AttributeError, ...) is packaged as a schema-valid FAIL receipt with
  `differences[].expected.code = DRILL_UNCAUGHT_ERROR` +
  `differences[].actual.cause_type = <ExceptionClassName>`. Never emit
  a raw stack trace to the receipt lane; operators consume the receipt
  file as the sole oracle.
- `DRILL_CONCURRENT_INVOCATION` — an existing drill holds
  `~/node27-archive-rebuild-drill-logs/drill.lock` (single-instance
  guard via `fcntl.flock`, non-blocking). Wait for the first drill to
  finish or investigate the stuck process.

### 7.3 How to run

```
# 1. Prime env
cp /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.example \
   /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env
chmod 0600 /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env
# fill in real PROD_DATABASE_URL_RO, STAGING_DATABASE_URL,
# POSTGRES_ADMIN_URL, and NHMS_ARCHIVE_ROOT.

# 2. Source + invoke against real archive + salvage manifests
set -a; source /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env; set +a
uv run python scripts/node27_archive_rebuild_drill.py \
  --archive-manifest "${NHMS_ARCHIVE_ROOT}/runs/<run_id>/manifest.json" \
  --archive-manifest "${NHMS_ARCHIVE_ROOT}/forcing/gfs/<cycle>/<basin>/<model>/manifest.json" \
  --salvage-manifest "${NHMS_ARCHIVE_ROOT}/db-export/forcing/<forcing_version_id>/manifest.json"
```

Exit code semantics: `0` = PASS, `1` = FAIL (per-item differences), `2` =
configuration refusal (missing env var, DSN parity, unsafe path). The
receipt path is announced to stdout on success.

### 7.4 Reading the receipt

Receipts match `schemas/archive_rebuild_drill_receipt.schema.json`.
Every receipt carries:

- `staging_database.database` — the isolated physical database name that
  was DROPped after the run. MUST differ from prod dbname.
- `staging_database.schema` — semantic drill-run label (e.g.
  `archive_drill_20260711_forcing_gfs`); NOT a Postgres CREATE SCHEMA.
- `staging_database.instance_id` — cluster/host identifier stamped by
  `NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID`.
- `coverage[]` — the validated `(source, window)` tuples the drill
  actually restored + verified. Coverage tuples are attributed ONLY to
  successfully verified manifests (per-source: `forcing` / `runs` from
  product cycles, `db-export` from salvage selectors).
- On PASS: `comparisons.cycles[]`, `comparisons.selectors[]`,
  `comparisons.counts[]`.
- On FAIL: `differences[]` where each entry names the failing item, the
  wire-format code, and the expected/actual values.

### 7.5 How the coverage rule maps to the retention gate

Per the `archive-rebuild-drill` spec: a drill PASS receipt covers a
candidate retention drop window only when its declared `coverage[]`
tuples include, sampled within or older than that window:

- ≥1 product-derived cycle for each timeseries-bearing source lane
  (`forcing/`, `runs/`) that has DB rows in the drop window, AND the
  UNION of these cycles' coverage tuples must cover the entire drop
  window; PLUS
- ≥1 `db-export` selector whenever verified salvage objects cover any
  part of the drop window, AND the UNION of the drill's
  `source=db-export` tuples must likewise cover the drop window.

The drill EMIT contract is per-cycle: each verified product manifest
contributes one 24 h coverage tuple sampled within or older than the
drop window (see §7.4). The retention gate check is UNION-based: a 30 d
drop window is normally covered by ~30 daily tuples whose union spans
it — no single tuple is expected to individually contain the drop
window. These two shapes coexist deliberately (drill emits per-cycle
tuples; retention union-checks them against the candidate drop window).

The drill declares its tuples faithfully; the retention runner (#855)
evaluates their UNION against its candidate drop window. A FAIL receipt,
a stale receipt, or a PASS receipt whose per-source UNION does not
cover the drop window blocks retention enforcement. See §8.2 wire-code
`DRILL_COVERAGE_FORCING_MISSING` / `DRILL_COVERAGE_RUNS_MISSING` /
`DRILL_COVERAGE_DB_EXPORT_MISSING` for the code emitted when the union
does not cover; see
`openspec/changes/tier-node27-timeseries-storage/design.md` #855
fixture block H2 pin for the canonical statement.

### 7.6 Recovery (post-fault operator playbook)

When a drill run leaves side effects (stale staging DB, stale workspace,
held lock), recover in this order — every step is safe to run against
a clean environment (no-op if already recovered):

1. **Stuck lock (`DRILL_CONCURRENT_INVOCATION`).** Confirm no live
   drill is running (`ps -ef | grep node27_archive_rebuild_drill`);
   if none, remove the lock file:
   `rm -f ~/node27-archive-rebuild-drill-logs/drill.lock`.
2. **Staging DB left over.** The drill's `finally:` teardown drops
   the staging DB even on FAIL. If a hard kill (SIGKILL, OOM) skipped
   the finally, drop by hand: connect via `POSTGRES_ADMIN_URL` and run
   `DROP DATABASE IF EXISTS "<staging_dbname>"`. Staging dbname is in
   the last receipt at `staging_database.database`.
3. **Workspace tree left over.** The drill removes
   `NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE` on both PASS and FAIL. If a
   hard kill skipped cleanup, remove the tree manually:
   `rm -rf "$NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE"`. Do NOT touch
   `NHMS_ARCHIVE_ROOT` (the archive source is read-only per ADR 0002).
4. **Prod DB never needs recovery.** The drill only ever holds the prod
   connection in `default_transaction_read_only = on`; there is no
   prod-side state to unwind. If a receipt disagrees, that is a bug —
   file it against #854.

### 7.7 Live receipts (§5.2 boundary)

Live PASS receipts on node-27 covering the planned 30-day drop window
are the domain of task §5.2 (follow-up commit under issue #854, not part
of the §5.1 PR that introduced this section). Once §5.2 lands, the live
receipts will be committed under
`docs/runbooks/receipts/node27_archive_rebuild_drill/` — mirroring the
mover and audit receipt directories.

## 8. Gated DB retention (`timeseries-db-retention`)

The retention runner
(`scripts/node27_timeseries_retention.py`, issue #855) drops chunks
strictly older than the drop window (default 30 days) from the two D3
detail hypertables `hydro.river_timeseries` and
`met.forcing_station_timeseries` via TimescaleDB `drop_chunks`. Enforce
mode is hard-gated on TWO archive receipts and refuses fail-closed if
either is missing, stale, or fails to cover the drop window (spec
`timeseries-db-retention` and design D6 / D7). Compression state is
never a gate — compressed chunks older than 30 days are exactly the
retention target (H3 divergence from #851).

Related documents:

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
  fixture block "Workflow Fixture: Issue #855" (H1–H17 pins).
- Architecture record:
  [`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md).
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md`. The
  runner is never imported by `apps/api/**` or `apps/frontend/**`.

### 8.1 Install (node-27, `nwm` user)

Live enablement of the retention unit is a §6.3 follow-up (issue #856);
this PR delivers the units + wrapper + tests, and the install steps below
are prepared for the follow-up commit.

1. Create the retention log directory (same shape as the compression
   sibling):

   ```
   mkdir -p ~/node27-timeseries-retention-logs
   ```

2. Copy the env example into place and lock it down. The env file MUST be
   mode `0600` — the wrapper refuses otherwise
   (`ENV_FILE_MODE_UNSAFE`).

   ```
   cp /home/nwm/NWM/infra/env/node27-timeseries-retention.example \
      /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   chmod 0600 /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   ```

   Fill in `DATABASE_URL` with a writer role (retention runs `drop_chunks`
   DDL). Do NOT share the audit env's `nhms_display_ro` role — retention
   requires DML privileges.

3. Register the two new units (§6.3 will `enable --now`; kept commented
   here because §6.3 owns the first live enforce):

   ```
   systemctl --user daemon-reload
   # systemctl --user enable --now nhms-node27-timeseries-retention.timer
   ```

   The service and timer files are installed under
   `~/.config/systemd/user/` from the checked-in
   `infra/systemd/nhms-node27-timeseries-retention.{service,timer}`.

### 8.2 Wire-format codes

The runner emits structured refusal reasons on `outcome=refused`. Codes
are byte-identical across code (`scripts/node27_timeseries_retention.py`
`WIRE_CODES` frozenset), this runbook, the design fixture
(`openspec/changes/tier-node27-timeseries-storage/design.md` #855 block),
and the unit tests. Any addition / rename / removal MUST land in all four
surfaces in the same commit.

- `COMPLETENESS_RECEIPT_MISSING` — env-declared completeness receipt path
  missing, unreadable, or schema-invalid.
- `COMPLETENESS_RECEIPT_STALE` — completeness `generated_at` older than
  `NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS`.
- `COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT` — `coverage_bounds` does not
  fully contain the drop window.
- `COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW` — any in-window subject has
  `verdict = gap`.
- `COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW` — any in-window subject
  has `verdict = pending-archive`.
- `DRILL_RECEIPT_MISSING` — env-declared drill receipt path missing,
  unreadable, or schema-invalid.
- `DRILL_RECEIPT_STALE` — drill `generated_at` older than
  `NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS`.
- `DRILL_RECEIPT_FAIL` — drill receipt `verdict = FAIL`.
- `DRILL_COVERAGE_FORCING_MISSING` — no set of `source=forcing` coverage
  tuples whose UNION covers the drop window (per-cycle 24 h tuples merge
  into a single covering interval; a 30 d drop window is normally covered
  by ~30 daily tuples).
- `DRILL_COVERAGE_RUNS_MISSING` — no set of `source=runs` coverage tuples
  whose UNION covers the drop window.
- `DRILL_COVERAGE_DB_EXPORT_MISSING` — completeness has `coverage=db-export`
  subject overlap but no set of drill `source=db-export` tuples whose
  UNION covers the drop window.
- `RETENTION_CONFIG_INVALID` — absolute-path / positive-int / env-parse
  failure before any DB call. Emitted to stderr as a single JSON line
  `{status: "failed", code: "RETENTION_CONFIG_INVALID", reason: <detail>}`;
  the runner exits with code 2 and NEVER publishes a file receipt (the
  receipt path itself may be part of what failed to parse).
- `RETENTION_CONCURRENT_INVOCATION` — non-blocking `fcntl.flock` on
  `/tmp/nhms-node27-timeseries-retention.lock` is already held. Receipt
  published, exit code 1.
- `RETENTION_DROP_FAILED` — per-chunk `drop_chunks` raised. Suffix
  `:<hypertable_schema>.<chunk_name>: <error>`. Whole tick refuses (H5
  fail-closed); subsequent chunks NOT attempted.
- `RETENTION_UNCAUGHT_ERROR` — catch-all top-level exception. Receipt
  carries `refusal_reason = "RETENTION_UNCAUGHT_ERROR:<ClassName>: <str(exc)>"`.
  Symmetric with #854 `DRILL_UNCAUGHT_ERROR`.

Refusal-code priority (highest first — the first hit wins):

```
COMPLETENESS_RECEIPT_MISSING
  -> COMPLETENESS_RECEIPT_STALE
    -> COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT
      -> COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW
        -> COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW
          -> DRILL_RECEIPT_MISSING
            -> DRILL_RECEIPT_STALE
              -> DRILL_RECEIPT_FAIL
                -> DRILL_COVERAGE_FORCING_MISSING
                  -> DRILL_COVERAGE_RUNS_MISSING
                    -> DRILL_COVERAGE_DB_EXPORT_MISSING
```

### 8.3 Metadata-table exemption + row-count invariant

The runner targets EXACTLY two hypertables (spec §Window and mechanism):

- `hydro.river_timeseries`
- `met.forcing_station_timeseries`

Metadata / coverage tables are NEVER retention targets:

- `hydro.hydro_run`
- `hydro.run_display_coverage`
- `met.forcing_version`
- `hydro.state_snapshot` (or wherever the state snapshot table currently
  lives)
- QC / lineage tables

Two guardrails enforce this:

1. **Structural**: `drop_chunks` only accepts hypertables; metadata
   tables are regular Postgres tables and cannot be dropped by
   `drop_chunks`. The runner's SQL literal restricts the tuple filter to
   the two D3 hypertables.
2. **Row-count invariant** (§6.1 test row 4): after every enforce tick,
   the row counts of the metadata / coverage tables MUST be unchanged.
   §6.3 embeds a pre/post row-count check in the live receipt review.

### 8.4 How to run

```
# 1. Prime env (once per node-27, then owned by operators)
cp /home/nwm/NWM/infra/env/node27-timeseries-retention.example \
   /home/nwm/NWM/infra/env/node27-timeseries-retention.env
chmod 0600 /home/nwm/NWM/infra/env/node27-timeseries-retention.env
# Fill DATABASE_URL (writer role), completeness/drill receipt paths,
# receipt path, and (optionally) lock path override.

# 2. First run MUST be dry-run — inspect candidate_chunks + deferred_remainder
set -a; source /home/nwm/NWM/infra/env/node27-timeseries-retention.env; set +a
uv run python scripts/node27_timeseries_retention.py --dry-run
cat "$NODE27_TIMESERIES_RETENTION_RECEIPT_PATH" | jq .

# 3. When ready to enforce, flip the env flag (or pass --enforce).
# Enforce PRECONDITIONS:
#  - Completeness receipt fresh AND covers the drop window with verdict=complete
#    for every subject overlapping the drop window.
#  - Drill receipt fresh AND verdict=PASS AND forcing+runs coverage tuples
#    span the drop window (+ db-export tuple if completeness reports a
#    db-export overlap).
# Either export NODE27_TIMESERIES_RETENTION_ENFORCE=1 in the env file or
# pass --enforce on the CLI.
uv run python scripts/node27_timeseries_retention.py --enforce
```

Exit codes: `0` = dry-run / enforced (both are "success" outcomes; the
receipt carries the outcome). `1` = refused (gate failure, per-chunk drop
failure, concurrent invocation, uncaught error — see §8.6). `2` = config
refusal (missing / non-absolute / non-positive env; no receipt written).

### 8.5 Reading the receipt

Receipts match `schemas/timeseries_retention_receipt.schema.json`
(schema `oneOf` — exactly one of `dry-run` / `refused` / `enforced`).

- `outcome=dry-run`: `mode=dry-run`; `candidate_chunks[]` lists chunks
  that WOULD be dropped up to the per-tick bound; `deferred_remainder[]`
  lists chunks beyond the bound. Gates ARE evaluated in dry-run mode —
  a dry-run invocation that would refuse still emits a `refused` receipt
  (`mode=enforce` per the schema `oneOf`) so operators see the exact
  refusal reason before ever running enforce. If gates pass, dry-run
  enumerates candidate chunks + deferred remainder without invoking
  `drop_chunks`. The `--dry-run` CLI flag controls the DROP phase only;
  gate evaluation is always run because it is the operator's oracle for
  whether enforce is safe.
- `outcome=refused`: `mode=enforce`; `refusal_reason` is one of the codes
  in §8.2. Nothing was dropped this tick. A `refused` receipt can be
  emitted by a `--dry-run` invocation too — the mode field always reads
  `enforce` because the schema pins that pairing.
- `outcome=enforced`: `mode=enforce`; `dropped_chunks[]` records each
  dropped chunk with its pre-drop `freed_bytes` (H4 — measured BEFORE
  `drop_chunks`); `deferred_remainder[]` records the beyond-bound
  chunks; `salvage_backed_windows[]` records the completeness-derived
  db-export windows that fell inside the drop window (H9).

### 8.6 Recovery (post-fault operator playbook)

1. **Stuck lock (`RETENTION_CONCURRENT_INVOCATION`).** Confirm no live
   retention run is active (`ps -ef | grep node27_timeseries_retention`),
   then remove the lock file:

   ```
   rm -f /tmp/nhms-node27-timeseries-retention.lock
   ```

   The lock path is byte-identical with the runner's
   `_default_lock_path()` and `infra/env/node27-timeseries-retention.example`.
2. **Per-chunk drop failure (`RETENTION_DROP_FAILED`).** The whole tick
   refuses fail-closed (H5) — no schema `partial` outcome exists.
   **Chunks that dropped successfully before the failure are NOT
   enumerated in the receipt** (schema `oneOf` forbids `dropped_chunks`
   on refused); those chunks are already gone. To reconstruct what
   actually happened, cross-reference the wrapper's `retention.log`
   (per-chunk drop timings printed to stderr) with the current
   `timescaledb_information.chunks` state before re-running enforce.
   Inspect the offending chunk (the refusal_reason suffix names it
   `<hypertable_schema>.<chunk_name>`). Common causes: statement timeout
   (5 min per chunk), active writer holding an incompatible lock, or a
   TimescaleDB catalog inconsistency. Re-run enforce after the operator
   has confirmed the DB is healthy. There is no automated retry loop —
   drops on healthy chunks should NOT proceed mid-failure without
   operator inspection.
3. **Config refusal (`RETENTION_CONFIG_INVALID`).** No receipt was
   written. Fix the env file per §8.4 and retry.
4. **Uncaught error (`RETENTION_UNCAUGHT_ERROR`).** The receipt carries
   the exception class + message. File a bug against #855 (or the
   downstream owner if the class is from a shared helper).

### 8.7 Salvage-backed windows

`salvage_backed_windows[]` in an `enforced` receipt is derived only from
the completeness receipt's subjects where `coverage=db-export` AND
`verdict=complete` AND the subject window overlaps the drop window (H9
provenance rule — chunk boundaries do NOT carry lane/subject identity).
Each entry names a `{start, end}` interval whose post-drop recovery
lane is the manual `COPY FROM` procedure documented in [§3.2](#32-salvage-restore-is-manual--no-automated-restore-lane);
the drill's coverage rule that permits dropping such windows is
[§7.5](#75-how-the-coverage-rule-maps-to-the-retention-gate).

## Rollback (unit-level, not data-level)

All units are read-mostly (audit is read-only; mover's writes are already
gated by ADR 0002 "no deletion without archive receipt"; retention only
drops chunks after both gate receipts pass). Rollback is disabling the
timers; the receipts stay on disk as historical evidence.

```
systemctl --user disable --now nhms-node27-product-archive.timer
systemctl --user disable --now nhms-node27-storage-inventory-audit.timer
systemctl --user disable --now nhms-node27-timeseries-compression.timer
systemctl --user disable --now nhms-node27-timeseries-retention.timer
```

Notes:

- Do **not** delete `~/node27-product-archive-logs/receipt.json` or
  `~/node27-storage-inventory-audit-logs/completeness-receipt.json`; they
  are the historical evidence chain and are consumed byte-for-byte by
  #855 retention.
- ADR 0002 makes archive+delete atomic per-cycle — there is no per-run
  data-level rollback because a completed archive terminal has already
  fsynced a verified pair before source retirement. To roll back a
  specific archived pair back to the object store, follow the salvage
  restore procedure in the (future) `db-export-salvage` runbook section
  or the archive rebuild drill (#5.x).
