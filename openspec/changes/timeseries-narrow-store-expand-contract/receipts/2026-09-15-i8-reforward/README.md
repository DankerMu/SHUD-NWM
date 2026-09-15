# I8 post-D12 re-forward (production, 2026-09-15)

Authorizer Danker ("GO，执行生产重新切换"); executor Main. Tool commit `3cdcf5007` (`window_execute.py`
sha256 `d6737c15…`), launched through transient user units by `launch-reforward.py.txt` (sha256 `03738807…`).

- `reprepare` → `REPREPARED` / `reforward`. Provenance is the D12 state `issue2370-window-1b8b2b5b447d-retry-prepare`
  (`state.json` `6bcfddd5…`, routes `d1605088…`); the retained run is 17892.
- `reforward --go Danker`:
  - T0 02:28:31Z; drain at 02:28:32Z (public 502); display validated at 02:29:22Z (public 200); `WINDOW_VALIDATED`
    at 02:29:30Z.
  - Canonical `river_timeseries` = OID 309763188, `river_timeseries_legacy` = OID 24541. Ledger unchanged (60 rows,
    `000059` retained).
  - Parse proof: `fcst_gfs_2026081900_dg_945b…` wrote 512 232 rows. The narrow, retained and legacy reads each
    verified 168 points. The four-route national proof returned 200. Routes: narrow 1533, legacy 6644.
  - Eight authorized timers restored; active tree is `415cbd1e` on `issue1987-reviewed-new-415cbd1e`, clean.
- Governance handoff:
  - `unstage` removed the owned pin, then its audit refused on `AUTOVACUUM_OUTPUT_STALLED`.
  - Cause: database-wide autovacuum/autoanalyze output had been silent since 2026-09-14 05:21Z. That predates the
    window. The launcher was alive, with no xmin holder, prepared transaction or lock wait.
  - With user approval, `pg_terminate_backend` was run once on the autovacuum launcher. The postmaster respawned it;
    within 30 s, `hydro_run` and the new narrow chunks were autoanalyzed.
  - Governance `recover` → `unstaged` PASS; the governance service now runs from `/home/nwm/NWM` and its timer is
    active.
- Not done here: the remaining task 5.2 items (`remaining` in the receipt). #1987 stays open. The historical
  legacy-route reparse is #2382.
