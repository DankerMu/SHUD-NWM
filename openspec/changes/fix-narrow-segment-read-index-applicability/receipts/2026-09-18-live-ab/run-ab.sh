#!/bin/bash
# #2451 tasks.md section 4.1 : same-session A/B, READ ONLY, live node-27 DB.
# DSN only via env, never argv. No ANALYZE, no compress_chunk, no writes.
set -uo pipefail
export TMPDIR=/home/nwm/tmp
OUT=/home/nwm/tmp/2451/live-ab
mkdir -p "$OUT"

set -a
. /home/nwm/NWM/infra/env/display.env
set +a
case "${DATABASE_URL:-}" in
  postgresql://nhms_display_ro:*@*) : ;;
  *) echo "DSN_ROLE_UNEXPECTED_want_nhms_display_ro"; exit 2 ;;
esac

PY=/home/nwm/NWM/.venv/bin/python
R=/home/nwm/NWM/openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate

for arm in base:/home/nwm/tmp/2451-base head:/home/nwm/tmp/2451-wt; do
  name="${arm%%:*}"; tree="${arm#*:}"
  for probe in probe1987 probe1987latest; do
    echo "=== $name / $probe  ($(git -C "$tree" rev-parse --short HEAD)) ==="
    "$PY" -B "$R/$probe.py" "$tree" "$OUT/$probe-$name.json" 2>&1 | tail -20
    echo "rc=$?"
  done
done
echo "=== ALL DONE ==="
ls -l "$OUT"
