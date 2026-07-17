## 1. File-Provider Refresh Transaction

- [x] 1.1 Add a bounded refresh runner/wrapper and common provider atomic-write
  seam: one destination lock at a time plus optional digest/inode expected-
  preimage CAS shared by registry/manual/lifecycle/readiness/state writers;
  concurrent authoritative updates return `provider_preimage_changed`.
- [x] 1.2 Reuse `publish_all_basin_scheduler_registry`; permit only bounded
  immutable content-addressed package orphan candidates before canonical commit
  and never auto-delete them. Derive each package version from the publisher's
  own validated required/optional/CALIB/forcing source plan, excluding absolute
  host/workspace paths, and recheck that identity before immutable publication.
- [x] 1.3 Before any canonical commit, derive readiness from the newest bounded
  no-follow private GFS/IFS catalogs plus the same prospective registry model
  set; publish exactly one catalog URI/SHA/row-count-bound entry per source/model,
  validate consumer recomputation and model-set parity, and reject invalid
  newest catalogs without legacy/older fallback. Renew state entries through
  `FileStateSnapshotIndexRepository` object verification.
- [x] 1.4 Implement pre-commit stat/digest preservation, old-or-new atomic reader
  behavior, phase-specific replace/post-read/receipt outcomes, certain rollback,
  and bounded identity-safe temp cleanup.
- [x] 1.5 Define/validate
  `nhms.scheduler.file_provider_refresh_receipt.v1`: seven outcomes, closed
  64-character reasons, 1 MiB/256-item/512-character/64-residue bounds, atomic
  latest, newest-32 history, 64 GiB/250k-entry/depth-32 workspace, 4,096 orphan
  cap with first-256/total/truncated evidence, and sanitized provider evidence.
- [x] 1.6 Reserve an exclusive mode-0600 local emergency receipt before commit;
  on primary receipt failure fsync a digest-bound `published_receipt_failed`
  v1 record and provide validation/reconstruction without provider republish;
  both channels failing is non-zero `replace_uncertain`.

## 2. Systemd and Runbook

- [x] 2.1 Add node-22 user-systemd service/timer and env example with absolute
  repo/venv paths, mode-0600 DB-free env, private lock/work/receipt, journal,
  timeout <=2h, and cadence+jitter <168h.
- [x] 2.2 Add static unit/env tests for no DB selectors, byte-identical install,
  scheduler-unit independence, failure rollback, and success refresh-timer
  enabled/active steady state.
- [x] 2.3 Update `docs/runbooks/current-production-ops.md` with provider dry-run/
  refresh, manual compatibility, timer install/monitoring, phases/outcomes,
  orphan/residue handling, live proof and rollback.

## 3. Scenario-Level Regression Evidence

- [x] 3.1 Input valid 13-model inventory plus three valid-except-age provider
  files -> exact identities/digests and published receipt; existing manual CLI
  output and scheduler consumer behavior remain compatible; a real split-root
  run proves packages private-only, canonical manifest shared, consumer load
  succeeds, and private package deletion fails closed.
- [x] 3.2 Input timer/manual/lifecycle overlap -> one shared lock owner, contender
  already-running, no competing replacement or false success.
- [x] 3.3 Input readiness/state authoritative replacement between snapshot and
  commit -> expected-preimage mismatch, new entries preserved, stable reason;
  prove full refresh entrypoints take one lock only and cannot deadlock across
  providers; state checkpoint copyback serializes on that same shared lock.
- [x] 3.4 Input invalid newest readiness checksum/identity/forecast hours/
  catalog/object, scan symlink/limit, registry model-set mismatch, catalog
  mutation, and invalid state checkpoint -> prior bytes unchanged, stable
  closed reason, no older/legacy/empty/timestamp-only/DB output; bound consumer
  identity mismatch recomputes against the exact catalog.
- [x] 3.5 Input relative/symlink/non-regular/uncontained paths, receipt/workspace
  over 1 MiB/64 GiB/250k/depth32, orphan count over 4,096, publisher/pre-replace/
  replace/fsync/post-read/primary-receipt failures and repeated success ->
  phase-correct preservation/rollback, complete old/new reader, first-256/
  total/truncated orphan evidence, repair-internal pre-write budget rejection,
  exact concurrent newest-32 history, certain cleanup and no secret/raw path.
- [x] 3.6 Input primary receipt failure after commit -> reserved emergency v1
  record binds committed digests and reconstructs primary without data
  publication; full-refresh zero/short writes, reserve/finalize file and parent
  fsync ordering/failures prove durability and descriptor/slot cleanup;
  primary+emergency failure -> replace-uncertain/direct validation.
- [x] 3.7 Input systemd install/start/failure/success/rollback -> no DB/libpq env,
  scheduler timer unchanged/restored, refresh timer rolled back on failure and
  enabled/active on success, services inactive between ticks; exact current
  receipt validation precedes mutation and transitional/re-entry states are safe.
