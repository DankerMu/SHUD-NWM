#!/usr/bin/env bash
# `-E` is load-bearing: without it an ERR trap set at top level is NOT inherited
# by function bodies, so `assert_scheduler_unchanged` failing inside a function
# exits 1 without ever running the trap that is supposed to restore state.
# Verified empirically; do not drop it.
set -Eeuo pipefail

repo=${NHMS_SCHEDULER_REFRESH_REPO:-/scratch/frd_muziyao/NWM}
unit_dir=${NHMS_SCHEDULER_REFRESH_UNIT_DIR:-$HOME/.config/systemd/user}
state_root=${NHMS_SCHEDULER_REFRESH_INSTALL_STATE_ROOT:-/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/install-state}
service=nhms-scheduler-file-provider-refresh.service
timer=nhms-scheduler-file-provider-refresh.timer
systemctl_bin=${NHMS_SCHEDULER_REFRESH_SYSTEMCTL:-/usr/bin/systemctl}
python_bin=${NHMS_SCHEDULER_REFRESH_PYTHON:-$repo/.venv/bin/python}
receipt=${NHMS_SCHEDULER_REFRESH_RECEIPT:-/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json}
db_selectors=(
  DATABASE_URL PIPELINE_DATABASE_URL PGAPPNAME PGCHANNELBINDING PGCLIENTENCODING
  PGCONNECT_TIMEOUT PGDATABASE PGDATESTYLE PGGEQO PGGSSDELEGATION PGGSSENCMODE
  PGGSSLIB PGHOST PGHOSTADDR PGKRBSRVNAME PGLOADBALANCEHOSTS PGLOCALEDIR
  PGMAXPROTOCOLVERSION PGMINPROTOCOLVERSION PGOPTIONS PGPASSFILE PGPASSWORD
  PGPORT PGREQUIREAUTH PGREQUIREPEER PGREQUIRESSL PGSERVICE PGSERVICEFILE
  PGSSLCERT PGSSLCERTMODE PGSSLCOMPRESSION PGSSLCRL PGSSLCRLDIR PGSSLKEY
  PGSSLMAXPROTOCOLVERSION PGSSLMINPROTOCOLVERSION PGSSLMODE PGSSLNEGOTIATION
  PGSSLROOTCERT PGSSLSNI PGSSL_CERT_FILE PGSSL_KEY_FILE PGSSL_ROOT_CERT_FILE
  PGSYSCONFDIR PGTARGETSESSIONATTRS PGTZ PGUSER
)

