**分册：已知卡点：展示 / ingest / forcing / scheduler**

本页是当前生产值守手册的 §8 开篇、§8.1-§8.7 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 8. 当前已知卡点

### 8.1 Display port drift

Symptom:

- `http://127.0.0.1:8080/health` fails or public `https://test.nwm.ac.cn/health`
  returns 502.

Check:

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
grep -E '^NHMS_DISPLAY_API_PORT=' infra/env/display.env
ss -ltnp 2>/dev/null | grep -E ':(8080|8000)\b'
curl -fsS --max-time 5 http://127.0.0.1:8080/health
curl -fksS --max-time 5 https://test.nwm.ac.cn/health
```

Fix:

```bash
cd /home/nwm/NWM
bash scripts/ops/start-display-api.sh
```

If `display.env` disagrees with nginx, back up the env file first, align the
port, restart through the wrapper, and verify both local and public `/health`.

### 8.2 Autopipe ingest failures

Symptoms:

- `/home/nwm/autopipe-logs/autopipe.log` shows repeated non-zero rc.
- JSON summary has non-empty `failed_runs`.
- New `object-store/runs/fcst_*` directories exist but DB `hydro.hydro_run`
  does not advance.

Checks:

```bash
ssh -p 32099 nwm@210.77.77.27
tail -n 240 /home/nwm/autopipe-logs/autopipe.log
cd /home/nwm/NWM
bash scripts/node27_autopipe_cron.sh
```

The wrapper uses the same env defaults, log path, and non-overlap lock as cron.
It is idempotent; rerun manually only after reading the previous failure and
confirming no cron run is active.

### 8.3 Forcing handoff parse failures

Symptoms:

```text
FORCING_DOMAIN_HANDOFF_UNAVAILABLE
checksum mismatch
mixed native_resolution labels for one valid_time
```

Impact:

- node-22 has completed run/output trees under the object store.
- node-27 autopipe skips or fails the affected run before DB ingest.
- `/api/v1/runs` is missing the basin/cycle even though SHUD output exists.

Boundary:

- Do not manually edit DB status to hide the issue.
- Repair the handoff payload/checksums or regenerate the forcing package, then
  rerun node-27 autopipe.
- Judge display readiness with parsed hydro output, layer publication logs, and
  node-27 API coverage.

### 8.4 `/ghdc` 与计算节点边界

Facts:

- node-22 can access `/ghdc/data/nwm/...`.
- Slurm compute nodes should not assume `/ghdc` is their runtime workspace.
- Compute intermediates belong under `/scratch/frd_muziyao/nhms-prod/...`;
  completed shared artifacts appear under `/ghdc/data/nwm/...` and then
  `/home/ghdc/nwm/...` on node-27.

If a Slurm job fails because `/ghdc` is missing, runtime roots are wrong. Fix
the compute-side workspace/object-store config rather than moving display paths
into sbatch runtime.

### 8.5 Node-22 scheduler stuck after missing forcing artifact

Accepted-submit restart reconciliation is configured by
`NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS` (production example: 300 seconds).
Values outside 30–3600 seconds fail closed at scheduler configuration time.
`NHMS_SCHEDULER_RECONCILE_SLURM_USER` and
`NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT` must match the `sacct` owner of jobs
submitted by node-22; an owner, comment, master, task-prefix, stage, or cohort
identity mismatch remains reconciling and cannot project candidate state.
`NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` (default 3, `<= 0` disables)
bounds how many consecutive `identity_mismatch_blocked` passes such a
reserved-unbound row may accumulate before it is released to
`reservation_lost` / `identity_mismatch_released`. When disabled (`<= 0`) the
`identity_blocked_streak` counter freezes at its current value instead of
counting (`0` only for rows that never counted; a row that reached `2` under an
enabled exit keeps reporting `2`), so read no-progress off the repeated
`identity_mismatch_blocked` outcome rows for the same `job_id`, not off the
counter; see
[`failed-basin-retry.md`](../failed-basin-retry.md) for the disposition of released
rows and of `blocked_strict_warm_start_init_state_mismatch` candidates.

Symptoms:

- `nhms-compute-scheduler.service` consumes CPU with no new Slurm job and no
  advancing file-journal evidence.
- Reconcile records `SLURM_RECONCILE_UNVERIFIED` for a Slurm job that `sacct`
  reports terminal.
- A previously completed cycle/basin is selected again because an older
  `hydro_run.status` row still says `created`.
- Forecast retry fails as a generic runtime/node failure while stderr shows a
  missing `forcing_package_uri` object-store tree.

Do not treat an ordinary unfinished convert/forcing resume as this missing-forcing
incident. Shared convert success with `restart_stage=forcing`, strict warm-start
ready, and no forcing package yet is unfinished pre-forecast work: ordinary
forcing remains eligible on a later scheduler pass, subject to existing
scheduling guards. Exact-cycle `--repair-missing-forcing` is for a
forecast/later retry that is already blocked by `forcing_version_row_absent` /
`FORCING_VERSION_ROW_ABSENT` (or `missing_forcing_package_uri` /
`FORCING_PACKAGE_URI_MISSING`) after a model republish or other per-model
witness gap. Using the repair wrapper on convert-only candidates does not
require an operator repair marker.



Safe online mitigation:

1. Keep node-22 compute-only. Node-22 local PostgreSQL `:55433` is historical,
   archived, and stopped — do not connect it as a current runtime dependency.
2. The default missing-forcing policy is fail-closed. For a package that can be
   regenerated, use the exact-cycle wrapper described below; do not edit the
   file journal, submit forecast directly, or switch a warm candidate to cold.
   Restoring preserved forcing bytes remains valid only when the preserved
   package, checksum, source/cycle/model identity, staging object-store path,
   and shared NFS copyback root all match.
3. Clear only stale scheduler locks whose PID is dead or whose live pass was
   intentionally stopped; preserve the stale-lock evidence JSON.
4. Restart scheduler from the latest merged code, not by hand-editing journal
   rows as a normal operating path.

Exact-cycle missing-forcing regeneration (node-22 only):

1. Confirm that the affected candidates are blocked only by
   `missing_forcing_package_uri` / `FORCING_PACKAGE_URI_MISSING` (package
   determined absent) or `forcing_version_row_absent` /
   `FORCING_VERSION_ROW_ABSENT` (no provenance tier — journal row, journal
   direct file, or object-store forcing-version sidecar — could witness the
   package); the repair channel accepts both reason/classifier pairs, and the
   repair action is the same idempotent exact-cycle forcing rebuild.

   A `forcing_version_row_absent` blocker is only repairable by a rebuild when
   the tier that failed is a data tier. Route by
   `state_evidence.forcing_provenance.tier_status`:

   | `tier_status` | Fault | Does an exact-cycle forcing rebuild fix it? |
   |---|---|---|
   | `sidecar_absent` | No forcing-version sidecar for this cycle | Yes — rebuild writes the package, sidecar, and manifest |
   | `sidecar_malformed` | Sidecar unparseable or names no package | Yes — rebuild rewrites the sidecar |
   | `sidecar_unreadable` (permission/IO class) | Reading the sidecar *record* was denied or failed | No — fix the store permissions/mount first |
   | `sidecar_oversized` | The sidecar record exceeds the read limit | No — the record itself is anomalous; investigate the producer/lineage that wrote it first |
   | `sidecar_manifest_probe_error` | Object-store read fault on the manifest object (symlinked leaf, stale NFS handle, permissions) | No — a rebuild cannot clear a read fault; fix the object/mount first |
   | `store_unconfigured` | No `object_store_root` for this candidate | No — the rebuild could not even write; fix the config first |
   | `identity_incomplete` | Candidate has no `basin_version_id`/`model_id` | No — fix the registry/candidate identity first |

   For the five config/identity/read-fault statuses the rebuild will NOT clear
   the blocker; repeating it only burns a cycle. Correct the configuration,
   identity, or storage fault, let the next pass re-read the tiers, and only
   then repair if a data tier is still the fault.

   Also confirm that
   `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`; and that the current registry has
   18 source-scoped variants for each enabled source. The raw readiness record
   must say `status=ready`, `required=true`, and
   `source=node27_nfs_raw_manifest`. The scheduler re-reads the manifest and all
   referenced raw files from the trusted
   `NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT`; the redacted public journal value
   `[local-path]` is never used as an operational path. A stale journal `ready`
   value alone cannot authorize repair. Every admitted basin must also have a
   complete warm state identity (`id`, URI, checksum, valid time, and warm
   lineage); missing, partial, cutover/cold-new, or cold state remains blocked.
   Provision these values from the tracked
   `infra/env/compute.scheduler-dbfree.env.example` into the ignored live
   `infra/env/compute.scheduler-dbfree.env` (do not edit or commit the live
   file):

   ```bash
   NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
   NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true
   NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT=/ghdc/data/nwm/object-store
   NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX=s3://nhms
   ```

   Both variables are bindings to the fixed node-22 topology authority; neither
   variable defines that authority. Runtime preflight requires both roots to
   resolve to the canonical directory and to each other, so moving them together
   to an allow-listed staging directory still fails before lock acquisition or
   repair work. Public evidence records only redacted path placeholders and
   boolean identity results.
   `/ghdc/data/nwm/object-store` is node-22's view of node-27
   `/home/ghdc/nwm/object-store`; it must remain in
   `NHMS_SCHEDULER_ALLOWED_ROOTS`. It is not the compute-visible staging root
   under `/scratch`; an allow-listed staging path cannot replace either
   authority value.
2. Preview the exact UTC cycle. Omitting `--source` intentionally previews both
   configured GFS and IFS cohorts; omitting `--basin-id` retains all 18 active
   basins per source:

   ```bash
   cd /scratch/frd_muziyao/NWM
   scripts/ops/node22-run-cycle-once.sh \
     --cycle-time 2026-07-12T00:00:00Z \
     --repair-missing-forcing \
     --plan
   ```

3. Inspect the evidence artifact printed by the wrapper. Every admitted repair
   must contain:

   ```text
   state_evidence.missing_forcing_repair.status = authorized
   state_evidence.missing_forcing_repair.restart_stage = forcing
   state_evidence.missing_forcing_repair.slurm_stage = produce_forcing_array
   state_evidence.missing_forcing_repair.login_node_forcing = false
   state_evidence.cold_fallback_allowed = false
   ```

   Read `state_evidence.forcing_provenance` on the same record to see which
   provenance tier the blocker came from:

   ```text
   state_evidence.forcing_provenance.source = journal | direct |
                                              object_store_sidecar | absent
   state_evidence.forcing_provenance.tier_status = <sidecar tier detail, only
                                                    when source = absent>
   state_evidence.forcing_provenance.probe       = manifest | package_uri
   state_evidence.forcing_provenance.probe_key   = <object key that was
                                                    actually probed>
   state_evidence.forcing_provenance.artifact_exists = true | false
   state_evidence.artifact_guard.unsafe_reason   = <why the probe refused the
                                                    reference, or null>
   ```

   - `tier_status` names which provenance tier failed, and routes the repair
     decision through the table in step 1.
   - `probe` / `probe_key` name the object the existence probe was actually
     given. On the journal/direct tiers (the recorded `forcing_package_uri`):
     `probe = manifest` means the record held a package *prefix* (with or
     without a trailing `/`) and the probe used the derived witness manifest
     file key `<package prefix>/forcing_package.json`; `probe = package_uri`
     means the record was already a valid file key and was probed verbatim. On
     the `object_store_sidecar` tier `probe_key` is always derived from this
     candidate's own identity — compare it against `manifest_uri`, which is only
     what the record *claimed*: a mismatch means the record points somewhere
     other than this candidate's package.
   - `probe_key` is stamped *before* the probe runs. If
     `artifact_guard.unsafe_reason` is `object_store_root_unconfigured` or
     `artifact_probe_error`, that key was **not** probed at all —
     `unsafe_reason` is the authoritative verdict, not `probe_key`.
     Do **not** generalize that to "a non-null `unsafe_reason` means nothing was
     probed": `artifact_target_not_a_file` is the opposite case — the probe ran,
     reached `probe_key`, and *determined* that something other than a regular
     file stands there.
   - `artifact_exists` says whether that probed manifest object was found *as a
     regular file*. `false` on an `object_store_sidecar` source is still never a
     read failure — a read fault leaves that tier as `source = absent` with a
     read-fault `tier_status` instead — but since #1394 it covers two different
     determinations, so read it together with `unsafe_reason`: a genuinely absent
     package (`unsafe_reason` is `null`), or something other than a regular file
     standing on the derived witness key (`artifact_target_not_a_file`). Only the
     first one is a missing package.
   - `artifact_guard.unsafe_reason` says why the probe refused or could not use
     the reference; `null` usually means the reference was probeable and simply
     not found ("probed, determined absent"), but read it together with
     `forcing_provenance.tier_status` — on the `object_store_sidecar` tier a
     read fault surfaces as `forcing_version_row_absent` with a read-fault
     `tier_status` and a **null** `unsafe_reason`, and there the rebuild is
     ineffective. That null-reason shape covers the tier's *read faults* only:
     a non-regular witness target is a determination, so on that tier it surfaces
     as the ordinary `missing_forcing_package_uri` blocker carrying a **non-null**
     `artifact_target_not_a_file`, the same as on the journal and direct tiers.
     Route by this table:

   | `unsafe_reason` | Fault | Does an exact-cycle forcing rebuild fix it? |
   |---|---|---|
   | `null` | The probe ran and the package is genuinely absent — **unless** `forcing_provenance.tier_status` is a read-fault status (see below), or the recorded reference is malformed enough that no probe could resolve it | Usually yes — that is exactly what the rebuild repairs. A malformed unresolvable reference is also repairable (the rebuild re-records it). **But** if `tier_status` is a read-fault status, no: on the `object_store_sidecar` tier `tier_status`, not `unsafe_reason`, is the authoritative verdict |
   | `object_store_root_unconfigured` | Neither the candidate's `object_store_root` nor `OBJECT_STORE_ROOT` is set, so no probe ran | No — the remedy is configuration; fix it and let the next pass re-probe |
   | `artifact_probe_error` | The object store refused the stat (symlinked witness leaf or ancestor, stale NFS handle, permissions) | No — a rebuild cannot clear a filesystem fault; fix the object/mount/permissions first |
   | `artifact_target_not_a_file` | The probe **did** run and found something at `probe_key` that is not a regular file — almost always a directory squatting on a file key (a leftover placeholder, an interrupted writer, a rsync that created the path as a directory); a FIFO, socket or device node is judged the same way. Applies to the object leg and the local leg alike — but the two name their target differently, so inspect per leg | No — the rebuild would have to write a file where a directory stands (`IsADirectoryError`). Inspect it first. **Object leg**: `probe_key` is the *recorded reference* with the manifest filename appended — it is store-relative only when the recording was. The `object_store_sidecar` tier derives a bare key, so `ls -ld "$OBJECT_STORE_ROOT/$PROBE_KEY"` works there directly; the journal/direct tier stamps the producer's recorded uri verbatim, which under every tracked config carries an `s3://<bucket>[/<prefix path>]` head (`OBJECT_STORE_PREFIX`). Strip that head before joining — `ls -ld "$OBJECT_STORE_ROOT/${PROBE_KEY#"$OBJECT_STORE_PREFIX"/}"` — exactly as `normalize_object_key` does, and percent-decode as well if the recorded value carries `%XX`. **Local leg**: `probe_key` is already an absolute local path (or a `file://` uri) — `ls -ld` it directly with any `file://` stripped, and do **not** prefix the store root; also note this leg stamps `forcing_provenance` only for a journal/direct `forcing_version` that names the same uri, so it can be `null` entirely and then there is no `probe_key` at all. **Copyback leg**: never stamps a `probe_key` of its own — so check `artifact_guard.artifact_type` FIRST: when it is `copyback_source`, use `artifact_guard.artifact_uri` even if a `probe_key` is present, because that `probe_key` was stamped by the forcing leg earlier in the same pass and names the forcing manifest, not the squatted copyback path. Whenever `probe_key` is missing, the reference is `artifact_guard.artifact_uri`. Remove the placeholder once you are sure it holds no wanted data, then let the next pass re-probe or re-run the rebuild |
   | `invalid_local_artifact_path` / `local_artifact_path_outside_allowed_roots` / `local_artifact_path_unresolvable` | A local-path reference is unresolvable or outside the allowed roots | No — fix the path, or the roots this probe actually consults: resource-profile keys `object_store_root` / `object_store_copyback_root` / `copyback_root` / `published_artifact_root` plus env `OBJECT_STORE_ROOT` / `NHMS_OBJECT_STORE_COPYBACK_ROOT` / `NHMS_PUBLISHED_ARTIFACT_ROOT` (**not** `NHMS_SCHEDULER_ALLOWED_ROOTS`, which feeds a different mechanism and is never read here) |
   | `local_artifact_root_unresolvable` | **A configured artifact ROOT itself could not be canonicalized**, and no remaining resolvable root contains the artifact. Investigate the root, **not** the artifact's placement: a symlink loop in the root chain, a directory the scheduler user cannot traverse (EACCES), a stale NFS handle (ESTALE) or an unmounted/half-mounted share, a non-directory component (ENOTDIR). Not only symlink loops — **every** errno other than `ENOENT` lands on this reason; `ENOENT` alone (a root that simply does not exist yet — including forms like `<missing>/../<loop>` where a missing component is hit before the loop) stays admitted and never produces this reason. Same root list as the row above | No — a rebuild cannot clear a filesystem or mount fault. Fix or unmount/remount the offending root (`readlink -f "$ROOT"` and `ls -ld` on each component reproduce the kernel's verdict), then let the next pass re-probe |

   A blocker with a non-null `unsafe_reason` is rejected by the authorized
   repair channel as `forcing_artifact_reference_unsafe` (see the rejected
   reasons below). That is deliberate: a rebuild cannot cure a configuration or
   filesystem fault.

   `source = absent` with a config/identity/read-fault `tier_status`
   (`store_unconfigured`, `identity_incomplete`, a permission-class
   `sidecar_unreadable`, `sidecar_oversized`, or `sidecar_manifest_probe_error`)
   means the rebuild cannot clear the blocker — see the routing table in step 1.

   A rejected preview retains the original missing-forcing blocker and records
   a stable reason such as `raw_manifest_not_ready`,
   `raw_manifest_identity_mismatch`, `candidate_not_direct_grid`, or
   `exact_cycle_identity_mismatch`. Fix the stated precondition; do not bypass
   it.

   One reason is **not** a precondition to fix: `operator_reentry_confirmation_present`
   (r2-01) means this candidate carries an operator re-entry confirmation
   (`confirm-operator-reentry`), and the repair channel refuses it by design.
   The reclassified repair retry restarts at `forcing`, while the re-entry
   provenance is stamped only at a forecast-cohort reservation — so **whether
   that submission moves the count depends on a stage the operator did not
   authorize**: a `forcing` stage that succeeds does reach the reservation and
   does stamp, while one that fails has submitted for real, moved nothing, and
   left the signature armed (plus a new run-id prefix that resets the
   stage-scoped attempt). That dependence is the reason it is refused, not a
   claim that the count could never move. The evidence echoes
   `missing_forcing_repair.confirmation.{decision,request_id}`.

   Remedy: **do not** use `--repair-missing-forcing` for it. Restore that
   model's own forcing first, then let the confirmed re-entry run on an ordinary
   pass — it restarts at `forecast` and consumes the signature exactly once.
   Which channel restores it depends on the cause:

   - **The model's forcing exists under a different model id** (a rename; the
     rename set is non-empty): use `scripts/node22_backfill_forcing_for_model_ids.py`,
     documented in §3.1.1 hop 5 above. It is a **rename-only** tool — it requires
     both the pre- and post-rename registry manifests
     (`scripts/node22_backfill_forcing_for_model_ids.py:605-606`) and derives its
     work solely from the rename set (`discover_work` at `:409`, `for rename in renames`
     at `:420-421`; the `resolve_renames → probe_coverage → discover_work` pipeline
     in `main` at `:648-651`).
   - **Any other cause** (e.g. retention deleted `forcing/<source>/<cycle>/...`
     under the primary root — `services/orchestrator/retention.py:75`, `:684-687`):
     that tool is **not** the channel. It returns `work_item_count: 0` (receipt
     fields at `:705` / `:715`), a shape §3.1.1 itself calls indistinguishable
     from a misconfigured `--forcing-root` / unmounted NFS by item count alone.
     **Escalation path:** repair the pre-forecast input out of band — re-produce
     that model's forcing package into the object store (or repair the canonical
     readiness / raw manifest identity that provoked the rewrite) — and leave the
     candidate blocked until it is fixed. The confirmation stays armed; the next
     pass then restarts the confirmed re-entry at `forecast`, stamps it at the
     reservation, and moves the count exactly once. Whether the retention frontier
     pins blocked or confirmed candidates is **not measured**; do not assume
     either way. That design gap is tracked as #2412.

   A second by-design refusal sits right after it:
   `journal_predecessor_quarantine_present` (#2408) means this **unconfirmed**
   blocker descends from a §8.7 journal-predecessor quarantine retry
   (`retry_journal_predecessor_identity_mismatch`) that landed on the stable
   missing-forcing blocker. It is a real re-run of the stale lineage;
   reclassified, it would restart at `forcing`, where no quarantine provenance
   is ever stamped (the same forecast-cohort reservation argument as r2-01), so
   a failing forcing stage would submit without moving the breaker count.
   Triage fields: `missing_forcing_repair.recorded_init_state_id`
   (the stale predecessor token) and `missing_forcing_repair.expected_init_state_id`
   (the token the journal expects), mirroring `journal_predecessor_identity`.

   Remedy: same as r2-01 — **do not** use `--repair-missing-forcing` for it;
   restore that model's own forcing (`scripts/node22_backfill_forcing_for_model_ids.py`
   when the rename set is non-empty, otherwise out of band as described above).
   The quarantine retry then restarts at `forecast`, is stamped at the
   reservation, and is counted.