- [x] 3.8 Run focused publisher/provider/systemd/scheduler tests, `uv run ruff
  check .`, and `openspec validate node22-scheduler-registry-refresh --strict
  --no-interactive`.
- [x] 3.9 Prove identical content under different roots/repair runs has one
  package version; required, optional runtime, CALIB, or forcing byte changes
  produce a new version; a kashigeer-style existing base version therefore does
  not conflict with repaired content; mutation after planning fails before any
  immutable object or canonical provider write.
- [x] 3.10 Bind the shared scheduler registry and private Slurm worker mirror as
  one prospective generation: mirror-first/canonical-last publication with
  expected-preimage rollback, identical SHA/model count in receipt/current
  validation, scheduler-inactive systemd ordering, and stage-manifest
  `SCHEDULER_REGISTRY_MIRROR_MISMATCH` fail-closed regression coverage. Treat
  mirror/shared/readiness/state as one reverse-order rollback transaction;
  exact atomic postimages are the only rollback ownership tokens, typed
  preimage conflicts preserve the concurrent authoritative lane while earlier
  owned lanes restore, unknown tokenless writes remain `replace_uncertain`, and
  rollback CAS conflict is never relabelled as a receipt publication failure.

## 4. Node-22/Node-27 Live Recovery

- [x] 4.1 Capture frozen SHA plus three provider hashes/evidence, unit states,
  process DB-free proof and Slurm queue; deploy by ff-only pull.
- [x] 4.2 Install refresh units stopped; run dry-run and manual refresh. Record
  old/new schema/checksum/generated_at, exact current live inventory (19 models
  on 2026-07-15 after removing duplicate `HHe-MAIN-02`), GFS/IFS readiness
  entries (19 each/38 total for that
  inventory) and catalog URI/SHA/row-count bindings, state entries and
  referenced-object proofs, v1 receipt, no-DB proof and identical node-22/
  node-27 NFS bytes.
- [x] 4.3 Prove renewal used full validation/publisher paths and rejects a
  timestamp-only mutation; any missing/invalid provider blocks before scheduler.
- [x] 4.4 Run one bounded pass no longer `db_free_registry_blocked`; bind one
  candidate/run to every actual Slurm stage job and at least one terminal
  accounting result; prove actual stages create genuinely new forcing/runs/
  states leaves rather than reuse old forcing.
- [x] 4.5 From node-27 verify exact new source/cycle/model/run identities,
  owner/group/mode/default ACL and `nwm` access. Restore scheduler and issue-owned
  jobs; on success leave refresh timer enabled/active, on failure roll it back.
- [x] 4.6 Commit redacted live receipts and tick this section only for the frozen
  implementation SHA after local, node-22 scheduling and node-27 NFS gates pass.
  Execute no product-archive or #856 cascade command.

## 5. Registry Cutover Gate (#1080 follow-up)

- [x] 5.1 Add `nhms.scheduler.registry_package_cutover.v1` JSON schema at
  `schemas/scheduler_registry_package_cutover.schema.json` plus a valid
  example fixture: `schema_version`, `generated_at`, `generation`, bounded
  `entries: [{model_id, old_checksum, new_checksum, effective_cycle_utc,
  transition_mode}]`; wire the schema into the existing
  `check-jsonschema` metaschema/example loop in `.github/workflows/ci.yml`.
- [x] 5.2 Extend `schemas/scheduler_file_provider_refresh_receipt.schema.json`
  with a top-level `registry_classification` object bound to
  `previous_registry_sha256` (nullable, 64-hex or null) and
  `new_registry_sha256` (nullable), plus bounded (256-cap, `total`,
  `truncated`) arrays for `added`, `unchanged`, `removed`, `package_changed`,
  `refused`, and `declared_cutovers`. Add schema conditionals that require
  `registry_classification` on every `dry_run`/`published` outcome AND on
  every `failed` outcome whose `reason` is one of
  `registry_cutover_undeclared`,
  `registry_cutover_removal_refused`, or
  `registry_cutover_declaration_invalid`; add a corresponding receipt example.
- [x] 5.3 In `scripts/scheduler_file_provider_refresh.py`, extend
  `_registry_precommit_gate` (or introduce a helper it calls) to: bounded
  no-follow read of the previous canonical `manifest-last.json` (missing =
  first publication), classify prospective vs previous by `model_id`, load
  the cutover declaration file when `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`
  is set (bounded no-follow), validate it against the schema, and enforce
  `old_checksum/new_checksum/generation/model_id/effective_cycle_utc/
  transition_mode` semantics with UTC-cycle alignment and the 24h past /
  168h future window. Emit refusal reasons
  `registry_cutover_undeclared`,
  `registry_cutover_removal_refused`, or
  `registry_cutover_declaration_invalid` before any canonical replace.
