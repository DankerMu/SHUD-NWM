## Design

**Change surface:**
- `scripts/node27_raw_retention.py`: lane vocabulary, config, preflight, `collect_targets`, the
  `run_retention` lock dispatch, and the summary base.
- `scripts/node27_canonical_retention_install.sh`: the rendered `LANES` line.
- Doc, env and unit-comment text.

**Must preserve:**
- **Unset `NODE27_RAW_RETENTION_LANES`** selects exactly `raw`, `canonical` and `precip-cache`. In that
  case no forcing root is probed, listed or locked, and no `forcing` skip entry is emitted.
- **`RawRetentionConfig.lanes` default** stays the legacy three (`DEFAULT_LANES`), so configs that
  tests build directly do not start selecting forcing.
- **Behaviour of the existing lanes:**
  - Raw, canonical and precip-cache targets are unchanged.
  - Their cutoff (`retention_days`, including the `--retention-days` CLI flag) is unchanged.
  - Their skip vocabulary, `copyback_lock_failures` counting and `SCHEMA_VERSION` string are
    unchanged.
- **Lock contract:** the canonical mutex primitive (`posix`), the shared pass-level lock budget, and the
  lock file's mode and owner contract are unchanged.
- **Large-file guard:** `.large-file-guard.json` stays byte-identical.
- **Operator jq check:** the check in `infra/env/node27-raw-retention.example` stays intact. It keys
  on `failed[]` and `*_unsafe` reasons only, so it covers the new lane, and
  `test_documented_operator_check_goes_red_on_an_unsafe_skip` still extracts it.