4. Submit the same exact cycle only after the preview admits the intended set:

   ```bash
   cd /scratch/frd_muziyao/NWM
   scripts/ops/node22-run-cycle-once.sh \
     --cycle-time 2026-07-12T00:00:00Z \
     --repair-missing-forcing \
     --submit
   ```

   The process-scoped flag is cleared by the wrapper when omitted on later
   invocations, even if a stale env file contains it. The scheduler rejects the
   repair mode for continuous, backfill, multi-cycle, missing, or malformed
   exact-cycle use.
5. Acceptance requires one `produce_forcing_array` cohort per source with 18
   members (for the current registry) and Slurm array throttles whose concurrent
   total is at most 32. The subsequent forecast stage must retain each basin's
   selected `init_state_*` and lineage. Login-node `ForcingProducer` calls,
   forecast-only submission, cold fallback, a different cycle, or a raw
   identity mismatch are failures, not degraded success.

Business-readiness receipt after fix:

- `nhms-compute-scheduler.service` and timer run with
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, no `DATABASE_URL`, and
  `NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND=32` plus a Slurm resource
  profile whose `max_concurrent=32`. The receipt must show multi-task
  `produce_forcing_array` submissions whose simultaneous array throttles sum
  to at most 32. `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` only bounds
  source/cycle cohort control threads; it is not accepted as forcing-concurrency
  proof. GFS and IFS cohorts may overlap and synchronize at pass finalization.