- [x] 5.4 Attach the classification payload to the primary and emergency
  receipt paths; sanitize `model_id` and checksum fields under the existing
  bounded/redaction rules; assert canonical bytes remain unchanged on every
  refusal outcome.
- [x] 5.5 Add regression tests under
  `tests/test_scheduler_file_provider_refresh.py` (and companion fixtures)
  covering: (a) 13 previous + 6 new -> 6 added + 13 unchanged; (b) 1 existing
  `package_changed` without declaration -> refusal + previous bytes intact;
  (c) valid declaration accepts the same transition; (d) each invalid
  declaration mode (schema-invalid, wrong generation, wrong old_checksum,
  wrong new_checksum, non-cycle-aligned effective_cycle_utc, out-of-window
  effective_cycle_utc, duplicate model_id, unknown model_id, symlinked/
  non-regular declaration file, over-size declaration file); (e) previously
  canonical model removed -> refusal; (f) missing previous canonical
  registry -> every row classified as `added`.
- [x] 5.6 Extend `tests/test_publish_scheduler_file_registry.py` with a
  concurrency assertion that the precommit gate runs while the same
  destination lock is held that governs canonical replacement, and that a
  refusal releases the lock without touching canonical bytes.  Round-2
  fix pass also proves in `tests/test_scheduler_file_provider_refresh.py`
  that `config.refresh_lock` is held during the gate call (a second
  non-blocking acquire during the gate raises `ProviderAtomicError`) and
  that `expected_preimage` CAS refuses a concurrent authoritative swap
  after snapshot.
- [x] 5.7 Update `docs/runbooks/current-production-ops.md` operator section
  with: what a `registry_cutover_*` refusal means, how to file a valid
  declaration (schema, path env, cycle alignment, generation binding), and
  how the receipt's `registry_classification` should be inspected before
  enabling the refresh timer after a cutover.  Round-2 fix pass updated
  the recipe to reflect the deterministic `manifest-<12hex>` generation
  and documented the manual CLI `--allow-uncovered-cutover` flag.
- [x] 5.8 Run `uv run pytest -q tests/test_scheduler_file_provider_refresh.py
  tests/test_publish_scheduler_file_registry.py`, `uv run ruff check .`, and
  `openspec validate node22-scheduler-registry-refresh --strict
  --no-interactive`; capture the receipt example through the CI
  `check-jsonschema` metaschema/example loop.
- [x] 5.9 Round-2 invariant closure: fix `_validate_registry_classification_field`
  reconciliation semantics (published receipts MUST accept
  `package_changed>0` when covered by `declared_cutovers`); pin
  `_prospective_registry_generation` to a wall-clock-free content hash
  (`manifest-<12hex>`); expand `REGISTRY_MODEL_IDENTITY_FIELDS` and
  introduce nested `REGISTRY_MODEL_NESTED_IDENTITY_FIELDS` so drift in
  `resource_profile.source_inventory_checksum`, `basin_version_id`, etc.
  escalates to `package_changed`; add lenient receipt reader
  (`_lenient_receipt_order`) so pre-#1080 `latest.json` on disk does not
  brick the first post-#1080 publish; wire the same gate into
  `scripts/publish_scheduler_file_registry.py::main` behind a bounded
  `--allow-uncovered-cutover` flag; module-level jsonschema validator;
  bounded-read sentinels in `_load_previous_canonical_registry` and
  `_load_cutover_declaration`; bind classified snapshot bytes+SHA to a
  single read; enforce `^[A-Za-z0-9_.:-]+$` on classification `model_id`
  runtime.  Round-2 test coverage adds T1-T13 in
  `tests/test_scheduler_file_provider_refresh.py` +
  `tests/test_publish_scheduler_file_registry.py` (see round-1
  verdict summary in `.workplans/1080/review/`).

## Evidence Floor

- Fixture: expanded; repair intensity: high; all selected packs and concrete
  scenario rows are in `design.md` and sections 1-5.
- Required identity chain: v1 receipt -> exact registry/readiness/state digests
  + `registry_classification` bound to previous/new canonical SHA-256 ->
  scheduler pass/candidate/run -> actual stage job(s)/terminal -> three new
  leaves -> node-27 ACL/access proof.
- Merge blockers: timestamp-only/DB/empty renewal, missing authoritative object
  validation, false success/rollback, partial canonical bytes, unbounded orphan/
  residue/receipt/workspace, synthetic/reused leaf, nonterminal/unbound jobs,
  incomplete unit/job restoration, undeclared `package_changed`/`removed`
  admitted to canonical replacement, `registry_classification` totals that do
  not reconcile with previous+prospective model sets, or any #1065/#856
  command.
- Explicit non-goals: product-archive enforce, #856/#1069-#1072, DB restoration,
  model lifecycle change, retention/compression/salvage/drill, frontend/display,
  numerical result changes, cross-plane consumer changes (owned by #1081), live
  2026070600 recovery (owned by #1082), and unrelated refactors.