usage() {
  printf 'usage: %s --install|--enable|--rollback\n' "$0" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
action=$1
[[ "$action" == --install || "$action" == --enable || "$action" == --rollback ]] || usage
[[ -d "$repo" && ! -L "$repo" ]]
install -d -m 0700 "$state_root"
[[ ! -L "$state_root" ]]
install -d -m 0700 "$unit_dir"

unit_state() {
  local unit=$1
  local enabled active
  enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
  active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
  printf '%s\t%s\n' "${enabled:-not-found}" "${active:-inactive}"
}

# The compute scheduler's `.service` is a TIMER-DRIVEN ONESHOT: it activates on
# its own cadence (every 5 minutes) and goes inactive again, so its `is-active`
# is not a property this installer can hold still.  Comparing it would abort on
# a unit nobody touched, and an installer that rolls back on that false positive
# turns arming into a retry loop.  `UnitFileState` is what "unchanged" means for
# such a unit; the `.timer` above it is still compared on both fields.
# (Design matrix R15 -- the same split the probe installer applies.)
unit_file_state() {
  local unit=$1
  local enabled
  enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
  printf '%s\n' "${enabled:-not-found}"
}

scheduler_state() {
  printf '%s%s' \
    "$(unit_state nhms-compute-scheduler.timer)" \
    "$(unit_file_state nhms-compute-scheduler.service)"
}

assert_scheduler_unchanged() {
  local before after
  before=$(<"$state_root/scheduler.before")
  after="$(scheduler_state)"
  [[ "$before" == "$after" ]]
}

# Every verb is `|| true`, not just the two that always were.  This function
# runs inside ERR trap bodies, and under `set -Eeuo pipefail` a failing command
# in a trap body TERMINATES the trap: an unguarded `enable` that fails left the
# service unrestored and skipped `assert_scheduler_unchanged` entirely, so the
# installer's own rollback manufactured the `enabled`/`inactive` geometry this
# lane exists to detect with nothing asserting the compute scheduler survived.
# Same guard as `rollback_files`' `daemon-reload`, and the probe installer's
# `restore_probe_timer` already guards all four verbs.
restore_unit_state() {
  local unit=$1
  local state=$2
  local enabled active
  IFS=$'\t' read -r enabled active <<< "$state"
  if [[ "$enabled" == enabled ]]; then
    $systemctl_bin --user enable "$unit" >/dev/null 2>&1 || true
  else
    $systemctl_bin --user disable "$unit" >/dev/null 2>&1 || true
  fi
  if [[ "$active" == active ]]; then
    $systemctl_bin --user start "$unit" >/dev/null 2>&1 || true
  else
    $systemctl_bin --user stop "$unit" >/dev/null 2>&1 || true
  fi
}

restore_refresh_state() {
  local timer_state service_state
  timer_state=$(sed -n '1p' "$state_root/refresh.before")
  service_state=$(sed -n '2p' "$state_root/refresh.before")
  [[ -n "$timer_state" && -n "$service_state" ]]
  restore_unit_state "$timer" "$timer_state"
  restore_unit_state "$service" "$service_state"
}

assert_refresh_service_inactive() {
  local active
  active=$($systemctl_bin --user is-active "$service" 2>/dev/null || true)
  [[ "${active:-inactive}" == inactive ]]
}

restore_invocation_state() {
  restore_unit_state "$timer" "$invocation_timer_state"
  restore_unit_state "$service" "$invocation_service_state"
}

enable_failure_restore() {
  restore_invocation_state
  assert_scheduler_unchanged
}

validate_current_receipt() {
  (
    cd "$repo"
    "$python_bin" -m scripts.scheduler_file_provider_refresh \
      --env-file "$repo/infra/env/compute.scheduler-provider-refresh.env" \
      --validate-current-receipt "$receipt" >/dev/null
  )
}

rollback_files() {
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  $systemctl_bin --user stop "$service" >/dev/null 2>&1 || true
  for unit in "$service" "$timer"; do
    if [[ -f "$state_root/$unit.before" ]]; then
      install -m 0644 "$state_root/$unit.before" "$unit_dir/$unit"
    else
      rm -f "$unit_dir/$unit"
    fi
  done
  # `|| true`: this runs inside the `--install` ERR trap body, and a failing
  # daemon-reload must not abort the trap before it reaches
  # `restore_refresh_state` and `assert_scheduler_unchanged` -- a partial
  # rollback is worse than none.  Same guard as the probe installer's
  # `remove_probe_units`.
  $systemctl_bin --user daemon-reload || true
}

assert_refresh_service_inactive

# The protected-state baseline is captured PER INVOCATION, before this action
# touches anything, so `assert_scheduler_unchanged` always means "this
# invocation changed nothing".  It used to be written by `--install` only and
# read by `--enable` and `--rollback`, which the runbook documents as
# standalone commands -- so an on-disk file became a contract between two
# versions of this script, and this change's own reshaping of it (4 fields ->
# 3) would make the next bare `--enable` abort on pure format drift with no
# unit having moved.  Nothing ever restores FROM this file; `refresh.before`
# below is the restore data and keeps its own existence precondition.
scheduler_state > "$state_root/scheduler.before"

if [[ "$action" == --install ]]; then
  env_file="$repo/infra/env/compute.scheduler-provider-refresh.env"
  env_mode=$(stat -c '%a' "$env_file" 2>/dev/null || stat -f '%Lp' "$env_file")
  [[ -f "$env_file" && ! -L "$env_file" && "$env_mode" == 600 ]]
  if grep -Eq "^[[:space:]]*($(IFS='|'; printf '%s' "${db_selectors[*]}"))=" "$env_file"; then
    exit 2
  fi
  [[ $(grep -Ec '^NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true$' "$env_file") -eq 1 ]]
  {
    unit_state "$timer"
    unit_state "$service"
  } > "$state_root/refresh.before"
  for unit in "$service" "$timer"; do
    if [[ -f "$unit_dir/$unit" && ! -L "$unit_dir/$unit" ]]; then
      install -m 0600 "$unit_dir/$unit" "$state_root/$unit.before"
    else
      rm -f "$state_root/$unit.before"
    fi
  done
  trap 'rollback_files; restore_refresh_state; assert_scheduler_unchanged' ERR
  for unit in "$service" "$timer"; do
    [[ -f "$repo/infra/systemd/$unit" && ! -L "$repo/infra/systemd/$unit" ]]
    install -m 0644 "$repo/infra/systemd/$unit" "$unit_dir/$unit"
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  $systemctl_bin --user daemon-reload
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  $systemctl_bin --user stop "$service" >/dev/null 2>&1 || true
  assert_scheduler_unchanged
  trap - ERR
  printf '{"status":"installed_stopped","scheduler_unchanged":true}\n'
elif [[ "$action" == --enable ]]; then
  for unit in "$service" "$timer"; do
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  validate_current_receipt
  invocation_timer_state=$(unit_state "$timer")
  invocation_service_state=$(unit_state "$service")
  trap enable_failure_restore ERR
  $systemctl_bin --user enable --now "$timer"
  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]
  [[ "$($systemctl_bin --user is-active "$service" 2>/dev/null || true)" == inactive ]]
  assert_scheduler_unchanged
  trap - ERR
  printf '{"status":"enabled_active","scheduler_unchanged":true}\n'
else
  [[ -f "$state_root/refresh.before" ]]
  rollback_files
  restore_refresh_state
  assert_scheduler_unchanged
  printf '{"status":"rolled_back","scheduler_unchanged":true}\n'
fi