- The cohort run id carries a stable digest of its candidate membership. A
  filtered drill and the full registry pass must not share the same array
  idempotency key; adding a basin must produce a new cohort identity.
- The emergency one-at-a-time override is removed or disabled.
- The receipt includes at least two eligible candidates or array tasks; a
  no-work pass proves safe daemon behavior but does not prove business
  operation.
- Slurm evidence binds terminal status to submitted manifest/task/stdout or
  file-journal identity. Generic job names such as `nhms_forecast` alone are not
  sufficient to mark success.
- Scheduler evidence shows duplicate-free file-journal progress and lock release
  after the pass.

#### 8.5.1 Withheld copyback source (`COPYBACK_SOURCE_WITHHELD`)

A candidate blocked with reason `copyback_source_withheld` / error code
`COPYBACK_SOURCE_WITHHELD` is **not** a missing-forcing blocker and none of the
triage tables above apply to it. The blocker means the copyback source reference
the scheduler resolved was a redaction placeholder (`[object-uri]`, `[uri]`,
`[local-path]`, `[redacted]`, `sha256:[redacted]`): the public-read redaction
boundary withheld the value, so existence could not be determined. Read it as
"cannot determine", not "source determined absent" — that is what distinguishes
it from `missing_copyback_source` / `COPYBACK_SOURCE_MISSING`, which does mean a
probe ran and found nothing.

