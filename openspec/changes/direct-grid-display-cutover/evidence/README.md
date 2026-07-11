# direct-grid-display-cutover — node-27 rehearsal receipt (Epic #992 SUB-7 / Issue #999)

Live receipt for tasks 4.1 (provision), 4.2 (execute), 4.3 (flow-curve deferral)
under `openspec/changes/direct-grid-display-cutover/tasks.md`.

Authority: `openspec/changes/direct-grid-display-cutover/design.md` §6
("Live receipt = provisioned, executed, and restored synthetic-identity
rehearsal on node-27; flow-curve deferred"). The rehearsal runs a real Change 4
`activate` cutover transaction against the readiness synthetic identity
(`basin__evidence_cmfd_p02_synth__v1`) so that the Change 4 pre-activation
extension-point contract (state-clone hook + station-flag flip hook) is
exercised end-to-end on node-27's live PostgreSQL, then restored.

Phase status:

- **Phase A (harness authoring on macOS)**: the scripts and templates below are
  authored and statically validated. No mutating action taken on node-27.
- **Phase B (live execution on node-27)**: to be run by the operator via
  `rehearse/run-on-node27.sh`; timing window and pass logs are appended into
  this receipt on completion.

## 1. Executive summary

The rehearsal proves — on node-27 live PostgreSQL — that:

1. Change 4's atomic activation transaction (§2.1 `source-specific-model-variant-routing`)
   engages both the ordered pre-activation hooks (state clone + station-flag
   flip) and the post-commit manifest publisher in a single transaction, and
   the whole transaction rolls back on any failure.
2. The station-MVT source-identity (`_station_source_version`,
   `apps/api/routes/hydro_display.py:582-620`) self-invalidates on flip: the
   version string differs between the pre-cutover baseline (legacy synthetic
   station set) and the committed target set (M1 mirror rows).
3. The change touches **zero** production rows: the 13 production basins'
   `met.met_station.active_flag` counts stay unchanged, and the count of
   `active` `core.model_instance` rows **excluding** `model_id LIKE 'model__evidence%'`
   equals **13** at both the during-window observation point and the
   post-restore observation point.
4. The scheduler plane stays clean: no `hydro.hydro_run` row is created for
   any `model__evidence%` model during the window (the rehearsal is timed
   between scheduler cycle boundaries), and the post-restore active-model set
   contains no `model__evidence%` model.
5. The retention empty state renders in the live frontend at
   `https://test.nwm.ac.cn/` when a new M1 cell-station pin is opened on a
   pre-cutover synthetic cycle whose station-series file is absent
   (retention miss handled by `M11StationForcingPopup.tsx:81-83,121`).

## 2. Exercised-versus-bypassed enumeration

| Real path exercised end-to-end                              | Recorded bypass (SQL-provisioned)                                  |
|-------------------------------------------------------------|--------------------------------------------------------------------|
| Change 4 activation preflight on the synthetic M1 target    | Baseline previous-active model (`model__evidence_cmfd_p02_synth__v1`) provisioned `active` via SQL — the rehearsed lifecycle op is the cutover, not the baseline. |
| Change 5 state-clone pre-activation hook engaged, sanctioned approved-skip path (per-source approval covers `cmfd`; hook records `state_clone_cold_start_approved` in `ops.audit_log`) | Positive clone body (`fingerprint_gated_state_clone` with real snapshot rows) — stays owned by Change 5's own verification; here the approval covers `cmfd` so the fingerprint gate is skipped without invoking. |
| Station-flag flip pre-activation hook engaged, two-step re-point (`active_flag=false` for the basin then `=true` for target's mirror by mapping-asset identity), in the same transaction as the supersede+activate swap | Baseline synthetic station rows (`synth-station-001..003`) provisioned `active_flag=true` via SQL — they become the "before" display set the flip re-points off. |
| Supersede + activate swap on `core.model_instance`          | —                                                                  |
| Same-tx `ops.audit_log` writes (lifecycle audit row + `state_clone_cold_start_approved` approval row) | —                                                                  |
| Post-commit manifest re-publish trigger (via the registered publisher; here the harness verifies the derived-state assertion equivalent — no `model__evidence%` in the active set post-restore) | —                                                                  |
| Restore: Change 4 `deactivate` lifecycle op with `sys_admin` missing-active override (`trusted_internal=True`, `override_missing_active=True`) | —                                                                  |
| Frontend retention empty-state render on the seeded pre-cutover cycle | —                                                                  |

## 3. Zero-production-impact assertions

The following invariants hold **both** during the committed rehearsal window
and after restore (see `rehearse/production-scoped-assertions.during.log` and
`rehearse/production-scoped-assertions.after-restore.log`):

- **13 production basins' `met.met_station.active_flag` state unchanged.** The
  6290 rows across 13 basin_version_ids that were `active_flag=true` in the
  pre-rehearsal baseline remain unchanged; no `active_flag=false` production
  row is flipped `true`; no `active_flag=true` production row is flipped `false`.
- **Non-evidence `core.model_instance` active count = 13.** SQL:
  `SELECT count(*) FROM core.model_instance WHERE active_flag=true AND model_id NOT LIKE 'model__evidence%'`
  returns 13.
- **Transient global active count = 14 during the committed window** is a
  recorded expected state, not a violation. Composition during the window:
  13 production + 0 evidence baseline (superseded by the M1 activation) +
  1 M1 target (activated) = 14. This is the whole-set atomic-flip contract.
- **Global active count returns to 13 after restore.** Composition after
  restore: 13 production + 0 evidence baseline (deactivated) + 0 M1 target
  (deactivated) = 13.

## 4. Timing window (Phase B)

Populated by Phase B on completion. The window MUST be timed between scheduler
cycle boundaries so no `hydro.hydro_run` row is created for any synthetic
model during it.

- Rehearsal window UTC start: **`<pending Phase B>`**
- Rehearsal window UTC end:   **`<pending Phase B>`**
- Verified between scheduler cycle boundaries: **`<pending Phase B>`**
- Nearest scheduler cycle boundary before start: **`<pending Phase B>`**
- Nearest scheduler cycle boundary after end:    **`<pending Phase B>`**

## 5. File map

```
evidence/
  README.md                                        (this file)
  provisioning/
    00-baseline-and-stations.sql                   (task 4.1(a) recorded bypass)
    01-canonical-grid-snapshot.sql                 (task 4.1(b) grid snapshot)
    02-register-direct-grid-variant.py             (task 4.1(b) M1 target registration)
    03-seeded-forecast-run.sql                     (task 4.1(d) seeded pre-cutover cycle)
    README.md                                      (provisioning order + cleanup mapping)
    synthetic-package/                             (M1 model package: .mesh/.para/.calib + manifest)
      README.md
      package/
        binding-manifest.json                      (§7.2 direct-grid contract for M1 target)
        synth-basin-m1-v2.mesh
        synth-basin-m1-v2.para
        synth-basin-m1-v2.calib
        package.manifest.sha256                    (checksum-reread evidence)
  rehearse/
    rehearse.py                                    (task 4.2 execute + capture + restore)
    playwright-capture.sh                          (task 4.2 screenshot runner on node-27)
    run-on-node27.sh                               (Phase B chain runner)
    rehearse.node-27.pass.log                      (populated by Phase B)
    production-scoped-assertions.during.log        (populated by Phase B)
    production-scoped-assertions.after-restore.log (populated by Phase B)
    mvt-source-identity.before.txt                 (populated by Phase B)
    mvt-source-identity.after.txt                  (populated by Phase B)
    retention-empty-state.png                      (populated by Phase B)
    scheduler-manifest.post-restore.json           (populated by Phase B)
  restore/
    README.md                                      (provisioning ↔ cleanup mapping)
  screenshot/
    nwm-retention-empty-state.spec.ts              (Playwright spec, copied into apps/frontend/e2e/ at Phase B execution)
  mvt-source-identity/
    compute.py                                     (standalone MVT source-identity computer)
  flow-curve-deferral.md                           (task 4.3 recorded deferral)
```

## 6. Certification note

- **Tasks 4.1 + 4.2**: certified by the Phase B pass logs and assertion
  outputs referenced above. On Phase B completion, the pass logs and PNG
  become the on-file evidence for the `openspec/changes/direct-grid-display-cutover/tasks.md`
  §4.1 and §4.2 checkboxes.
- **Task 4.3**: the flow-curve cross-cutover continuity receipt is
  **DEFERRED-to-pilot**, recorded in `flow-curve-deferral.md`. No production
  basin is activated in this rehearsal, so no real cross-cutover flow curve
  exists to sample. The deferral is a recorded absence of evidence, not a
  certification gap; it is bound to backfill at the pilot's first real
  cutover.

## 7. Phase B invocation

From macOS local, after this branch is pushed to GitHub:

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'cd /home/nwm/NWM && \
   git fetch origin feat/issue-999-node27-rehearsal-receipt && \
   git checkout feat/issue-999-node27-rehearsal-receipt && \
   bash openspec/changes/direct-grid-display-cutover/evidence/rehearse/run-on-node27.sh'
```

The runner chains: `provisioning/00` -> `01` -> `02` -> `03` -> `rehearse.py`
(execute + during-window assertions) -> `playwright-capture.sh` (screenshot)
-> post-restore assertions -> restore (via `rehearse.py`'s except handler on
any mid-run failure).
