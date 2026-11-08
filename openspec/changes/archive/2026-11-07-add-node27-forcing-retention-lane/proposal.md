## Why

node-27 exports `/home/ghdc` over NFS, and node-22 mounts it as `/ghdc/data`. Every node-22
forcing package copied back to the shared object-store therefore lands on node-27's `/home` LV.
Nothing ever retires `object-store/forcing/`:

- node-22 retention sweeps only `runs/` on the copyback root (`services/orchestrator/retention.py:21-24`).
- node-27 raw-retention covers only `raw`, `canonical` and `precip-cache` (`scripts/node27_raw_retention.py:5-9`).

On 2026-11-07, `forcing/` held 353G (gfs+ifs, 132 cycles each, from 2026-08-09T12) and grew by about
4.5G per cycle.

Owner decision (2026-11-07, #3087): `forcing/` and `runs/` keep 30 days. The pre-2026-10-08 backlog
was removed by hand; this change makes the forcing rule permanent. `runs/` already has an owner,
node-22 extra-root retention (`NHMS_RETENTION_EXTRA_ROOTS_DAYS=30`), and is not changed here.

## What Changes

- `scripts/node27_raw_retention.py` gains an opt-in `forcing` lane.
  - **Targets:** `<object-store-root>/forcing/<segment>/<YYYYMMDDHH>`, where `<segment>` is the
    configured source's lower-case object-store segment (`gfs`, `ifs`). This is the spelling the
    forcing producer writes; for IFS this differs from the canonical storage id `IFS`.
  - **Cutoff:** its own, `display_watermark - NODE27_RAW_RETENTION_FORCING_DAYS` (default 30).
  - **Floor:** fail-closed. A value below `FORCING_RETENTION_FLOOR_DAYS =
    max(NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1, 14)` blocks the run at preflight. The 14 is the
    production `AUTOPIPE_MAX_CYCLE_AGE_DAYS` from #3062: ingest reads the forcing handoff, so forcing
    must outlive the autopipe cycle floor.
  - **Locking:** every forcing tree is removed while holding the copyback batch mutex, exactly like
    canonical, because `forcing/` is a copyback promote target.
- **Lane selection:** `forcing` joins the accepted `NODE27_RAW_RETENTION_LANES` vocabulary, but an
  unset variable still selects only the legacy three lanes (`raw`, `canonical`, `precip-cache`), byte
  for byte as before. Forcing is pruned only where an operator names it.
- **Deployment:** `scripts/node27_canonical_retention_install.sh` renders
  `NODE27_RAW_RETENTION_LANES=canonical,forcing` for the system unit, which runs as `frd_muziyao`, the
  owner of `forcing/`. Measured on node-27 on 2026-11-07:
  - `forcing/` and `forcing/{gfs,ifs}` are `frd_muziyao:wwwroot 775`;
  - cycle directories are `frd_muziyao:nfsdata 755`;
  - `find forcing -mindepth 1 ! -user frd_muziyao` returns 0 entries at every depth (12006 directories);
  - `nwm` belongs to neither group, so the nwm unit cannot delete anything there.

  The nwm user unit stays `raw,precip-cache` and records `forcing` as `lane_not_selected`.
- **Summary:** additive `forcing_root`, `forcing_retention_days` and `forcing_cutoff` fields; the
  existing fields are unchanged.
- **Docs:** `infra/env/node27-raw-retention.example`; the `retention.py` docstring that calls
  copyback `forcing/` "never touched" now points at the node-27 lane as the owner; the runbook
  section on the canonical unit.

## Triage

```text
Issue type: feature
Fixture level: expanded
Upstream suggested level: absent
Blast radius: wrong cutoff/segment deletes live display forcing (station-series 404) or forcing still needed by ingest; lock bypass races node-22 copyback promotes
Selected risk packs: File IO / path safety; Concurrency / shared state; Config; Auth / permissions; Schema (summary fields); Resource limits (lock hold); Legacy compatibility; Error handling / partial outputs; Documentation
Evidence floor: uv run ruff check . ; uv run pytest -q tests/test_node27_raw_retention*.py tests/test_copyback_guard.py tests/test_retention_extra_roots.py ; node-27 canonical system unit receipt
```

## Impact

- Code: `scripts/node27_raw_retention.py` (957 lines; must stay under 1000, otherwise split by
  responsibility), `scripts/node27_canonical_retention_install.sh`.
- Tests: `tests/test_node27_raw_retention.py`, `tests/test_node27_raw_retention_copyback_mutex.py`,
  `tests/test_node27_raw_retention_canonical_deployment.py`.
- Docs/config: `infra/env/node27-raw-retention.example`, `services/orchestrator/retention.py`
  (docstring only), `docs/runbooks/production-ops/artifacts-and-storage.md`.
- Spec: `node27-raw-retention`.

## Deviations from the issue

- The issue says "现有 raw / canonical / precip-cache 行为字节不变（既有测试全绿）". Behaviour is unchanged, but five
  existing assertions pin exact key sets or skip lists. They gain exactly the three summary fields and/or the
  `forcing` `lane_not_selected` entry (design "Must preserve" lists them). Two more assertions in
  `tests/test_node27_raw_retention_canonical_deployment.py` (`:459`, `:471`) hard-code the installer's
  `LANES=canonical` line and change to `canonical,forcing` with D7.
- The lane vocabulary and env parsing moved to `scripts/node27_raw_retention_lanes.py` (D8, 1000-line guard), which
  added a `scripts/select_ci_tests_rules.py` route. Two internal names were made lane-neutral
  (`_remove_under_copyback_mutex`, `_CopybackLockBudget`); they have no external references.
- The issue's node-22 `runs/` receipt (`DRY_RUN=false`) cannot be produced until the 2026-11 catch-up ends and
  `NHMS_RETENTION_DRY_RUN` is restored. It is tracked as an open post-merge task (tasks 4.5), not a merge gate.
