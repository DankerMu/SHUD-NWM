## Fixture

- Issue: #3087 · Fixture level: **expanded** (file delete behaviour on a shared NFS tree, production config, lock contention with node-22 copyback).
- Risk packs:
  - **File IO / path safety** (selected): only real `<lower-case segment>/<10-digit cycle>` dirs; symlinks/files never listed → 1.2, 3.1.
  - **Concurrency / shared state** (selected): every forcing rmtree under the copyback mutex, shared budget, canonical first → 1.4, 3.2.
  - **Config** (selected): strict `FORCING_DAYS` parser, default 30, floor 14 independent of lane selection, installer LANES → 1.3, 2.1, 3.3, 3.4.
  - **Auth / permissions** (selected): lane runs only in the system unit as `frd_muziyao` (owner of `forcing/`); nwm unit keeps `raw,precip-cache` → 2.1, 3.4, 4.4 pre-check `find forcing -mindepth 1 ! -user frd_muziyao | wc -l` = 0 (every depth: rmtree needs write on basin/model dirs too).
  - **Schema** (selected): summary gains `forcing_root` / `forcing_retention_days` / `forcing_cutoff`, additive, `SCHEMA_VERSION` unchanged → 1.5, 3.5.
  - **Resource limits** (selected): one forcing cycle is ~4.5G vs ~2.2G canonical, so the lock is held longer per tree → 4.4 records one forcing removal's lock hold time.
  - **Legacy compatibility** (selected): unset LANES and the config default keep the legacy three lanes → 1.1, 3.5.
  - **Error handling / partial outputs** (selected): lock failure keeps the tree, `failed[]` + non-zero exit → 3.2.
  - **Documentation** (selected): env example, `retention.py` docstring, runbook, system unit comments; the jq program stays intact → 2.2.
  - Public API / Release: not selected (no API, no DB, no dependency change).

## 1. Runner

- [x] 1.1 Lane vocabulary: `forcing` accepted in `ALL_LANES`; unset LANES and the `RawRetentionConfig.lanes` default = `DEFAULT_LANES` (raw, canonical, precip-cache).
- [x] 1.2 Forcing lane via the existing mapped-lane walker with an injected segment function `normalize_source_id(src).lower()`, lane root `<root>/forcing`, skip prefix `forcing`.
- [x] 1.3 Strict `NODE27_RAW_RETENTION_FORCING_DAYS` parser (unset/blank = 30, digits only); `FORCING_RETENTION_FLOOR_DAYS = 14`; blockers `not_an_integer` / `must_be_positive` / `below_floor`, checked whether or not the lane is selected; `RawRetentionConfig.forcing_retention_days` default 30.
- [x] 1.4 Collection order raw → canonical → forcing → precip-cache (forcing before the precip-cache early returns); forcing targets removed through the copyback-mutex helper on the shared budget.
- [x] 1.5 Summary base gains `forcing_root`, `forcing_retention_days`, `forcing_cutoff` (also when `disabled`).
- [x] 1.6 `scripts/node27_raw_retention.py` < 1000 lines (or split by responsibility); `.large-file-guard.json` unchanged.

## 2. Deployment + docs

- [x] 2.1 `scripts/node27_canonical_retention_install.sh` renders `NODE27_RAW_RETENTION_LANES=canonical,forcing`; `FORCING_DAYS` passes through; system unit comments updated.
- [x] 2.2 Update `infra/env/node27-raw-retention.example` (lanes, FORCING_DAYS, floor and its coupling to `AUTOPIPE_MAX_CYCLE_AGE_DAYS`; jq program untouched), the `services/orchestrator/retention.py` docstring (copyback `forcing/` owned by the node-27 forcing lane), and the canonical-unit section of `docs/runbooks/production-ops/artifacts-and-storage.md`.

## 3. Tests (each new test red against the pre-change source)

- [x] 3.1 Spec scenario "an aged forcing cycle is removed under the mutex": aged real `gfs` and `ifs` cycles deleted; in-window kept; `tmp` dir `unparseable_cycle_name`; symlink and file untouched and unlisted.
- [x] 3.2 Lock held elsewhere → forcing tree kept, `failed[]` `lock_timeout`, counted, non-zero exit; a successful removal happens while the real lock file is held.
- [x] 3.3 `FORCING_DAYS` `13` / `0` / `abc` with lanes `raw,precip-cache` → preflight blocked, non-zero exit, nothing removed; drift test pins 14 against `services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1` and `infra/env/node27-ingest.example` `AUTOPIPE_MAX_CYCLE_AGE_DAYS`.
- [x] 3.4 Installer renders `canonical,forcing` and passes `FORCING_DAYS` through.
- [x] 3.5 Unset LANES with aged forcing trees → no forcing target, no `forcing` skip key; `raw,precip-cache` and `canonical` each add one `forcing` `lane_not_selected`; the five pinned assertions (design "Must preserve") updated by exactly the new fields/entry; every other existing test unchanged.

## 4. Verification / Evidence Floor

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 `uv run pytest -q tests/test_node27_raw_retention.py tests/test_node27_raw_retention_copyback_mutex.py tests/test_node27_raw_retention_canonical_deployment.py tests/test_copyback_guard.py tests/test_retention_extra_roots.py` + selector-chosen set
- [x] 4.3 `openspec validate add-node27-forcing-retention-lane --strict --no-interactive`
- [ ] 4.4 node-27 live receipt (post-merge deploy):
  - pre-check: `find forcing -mindepth 1 ! -user frd_muziyao | wc -l` = 0 at every depth (rmtree needs write on basin/model subdirs; a foreign-owned subdir leaves a half-deleted cycle);
  - rerun the installer, then `systemctl start nhms-node27-canonical-retention.service`;
  - from the summary JSON: `forcing_cutoff`, the `deleted[]` keys starting with `forcing/`, and the kept cycles all match `ls forcing/<S>`; `failed[]` is empty; record one forcing removal's lock hold time;
  - station-series returns 200 for an in-window cycle and 404 `STATION_FORCING_FILE_NOT_FOUND` for a deleted one.
- [ ] 4.5 (post-catch-up, open) node-22 `runs/` 30-day sweep receipt from one scheduler pass after `NHMS_RETENTION_DRY_RUN=false` is restored.
