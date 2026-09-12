#!/usr/bin/env bash
# Install / arm / roll back the node-22 refresh-timer health probe units.
#
# The probe is a watchdog, so the one thing this installer must never do is
# disturb what it watches.  Every action captures the enabled/active state of
# four protected units before it touches anything and asserts them byte-equal
# afterwards: nhms-compute-scheduler.{timer,service} and both
# nhms-scheduler-file-provider-refresh.{timer,service}.  Only the probe's own
# units are ever mutated.
set -euo pipefail

repo=${NHMS_REFRESH_HEALTH_REPO:-/scratch/frd_muziyao/NWM}
unit_dir=${NHMS_REFRESH_HEALTH_UNIT_DIR:-$HOME/.config/systemd/user}
state_root=${NHMS_REFRESH_HEALTH_INSTALL_STATE_ROOT:-/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/install-state}
receipt_root=${NHMS_REFRESH_HEALTH_RECEIPT_ROOT:-/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts}
systemctl_bin=${NHMS_REFRESH_HEALTH_SYSTEMCTL:-/usr/bin/systemctl}
service=nhms-node22-refresh-timer-health.service
timer=nhms-node22-refresh-timer-health.timer
protected_units=(
  nhms-compute-scheduler.timer
  nhms-compute-scheduler.service
  nhms-scheduler-file-provider-refresh.timer
  nhms-scheduler-file-provider-refresh.service
)

usage() {
  printf 'usage: %s --install|--enable|--rollback\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
action=$1
[[ "$action" == --install || "$action" == --enable || "$action" == --rollback ]] || usage
[[ -d "$repo" && ! -L "$repo" ]]
# GNU `install -d -m` applies the mode to the FINAL component only, so name
# every level that must be private -- the shared parent included.  D2 wants the
# receipt root itself 0700, and a 0755 directory above it leaks the run list.
install -d -m 0700 "$(dirname "$state_root")" "$state_root"
[[ ! -L "$state_root" ]]
install -d -m 0700 "$(dirname "$receipt_root")" "$receipt_root"
[[ ! -L "$receipt_root" ]]
install -d -m 0700 "$unit_dir"

# `is-enabled` / `is-active` are read-only queries; both return non-zero for
# states this script must still record, hence the `|| true`.
protected_state() {
  local unit enabled active
  for unit in "${protected_units[@]}"; do
    enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
    active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
    printf '%s\t%s\t%s\n' "$unit" "${enabled:-not-found}" "${active:-inactive}"
  done
}

assert_protected_unchanged() {
  local after
  after=$(protected_state)
  [[ "$after" == "$(<"$state_root/protected.before")" ]]
}

remove_probe_units() {
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  local unit
  for unit in "$service" "$timer"; do
    if [[ -f "$state_root/$unit.before" ]]; then
      install -m 0644 "$state_root/$unit.before" "$unit_dir/$unit"
    else
      rm -f "$unit_dir/$unit"
    fi
  done
  $systemctl_bin --user daemon-reload
}

protected_state > "$state_root/protected.before"

if [[ "$action" == --install ]]; then
  for unit in "$service" "$timer"; do
    if [[ -f "$unit_dir/$unit" && ! -L "$unit_dir/$unit" ]]; then
      install -m 0600 "$unit_dir/$unit" "$state_root/$unit.before"
    else
      rm -f "$state_root/$unit.before"
    fi
  done
  trap 'remove_probe_units; assert_protected_unchanged' ERR
  for unit in "$service" "$timer"; do
    [[ -f "$repo/infra/systemd/$unit" && ! -L "$repo/infra/systemd/$unit" ]]
    install -m 0644 "$repo/infra/systemd/$unit" "$unit_dir/$unit"
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  $systemctl_bin --user daemon-reload
  # Installed but inert: arming is the separate, explicit `--enable`.
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  assert_protected_unchanged
  trap - ERR
  printf '{"status":"installed_stopped","protected_unchanged":true}\n'
elif [[ "$action" == --enable ]]; then
  for unit in "$service" "$timer"; do
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  $systemctl_bin --user enable --now "$timer"
  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]
  assert_protected_unchanged
  printf '{"status":"enabled_active","protected_unchanged":true}\n'
else
  remove_probe_units
  assert_protected_unchanged
  printf '{"status":"rolled_back","protected_unchanged":true}\n'
fi