- `artifact_guard.unsafe_reason` is `null` here because **no probe ran at all**.
  The §8.5 `unsafe_reason` table above is keyed on probe verdicts and does not
  cover this blocker; do not read its `null` row as "probed, determined absent".
- `artifact_guard.artifact_uri` is the placeholder itself, i.e. the evidence that
  the reference was withheld rather than absent.
- The exact-cycle forcing rebuild does **not** apply: it repairs forcing
  packages, and this blocker names a copyback reference. Running it burns a cycle
  and clears nothing. The blocker is also refused by the missing-forcing repair
  authorization channel by design.
- Whether a manual retry request helps depends on which arm the candidate rides,
  so check for a failure signal (failed pipeline status, failed hydro run, or a
  failed job row) before choosing:
  - **Failure-state candidate** (the common case, and the one this blocker's
    regression tests pin): a manual retry request **does** pre-empt this blocker
    and re-submits the candidate, exactly as it does for the missing-forcing
    blockers. Note it **bypasses, not clears**: the withheld reference is
    untouched, so if the resubmitted run fails again the blocker reappears on the
    next pass.
  - **Completed-stage resume candidate** (no failure signal at all; the state
    carries `completed_stage_evidence` with a `copyback` restart stage): that arm
    is evaluated *before* the manual-retry branch, so a manual retry request has
    no effect and the candidate stays blocked. There is no operator clearing path
    for this arm today — the DB-free public read re-redacts the reference on every
    pass. Defining one depends on a copyback write side that does not exist yet;
    it is tracked in issue #1464. Report the occurrence — the geometry is latent
    in production and a live instance is itself the signal.

### 8.6 Heihe 底图和 DB 范围混用

Current DB registered Heihe data uses `/home/ghdc/nwm/Basins/...` on node-27.
Older static basemap scripts may have used repository-local fixtures with a
smaller extent. For live display and ingest, use the node-27 Basins source of
truth.

### 8.7 Heihe 河段两层模型

Heihe DB river network has GIS display segments and SHUD output segments.
`hydro.river_timeseries.q_down` attaches directly to SHUD output segments.
GIS segments map through `properties_json->>'iRiv'`. If an API/frontend query
uses GIS segment ids directly, some segments can appear to have no flow.
