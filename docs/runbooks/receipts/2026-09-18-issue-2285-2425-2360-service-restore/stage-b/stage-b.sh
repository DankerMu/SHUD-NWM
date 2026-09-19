#!/bin/bash
# Stage B (nwm side) — design D8 / tier runbook D7 / current-production-ops §一次性安装 prerequisites.
# Stops at the first failed assertion. Never prints env values.
set -euo pipefail
R=/home/nwm/tmp/svc-restore/stage-b
U=$HOME/.config/systemd/user
N=/home/nwm/NWM
FENCE_ENV=/home/nwm/.local/state/issue1895-maintenance-retirement-95481481/config/node27-timeseries-compression.env
REPO_ENV=$N/infra/env/node27-timeseries-compression.env
RAW_ENV=$N/infra/env/node27-raw-retention.env
EXPECT=8942722d5cca14f828b3b8d58c360614d2452978
mkdir -p "$R/before" "$R/after"
say() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$R/log"; }
die() { say "FAIL: $*"; exit 1; }

# 0. clean checkout, ff to the merge commit
[ -z "$(git -C $N status --porcelain)" ] || die "prod checkout not clean"
git -C $N rev-parse HEAD > "$R/before/head"
git -C $N pull -q --ff-only origin master
[ "$(git -C $N rev-parse HEAD)" = "$EXPECT" ] || die "HEAD $(git -C $N rev-parse HEAD) != $EXPECT"
git -C $N diff --quiet HEAD -- || die "tracked diff after pull"
say "pulled to $EXPECT"

# 1. D7 rebind
for f in nhms-node27-timeseries-compression.service nhms-node27-raw-retention.service; do cp -p "$U/$f" "$R/before/$f"; done
st=$(systemctl --user is-active nhms-node27-timeseries-compression.service || true)
case "$st" in active|activating) die "compression $st";; esac
systemctl --user stop nhms-node27-timeseries-compression.timer
cp -p "$REPO_ENV" "$REPO_ENV.bak-pre-rebind-20260919"
install -m 0600 "$FENCE_ENV" "$REPO_ENV"
sed -i 's#^NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=.*#NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=/home/nwm/NWM#' "$REPO_ENV"
for line in \
  '^NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=/home/nwm/NWM$' \
  '^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=2$' \
  '^NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH=/home/nwm/NWM/.nhms-issue1069-live/scheduled-receipt.json$' \
  '^NODE27_TIMESERIES_COMPRESSION_LOCK_PATH=/home/nwm/NWM/.nhms-issue1069-live/compression.lock$'; do
  c=$(grep -c "$line" "$REPO_ENV" || true); echo "$line $c" >> "$R/after/rebind-asserts"; [ "$c" = 1 ] || die "assert $line = $c"
done
stat -c '%a %U' "$REPO_ENV" > "$R/after/repo-env-mode"
$N/.venv/bin/python -E $N/scripts/node27_timeseries_budget_preflight.py --compression-env "$REPO_ENV" --check > "$R/after/preflight.out" 2>&1 || die "preflight"
install -m 0644 $N/infra/systemd/nhms-node27-timeseries-compression.service "$U/"
systemctl --user daemon-reload
diff "$U/nhms-node27-timeseries-compression.service" $N/infra/systemd/nhms-node27-timeseries-compression.service > "$R/after/diff-compression.txt" || die "compression unit diff"
systemctl --user show -p DropInPaths,WorkingDirectory,ExecStart nhms-node27-timeseries-compression.service > "$R/after/compression-show.txt"
grep -q '^DropInPaths=$' "$R/after/compression-show.txt" || die "compression drop-ins"
git -C $N diff --quiet HEAD -- || die "tracked diff before timer start"
systemctl --user start nhms-node27-timeseries-compression.timer
say "compression rebound"

# 2. nwm raw-retention: LANES + unit with OnFailure
cp -p "$RAW_ENV" "$RAW_ENV.bak-pre-lanes-20260919"
[ "$(grep -c '^NODE27_RAW_RETENTION_LANES=' "$RAW_ENV" || true)" = 0 ] || die "LANES already present"
printf '%s\n' 'NODE27_RAW_RETENTION_LANES=raw,precip-cache' >> "$RAW_ENV"
grep -c '^NODE27_RAW_RETENTION_LANES=raw,precip-cache$' "$RAW_ENV" > "$R/after/lanes-count"
stat -c '%a %U' "$RAW_ENV" > "$R/after/raw-env-mode"
install -m 0644 $N/infra/systemd/nhms-node27-raw-retention.service "$U/"
systemctl --user daemon-reload
diff "$U/nhms-node27-raw-retention.service" $N/infra/systemd/nhms-node27-raw-retention.service > "$R/after/diff-raw.txt" || die "raw unit diff"
systemctl --user show -p OnFailure,DropInPaths nhms-node27-raw-retention.service > "$R/after/raw-show.txt"
grep -q 'OnFailure=nhms-node27-unit-failure-alert@nhms-node27-raw-retention.service.service' "$R/after/raw-show.txt" || die "OnFailure"
for f in nhms-node27-timeseries-retention.service nhms-node27-timeseries-retention.timer nhms-node27-timeseries-compression.timer nhms-node27-raw-retention.timer; do
  diff "$U/$f" "$N/infra/systemd/$f" > /dev/null && echo "$f same" >> "$R/after/unit-diffs" || echo "$f DIFF" >> "$R/after/unit-diffs"
done
git -C $N diff --quiet HEAD -- || die "tracked diff at end"
say "STAGE_B_NWM_OK"
