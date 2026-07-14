## 1. File-Provider Refresh Transaction

- [ ] 1.1 Add a bounded refresh runner/wrapper and common provider atomic-write
  seam: one destination lock at a time plus optional digest/inode expected-
  preimage CAS shared by registry/manual/lifecycle/readiness/state writers;
  concurrent authoritative updates return `provider_preimage_changed`.
- [ ] 1.2 Reuse `publish_all_basin_scheduler_registry`; permit only bounded
  immutable content-addressed package orphan candidates before canonical commit
  and never auto-delete them.
- [ ] 1.3 Renew valid-except-age readiness entries through full
  `_validate_readiness_index` catalog/object verification and state entries
  through `FileStateSnapshotIndexRepository` object verification; invoke only
  the existing readiness/state publishers and reject invalid/missing inputs.
- [ ] 1.4 Implement pre-commit stat/digest preservation, old-or-new atomic reader
  behavior, phase-specific replace/post-read/receipt outcomes, certain rollback,
  and bounded identity-safe temp cleanup.
- [ ] 1.5 Define/validate
  `nhms.scheduler.file_provider_refresh_receipt.v1`: seven outcomes, closed
  64-character reasons, 1 MiB/256-item/512-character/64-residue bounds, atomic
  latest, newest-32 history, 64 GiB/250k-entry/depth-32 workspace, 4,096 orphan
  cap with first-256/total/truncated evidence, and sanitized provider evidence.
- [ ] 1.6 Reserve an exclusive mode-0600 local emergency receipt before commit;
  on primary receipt failure fsync a digest-bound `published_receipt_failed`
  v1 record and provide validation/reconstruction without provider republish;
  both channels failing is non-zero `replace_uncertain`.

## 2. Systemd and Runbook

- [ ] 2.1 Add node-22 user-systemd service/timer and env example with absolute
  repo/venv paths, mode-0600 DB-free env, private lock/work/receipt, journal,
  timeout <=2h, and cadence+jitter <168h.
- [ ] 2.2 Add static unit/env tests for no DB selectors, byte-identical install,
  scheduler-unit independence, failure rollback, and success refresh-timer
  enabled/active steady state.
- [ ] 2.3 Update `docs/runbooks/current-production-ops.md` with provider dry-run/
  refresh, manual compatibility, timer install/monitoring, phases/outcomes,
  orphan/residue handling, live proof and rollback.

## 3. Scenario-Level Regression Evidence

- [ ] 3.1 Input valid 13-model inventory plus three valid-except-age provider
  files -> exact identities/digests and published receipt; existing manual CLI
  output and scheduler consumer behavior remain compatible.
- [ ] 3.2 Input timer/manual/lifecycle overlap -> one shared lock owner, contender
  already-running, no competing replacement or false success.
- [ ] 3.3 Input readiness/state authoritative replacement between snapshot and
  commit -> expected-preimage mismatch, new entries preserved, stable reason;
  prove writers take one lock only and cannot deadlock across providers.
- [ ] 3.4 Input invalid readiness checksum/identity/forecast hours/catalog/object
  and invalid state checkpoint -> prior bytes unchanged, stable closed reason,
  no empty/timestamp-only/DB output; valid-except-age cases revalidate every
  reference before renewal.
- [ ] 3.5 Input relative/symlink/non-regular/uncontained paths, receipt/workspace
  over 1 MiB/64 GiB/250k/depth32, orphan count over 4,096, publisher/pre-replace/
  replace/fsync/post-read/primary-receipt failures and repeated success ->
  phase-correct preservation/rollback, complete old/new reader, first-256/
  total/truncated orphan evidence, certain cleanup and no secret/raw path.
- [ ] 3.6 Input primary receipt failure after commit -> reserved emergency v1
  record binds committed digests and reconstructs primary without data
  publication; primary+emergency failure -> replace-uncertain/direct validation.
- [ ] 3.7 Input systemd install/start/failure/success/rollback -> no DB/libpq env,
  scheduler timer unchanged/restored, refresh timer rolled back on failure and
  enabled/active on success, services inactive between ticks.
- [ ] 3.8 Run focused publisher/provider/systemd/scheduler tests, `uv run ruff
  check .`, and `openspec validate node22-scheduler-registry-refresh --strict
  --no-interactive`.

## 4. Node-22/Node-27 Live Recovery

- [ ] 4.1 Capture frozen SHA plus three provider hashes/evidence, unit states,
  process DB-free proof and Slurm queue; deploy by ff-only pull.
- [ ] 4.2 Install refresh units stopped; run dry-run and manual refresh. Record
  old/new schema/checksum/generated_at, exact 13 models, readiness/state entries
  and referenced-object proofs, v1 receipt, no-DB proof and identical node-22/
  node-27 NFS bytes.
- [ ] 4.3 Prove renewal used full validation/publisher paths and rejects a
  timestamp-only mutation; any missing/invalid provider blocks before scheduler.
- [ ] 4.4 Run one bounded pass no longer `db_free_registry_blocked`; bind one
  candidate/run to every actual Slurm stage job and at least one terminal
  accounting result; prove actual stages create genuinely new forcing/runs/
  states leaves rather than reuse old forcing.
- [ ] 4.5 From node-27 verify exact new source/cycle/model/run identities,
  owner/group/mode/default ACL and `nwm` access. Restore scheduler and issue-owned
  jobs; on success leave refresh timer enabled/active, on failure roll it back.
- [ ] 4.6 Commit redacted live receipts and tick this section only for the frozen
  implementation SHA after local, node-22 scheduling and node-27 NFS gates pass.
  Execute no product-archive or #856 cascade command.

## Evidence Floor

- Fixture: expanded; repair intensity: high; all selected packs and concrete
  scenario rows are in `design.md` and sections 1-4.
- Required identity chain: v1 receipt -> exact registry/readiness/state digests
  -> scheduler pass/candidate/run -> actual stage job(s)/terminal -> three new
  leaves -> node-27 ACL/access proof.
- Merge blockers: timestamp-only/DB/empty renewal, missing authoritative object
  validation, false success/rollback, partial canonical bytes, unbounded orphan/
  residue/receipt/workspace, synthetic/reused leaf, nonterminal/unbound jobs,
  incomplete unit/job restoration, or any #1065/#856 command.
- Explicit non-goals: product-archive enforce, #856/#1069-#1072, DB restoration,
  model lifecycle change, retention/compression/salvage/drill, frontend/display,
  numerical result changes and unrelated refactors.
