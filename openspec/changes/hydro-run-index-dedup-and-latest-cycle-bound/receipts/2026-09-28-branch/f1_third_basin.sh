#!/bin/bash
# F1 re-measure on the basins with the most forecast runs (heihe, qhh: 541 each, tie).
# Read-only (nhms_display_ro, BEGIN READ ONLY). Each variant runs 3 times; the 3rd (warm) is kept.
# Candidates are a multi-row VALUES list on purpose: with a single row the planner folds the
# window into constants and excludes chunks at plan time (narrow_pkey everywhere, 36 hit), which
# the real statement (candidates from the cand CTE) never gets. GFS and IFS 00Z/12Z windows are
# both [cycle, cycle+7d] on node-27, so each variant carries one GFS-like and one IFS-like row.
set -uo pipefail
cd /home/nwm/tmp/batch-db
RO="$(cat ro.dsn)"
run() { # basin tag cands win_lo win_hi
  local b=$1 tag=$2 cands=$3 lo=$4 hi=$5 out=f1-$1-$2.out
  for i in 1 2 3; do
    { echo "# f1-probe-window.sql basin=basins_$b tag=$tag run=$i/3 (kept: 3, warm) at $(date -u +%FT%TZ)"
      echo "# segid=basins_${b}_shud_shud_riv_000001 net=basins_${b}_rivnet_vbasins bv=basins_${b}_vbasins"
      echo "# cands=$cands"
      psql "$RO" -X -v ON_ERROR_STOP=1 -v bv=basins_${b}_vbasins -v segid=basins_${b}_shud_shud_riv_000001 \
        -v net=basins_${b}_rivnet_vbasins -v "cands=$cands" -v win_lo="$lo" -v win_hi="$hi" -f f1-probe-window.sql
    } > "$out" 2>&1
  done
}
C00="(-1, timestamptz '2026-09-28 00:00+00', timestamptz '2026-10-05 00:00+00')"
C12="(-2, timestamptz '2026-09-28 12:00+00', timestamptz '2026-10-05 12:00+00')"
C00I="(-3, timestamptz '2026-09-28 00:00+00', timestamptz '2026-10-05 00:00+00')"
C12I="(-4, timestamptz '2026-09-28 12:00+00', timestamptz '2026-10-05 12:00+00')"
for b in heihe qhh; do
  run $b gfs-ifs-00z "$C00, $C00I" "2026-09-28 00:00+00" "2026-10-05 00:00+00"
  run $b gfs-ifs-12z "$C12, $C12I" "2026-09-28 12:00+00" "2026-10-05 12:00+00"
  run $b gfs-ifs-00z12z "$C12, $C12I, $C00, $C00I" "2026-09-28 00:00+00" "2026-10-05 12:00+00"
done
# Controls (heihe only), run after the loop above with the same 3-run/keep-3rd pattern:
#  - master shape: f1-probe-window-master.sql (f1-probe-window.sql with the LATERAL ... LIMIT 1
#    turned back into origin/master's unbounded WHERE EXISTS) ->
#    f1-heihe-master-gfs-ifs-00z.out ("$C00, $C00I"), f1-heihe-master-gfs-ifs-00z12z.out ("$C12, $C12I, $C00, $C00I")
#  - one VALUES row: f1-probe-window.sql with "$C00" -> f1-heihe-single-00z.out (plan-time
#    exclusion; not the real statement's shape)