- **Existing tests that change on purpose** (deviation from the issue's "既有测试字节不变"). These
  tests assert exact key sets or skip lists, so they gain exactly the new summary fields and/or the
  `forcing` `lane_not_selected` entry, and nothing else:
  - `tests/test_node27_raw_retention.py:1615` (summary key set), `:1670-1673` and `:1693`
  - `tests/test_node27_raw_retention_copyback_mutex.py:481`
  - `tests/test_node27_raw_retention_canonical_deployment.py:435`

**Must add / change:**

- **D1 Lane vocabulary.**
  - `FORCING_LANE_KEY = "forcing"`, with reason `forcing_cycle_aged_out` and skip prefix `forcing`,
    so its unsafe skips end in `_unsafe`.
  - `ALL_LANES`, the accepted vocabulary, gains `forcing`. `DEFAULT_LANES` is the legacy three.
  - `_split_lanes` returns `DEFAULT_LANES` when the variable is unset.
- **D2 Segment.** The path segment is `normalize_source_id(source).lower()`.
  - `packages/common/source_identity.py` maps `gfs→gfs` and `IFS→IFS`, so the segments are `gfs` and
    `ifs`. That matches the producer's `_object_source_segment` (`workers/forcing_producer/producer.py`).
    Only IFS differs from the canonical spelling.
  - An unmappable source becomes `{"key": "forcing/<src>", "reason": "source_unmappable"}`.
  - `_collect_mapped_lane` is reused with an injected segment function, not duplicated.
- **D3 Cutoff and floor.**
  - The lane's cutoff is `forcing_cutoff = reference_time - forcing_retention_days`, on the same
    display-watermark anchor as the other lanes.
  - **Parsing.** `NODE27_RAW_RETENTION_FORCING_DAYS` is env-only, with a strict parser: unset or blank
    means 30, and anything else must be digits only. The pattern is
    `scripts/node27_autopipeline.py:_max_cycle_age_days`, not `_env_int`, which would silently turn
    `0` or `abc` into the default.
  - **Floor.** `FORCING_RETENTION_FLOOR_DAYS = 14` is a pinned literal, to keep the runner's imports
    light. It equals `max(NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1, AUTOPIPE_MAX_CYCLE_AGE_DAYS)` =
    `max(12 + 1, 14)`. A drift test pins it against `services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1`
    and against the `AUTOPIPE_MAX_CYCLE_AGE_DAYS` value in `infra/env/node27-ingest.example`.
  - **Blocker.** Non-digit input, a value of 0 or less, or a value below the floor blocks preflight
    with `{"field": "forcing_retention_days", "reason": "not_an_integer"|"must_be_positive"|"below_floor", "value": <raw>, "floor": 14}`.
    The check runs whether or not the lane is selected, because one env file feeds both units.
  - **Config field.** `RawRetentionConfig.forcing_retention_days` defaults to 30.
- **D4 Collection order.**
  - The order is: raw, then canonical, then forcing, then precip-cache.
  - The forcing block sits before the precip-cache early returns (`collect_targets` `:646` / `:649`),
    so `canonical,forcing` really collects forcing.
  - When the variable is set and forcing is not selected, the lane contributes one
    `{"key": "forcing", "reason": "lane_not_selected"}` entry. With the variable unset it contributes
    nothing.
- **D5 Lock.** A target whose reason is `forcing_cycle_aged_out` is removed through the same
  copyback-mutex helper and budget as canonical. Canonical targets come first, so canonical keeps its
  budget priority. Lock failures go to `failed[]` with `lock_failure` and are counted.
- **D6 Summary.** The base payload gains `forcing_root`, `forcing_retention_days` and
  `forcing_cutoff`, always present, `disabled` included. The change is additive only, so
  `SCHEMA_VERSION` is unchanged.
- **D7 Deployment.**
  - The installer renders `NODE27_RAW_RETENTION_LANES=canonical,forcing`.
  - `FORCING_DAYS` passes through, since it is not a per-unit key.
  - `check_source_env` still refuses an nwm env that tries to select lanes the user unit cannot own,
    if it does so today.
  - The system unit's canonical-only comments are updated.
- **D8 Size.** `scripts/node27_raw_retention.py` stays under 1000 lines; otherwise the lane
  definitions are split into a sibling module by responsibility.

**Governing invariant:** a directory under `forcing/` is deleted only when all of these hold:
- an operator selected the `forcing` lane;
- it is a real (non-symlink) `<lower-case segment>/<10-digit cycle>` directory;
- it is older than `watermark - forcing_retention_days`, with `forcing_retention_days ≥ 14`;
- the copyback batch mutex is held for that one tree.

**Sibling surfaces:**
- **Canonical lane:** shares the mutex helper and budget.
- **node-22 copyback writers** take the same `.nhms-copyback-batch.lock`:
  - `services/tile_publisher/publisher.py` `_copyback_batch_mutex`, around the forcing loop;
  - `services/tile_publisher/forcing_copyback_backfill.py:605`, once per package;
  - `services/orchestrator/run_tree_copyback.py:100`.
- **`services/orchestrator/retention.py`:** sweeps `runs/` only and is unchanged. Only its docstring
  changes.
- **Display station-series:** a deleted cycle returns 404 `STATION_FORCING_FILE_NOT_FOUND`, which is
  the existing contract. 30 days exceeds the 12-day lookback.
- **Autopipe forcing handoff:** protected by the floor.
- **Installer and units.**
- **Operator jq check.**

**Seams under test:** `config_from_env` (strict parser, floor), `collect_targets`, `run_retention`
with a real temp tree and a real lock file, the `main` exit code, and the installer's
`render_canonical_env`.

**Required evidence:**
- **Unset lanes** with aged forcing trees: no forcing target and no `forcing` key in `skipped[]`. The
  other lanes' planned/skipped output is unchanged.
- **`LANES=forcing`, `FORCING_DAYS=30`, reference 2026-11-07T00Z:**
  - The aged real `forcing/gfs/2026100612` and `forcing/ifs/2026100612` are deleted under the mutex.
  - The in-window `forcing/gfs/2026101000` is skipped as `within_retention_window`.
  - The non-cycle directory `forcing/gfs/tmp` is skipped as `unparseable_cycle_name`.
  - A symlinked `forcing/ifs/2026100100` and a plain file `forcing/ifs/README` produce no entry, are
    not listed, and are still on disk.
  - There is no case-variant directory test, because macOS volumes are case-insensitive.
- **Bad `FORCING_DAYS`:** `13`, `0` or `abc`, with lanes `raw,precip-cache`, block preflight, exit
  non-zero, and remove nothing.
- **Lock held by another process:** the forcing tree is kept, appears in `failed[]` with
  `lock_failure: lock_timeout`, is counted, and the exit code is non-zero.
- **Drift test:** the floor literal 14 equals `max(mvt NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1, ingest.example AUTOPIPE_MAX_CYCLE_AGE_DAYS)`.
- **Installer output** contains `NODE27_RAW_RETENTION_LANES=canonical,forcing` and passes
  `NODE27_RAW_RETENTION_FORCING_DAYS` through.

**Non-goals:**
- Changing `runs/` ownership or retention days (node-22 extra-root, 30d).
- node-22 primary-scratch forcing.
- Basin-level granularity.
- Display API or ingest changes.

**Review focus:**
- The segment spelling for IFS.
- Unset-lanes compatibility and the config default.
- The strict parser and a floor that is independent of lane selection.
- The forcing block placed before the precip-cache returns.
- Every forcing removal goes through the mutex.
- The 1000-line limit.
