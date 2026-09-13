#!/usr/bin/env bash
# Install / arm / roll back the node-22 refresh-timer health probe units.
#
# The probe is a watchdog, so the one thing this installer must never do is
# disturb what it watches.  Every action captures the state of four protected
# units before it touches anything and asserts them byte-equal afterwards:
# nhms-compute-scheduler.{timer,service} and both
# nhms-scheduler-file-provider-refresh.{timer,service}.  Only the probe's own
# units are ever mutated.
#
# The comparison is per unit TYPE (design matrix R15).  For the two TIMERS both
# UnitFileState and is-active are compared.  For the two timer-driven ONESHOT
# services only UnitFileState is: a oneshot's is-active legitimately flips on
# its own cadence -- the compute scheduler every 5 minutes, the refresh service
# inside its 02:15-04:15Z window -- so comparing it would abort on a unit nobody
# touched, and an installer that rolls back on that false positive turns arming
# into a retry loop.  UnitFileState is what "unchanged" means for a unit whose
# activity is driven by its timer.
#
# `-E` is load-bearing, not decoration: without it an ERR trap set here is NOT
# inherited by function bodies, so `assert_protected_unchanged` failing inside a
# function exits 1 without ever running the trap that is supposed to back the
# install out.  `test_r15_without_errtrace_the_install_trap_never_runs` runs a
# copy of this file with `-E` dropped and shows the units left behind.
set -Eeuo pipefail

repo=${NHMS_REFRESH_HEALTH_REPO:-/scratch/frd_muziyao/NWM}
unit_dir=${NHMS_REFRESH_HEALTH_UNIT_DIR:-$HOME/.config/systemd/user}
state_root=${NHMS_REFRESH_HEALTH_INSTALL_STATE_ROOT:-/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/install-state}
receipt_root=${NHMS_REFRESH_HEALTH_RECEIPT_ROOT:-/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts}
systemctl_bin=${NHMS_REFRESH_HEALTH_SYSTEMCTL:-/usr/bin/systemctl}
service=nhms-node22-refresh-timer-health.service
timer=nhms-node22-refresh-timer-health.timer
# Compared on BOTH UnitFileState and is-active.
protected_timers=(
  nhms-compute-scheduler.timer
  nhms-scheduler-file-provider-refresh.timer
)
# Timer-driven oneshots: compared on UnitFileState only (see the header).
protected_oneshots=(
  nhms-compute-scheduler.service
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
  for unit in "${protected_timers[@]}"; do
    enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
    active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
    printf '%s\t%s\t%s\n' "$unit" "${enabled:-not-found}" "${active:-inactive}"
  done
  for unit in "${protected_oneshots[@]}"; do
    enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
    printf '%s\t%s\n' "$unit" "${enabled:-not-found}"
  done
}

assert_protected_unchanged() {
  local after
  after=$(protected_state)
  [[ "$after" == "$(<"$state_root/protected.before")" ]]
}

probe_timer_state() {
  local enabled active
  enabled=$($systemctl_bin --user is-enabled "$timer" 2>/dev/null || true)
  active=$($systemctl_bin --user is-active "$timer" 2>/dev/null || true)
  printf '%s\t%s\n' "${enabled:-not-found}" "${active:-inactive}"
}

# Restore the probe timer to the state it was in when THIS invocation started,
# rather than blanket-disarming it: a re-run of `--enable` that trips on a
# divergent protected read must not disarm a probe that was already armed.
restore_probe_timer() {
  local enabled active
  IFS=$'\t' read -r enabled active <<< "$invocation_timer_state"
  if [[ "$enabled" == enabled ]]; then
    $systemctl_bin --user enable "$timer" >/dev/null 2>&1 || true
  else
    $systemctl_bin --user disable "$timer" >/dev/null 2>&1 || true
  fi
  if [[ "$active" == active ]]; then
    $systemctl_bin --user start "$timer" >/dev/null 2>&1 || true
  else
    $systemctl_bin --user stop "$timer" >/dev/null 2>&1 || true
  fi
}

enable_failure_restore() {
  restore_probe_timer
  assert_protected_unchanged
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
  # `|| true`: this runs inside an ERR trap body, and a failing daemon-reload
  # must not abort the trap before it reaches its own assertion.
  $systemctl_bin --user daemon-reload || true
}

# `remove_probe_units` swallows every systemctl failure on purpose -- it is also
# the `--install` ERR trap body, which must not be cut short -- and `--install`
# swallows its own `disable --now` failure too, so on both `--install` and
# `--rollback` success is decided by READING THE UNITS BACK, never by how the
# calls went.  A refused `disable --now` leaves the timer loaded, enabled and
# active; without this check either action would still print its status line.
#
# "Gone" means neither probe unit can fire again, for both the timer and the
# service:
#   is-enabled  disabled | static | not-found | (empty)
#     `disabled`: the file is still present (restored from `$unit.before`, or
#     the removal raced) but nothing pulls it in.  `static`: a unit with no
#     [Install] section -- the probe SERVICE, whenever it is loaded -- cannot
#     be enabled at all.  Empty: the unit file is gone -- depending on the
#     systemd release, `is-enabled` on a missing unit may put nothing on
#     stdout, only a stderr message.  `not-found` is the placeholder this
#     installer itself records for that case (`protected_state`), accepted for
#     symmetry.  Empty is accepted only because the `is-active` read below
#     refuses an empty answer, which is what an unreachable user manager
#     produces.
#   is-active   inactive | failed
#     Not running.  `failed` is accepted because the probe service exits
#     non-zero on every unhealthy verdict by design (D2), and a failed unit
#     stays `failed` until `reset-failed` even after its file is removed;
#     refusing it would make rollback impossible after any real alert.
# Anything else -- `enabled`, `enabled-runtime`, `linked`, `masked`, `active`,
# `activating`, `reloading`, an empty `is-active` -- is refused: the run exits
# non-zero without printing a status line.  A probe tick still running at that
# moment is therefore refused too; re-run once it finishes.
assert_probe_units_gone() {
  local unit enabled active
  for unit in "$timer" "$service"; do
    enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
    active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
    case "$enabled" in
      disabled | static | not-found | '') ;;
      *)
        printf 'probe read-back: %s is still %s\n' "$unit" "$enabled" >&2
        return 1
        ;;
    esac
    case "$active" in
      inactive | failed) ;;
      *)
        printf 'probe read-back: %s is-active=%s, not stopped\n' "$unit" "${active:-<no answer>}" >&2
        return 1
        ;;
    esac
  done
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
  # Still inside the ERR trap: a refused read-back runs `remove_probe_units`
  # (the same swallowed `disable --now`, then the pre-install unit files back)
  # and the protected assertion, and exits non-zero with no status line.
  assert_probe_units_gone
  assert_protected_unchanged
  trap - ERR
  printf '{"status":"installed_stopped","protected_unchanged":true}\n'
elif [[ "$action" == --enable ]]; then
  for unit in "$service" "$timer"; do
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  # Armed BEFORE the mutation, so a failure of either post-arming assertion
  # backs the arming out instead of leaving a half-armed probe behind an
  # exit code of 1.
  invocation_timer_state=$(probe_timer_state)
  trap enable_failure_restore ERR
  $systemctl_bin --user enable --now "$timer"
  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]
  assert_protected_unchanged
  trap - ERR
  printf '{"status":"enabled_active","protected_unchanged":true}\n'
else
  remove_probe_units
  assert_probe_units_gone
  assert_protected_unchanged
  printf '{"status":"rolled_back","protected_unchanged":true}\n'
fi
