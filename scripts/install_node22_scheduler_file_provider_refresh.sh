#!/usr/bin/env bash
set -euo pipefail

repo=/scratch/frd_muziyao/NWM
unit_dir="$HOME/.config/systemd/user"
state_root=/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/install-state
service=nhms-scheduler-file-provider-refresh.service
timer=nhms-scheduler-file-provider-refresh.timer
systemctl_bin=/usr/bin/systemctl

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

assert_scheduler_unchanged() {
  local before after
  before=$(<"$state_root/scheduler.before")
  after="$(unit_state nhms-compute-scheduler.timer)$(unit_state nhms-compute-scheduler.service)"
  [[ "$before" == "$after" ]]
}

restore_unit_state() {
  local unit=$1
  local state=$2
  local enabled active
  IFS=$'\t' read -r enabled active <<< "$state"
  if [[ "$enabled" == enabled ]]; then
    $systemctl_bin --user enable "$unit"
  else
    $systemctl_bin --user disable "$unit" >/dev/null 2>&1 || true
  fi
  if [[ "$active" == active ]]; then
    $systemctl_bin --user start "$unit"
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
  $systemctl_bin --user daemon-reload
}

if [[ "$action" == --install ]]; then
  env_file="$repo/infra/env/compute.scheduler-provider-refresh.env"
  [[ -f "$env_file" && ! -L "$env_file" && "$(stat -c '%a' "$env_file")" == 600 ]]
  if grep -Eq '^[[:space:]]*(DATABASE_URL|PIPELINE_DATABASE_URL|PGHOST|PGPORT|PGDATABASE|PGUSER|PGSERVICE|PGSERVICEFILE)=' "$env_file"; then
    exit 2
  fi
  printf '%s%s' \
    "$(unit_state nhms-compute-scheduler.timer)" \
    "$(unit_state nhms-compute-scheduler.service)" > "$state_root/scheduler.before"
  {
    unit_state "$timer"
    unit_state "$service"
  } > "$state_root/refresh.before"
  refresh_service_before=$(sed -n '2p' "$state_root/refresh.before")
  [[ "$refresh_service_before" != *$'\tactive' ]]
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
  trap 'rollback_files; restore_refresh_state; assert_scheduler_unchanged' ERR
  for unit in "$service" "$timer"; do
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  receipt=/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json
  "$repo/.venv/bin/python" -c \
    'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); assert p["outcome"] == "published" and p["database_free"] is True' \
    "$receipt"
  $systemctl_bin --user enable --now "$timer"
  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]
  [[ "$($systemctl_bin --user is-active "$service" 2>/dev/null || true)" == inactive ]]
  assert_scheduler_unchanged
  trap - ERR
  printf '{"status":"enabled_active","scheduler_unchanged":true}\n'
else
  [[ -f "$state_root/scheduler.before" && -f "$state_root/refresh.before" ]]
  rollback_files
  restore_refresh_state
  assert_scheduler_unchanged
  printf '{"status":"rolled_back","scheduler_unchanged":true}\n'
fi
