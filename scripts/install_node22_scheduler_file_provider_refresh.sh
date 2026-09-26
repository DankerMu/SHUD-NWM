#!/usr/bin/env bash
# Install / arm / roll back the node-22 file-provider refresh lane
# (nhms-scheduler-file-provider-refresh.{timer,service}).
#
# Failure-path contract (#2294):
#
# * `-E` is load-bearing: without it an ERR trap is not inherited by function
#   bodies, so an assertion failing inside a function exits 1 without running
#   the restore it is supposed to trigger.
# * Every failure after the first mutation runs ONE restore handler, in the
#   main shell only (`$BASHPID` guard; bash >= 4).  The handler clears the ERR
#   trap, attempts every restore step whatever the earlier ones did (each step
#   records its own failure in `failed_steps`), always finishes with both
#   read-backs, and exits 1 without a status line.
# * Success is decided by reading the units back, never by how the systemctl
#   calls went: the refresh units against their restore target, the compute
#   scheduler against the state captured at the start of THIS invocation (per
#   unit type: the timer on UnitFileState and is-active, the timer-driven
#   oneshot on UnitFileState only).  No file written by another invocation is
#   ever compared; a legacy `scheduler.before` is neither read nor written.
# * `--install` refuses while the refresh timer or service is armed, and never
#   rewrites a recorded restore baseline (`refresh.before` plus the unit
#   `.before` files); it parses an existing `refresh.before` before its first
#   mutation and refuses a malformed one.  `refresh.before` is written last,
#   through a temp file and `mv`, so its presence marks a complete baseline.
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
# Compared on BOTH UnitFileState and is-active.
protected_timers=(nhms-compute-scheduler.timer)
# Timer-driven oneshot: compared on UnitFileState only -- its is-active flips
# every 5 minutes on its own timer, whoever runs this installer.
protected_oneshots=(nhms-compute-scheduler.service)

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

failed_steps=()
baseline_timer_state=
baseline_service_state=
parsed_enabled=
parsed_active=

note_failure() {
  failed_steps+=("$1")
  printf 'restore step failed: %s\n' "$1" >&2
}

report_failure() {
  if [[ ${#failed_steps[@]} -eq 0 ]]; then
    printf '%s failed; every restore step and read-back passed\n' "$1" >&2
  else
    printf '%s failed; %d restore step(s) or read-back(s) failed\n' "$1" "${#failed_steps[@]}" >&2
  fi
}

unit_state() {
  local unit=$1
  local enabled active
  enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
  active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
  printf '%s\t%s\n' "${enabled:-not-found}" "${active:-inactive}"
}

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

# Fails as a bare test, so under the trap it is `-E` that makes the ERR trap
# see it (a `return 1` would be seen at the call site with or without `-E`).
assert_scheduler_unchanged() {
  local after
  after=$(protected_state)
  if [[ "$after" != "$scheduler_before" ]]; then
    printf 'scheduler read-back: compute-scheduler units changed during this run\nbefore:\n%s\nafter:\n%s\n' \
      "$scheduler_before" "$after" >&2
  fi
  [[ "$after" == "$scheduler_before" ]]
}

# A recorded unit state is exactly `<UnitFileState><TAB><ActiveState>`: two
# non-empty fields, one tab, no newline.  Anything else fails loudly.
parse_unit_state() {
  local state=$1 tabs
  parsed_enabled=
  parsed_active=
  if [[ "$state" == *$'\n'* ]]; then
    printf 'malformed unit state %q: embedded newline\n' "$state" >&2
    return 1
  fi
  tabs=${state//[!$'\t']/}
  if [[ ${#tabs} -ne 1 ]]; then
    printf 'malformed unit state %q: expected 2 tab-separated fields, found %d\n' "$state" "$((${#tabs} + 1))" >&2
    return 1
  fi
  parsed_enabled=${state%%$'\t'*}
  parsed_active=${state#*$'\t'}
  if [[ -z "$parsed_enabled" || -z "$parsed_active" ]]; then
    printf 'malformed unit state %q: empty field\n' "$state" >&2
    return 1
  fi
}

# `refresh.before` is exactly two lines: the timer's state, then the service's.
read_refresh_baseline() {
  local -a lines=()
  baseline_timer_state=
  baseline_service_state=
  if [[ ! -f "$state_root/refresh.before" || -L "$state_root/refresh.before" ]]; then
    printf 'no recorded refresh baseline at %s\n' "$state_root/refresh.before" >&2
    return 1
  fi
  mapfile -t lines < "$state_root/refresh.before" || return 1
  if [[ ${#lines[@]} -ne 2 ]]; then
    printf '%s: expected exactly 2 lines (timer, service), found %d\n' \
      "$state_root/refresh.before" "${#lines[@]}" >&2
    return 1
  fi
  parse_unit_state "${lines[0]}" || return 1
  parse_unit_state "${lines[1]}" || return 1
  baseline_timer_state=${lines[0]}
  baseline_service_state=${lines[1]}
}

# D2: read the refresh units back against a restore target.  The timer on
# both fields; the service, a oneshot its timer drives, on UnitFileState only.
assert_refresh_state_restored() {
  local want_timer=$1 want_service=$2 got_timer got_service want_enabled want_active
  got_timer=$(unit_state "$timer")
  got_service=$(unit_state "$service")
  if ! parse_unit_state "$want_timer"; then
    printf 'refresh read-back: no valid target for %s\n' "$timer" >&2
    return 1
  fi
  want_enabled=$parsed_enabled
  want_active=$parsed_active
  if [[ "$got_timer" != "$want_enabled"$'\t'"$want_active" ]]; then
    printf 'refresh read-back: %s is %s, expected %s/%s\n' \
      "$timer" "${got_timer/$'\t'//}" "$want_enabled" "$want_active" >&2
    return 1
  fi
  if ! parse_unit_state "$want_service"; then
    printf 'refresh read-back: no valid target for %s\n' "$service" >&2
    return 1
  fi
  if [[ "${got_service%%$'\t'*}" != "$parsed_enabled" ]]; then
    printf 'refresh read-back: %s UnitFileState is %s, expected %s\n' \
      "$service" "${got_service%%$'\t'*}" "$parsed_enabled" >&2
    return 1
  fi
}

# "Disarmed" = neither refresh unit can fire: is-enabled in {disabled, static,
# not-found, ''} and is-active in {inactive, failed}.  Read raw: an empty
# is-active is an unreachable user manager, which is NOT disarmed.
refresh_units_disarmed() {
  local unit enabled active
  for unit in "$timer" "$service"; do
    enabled=$($systemctl_bin --user is-enabled "$unit" 2>/dev/null || true)
    active=$($systemctl_bin --user is-active "$unit" 2>/dev/null || true)
    armed_unit=$unit
    armed_enabled=${enabled:-<no answer>}
    armed_active=${active:-<no answer>}
    case "$enabled" in
      disabled | static | not-found | '') ;;
      *) return 1 ;;
    esac
    case "$active" in
      inactive | failed) ;;
      *) return 1 ;;
    esac
  done
}

assert_refresh_units_disarmed() {
  if refresh_units_disarmed; then
    return 0
  fi
  printf 'install read-back: %s is %s/%s, not disarmed\n' "$armed_unit" "$armed_enabled" "$armed_active" >&2
  return 1
}

# Every step is guarded and records its own failure.  `disable`/`stop` stay
# swallowed: a real user manager exits non-zero for them on a unit whose file
# is gone or not loaded -- exactly a first-install `not-found` baseline -- so
# the read-back, not their exit status, decides whether the target was reached.
restore_unit_state() {
  local unit=$1
  if ! parse_unit_state "$2"; then
    note_failure "restore $unit: unparseable target state"
    return 0
  fi
  if [[ "$parsed_enabled" == enabled ]]; then
    $systemctl_bin --user enable "$unit" || note_failure "enable $unit"
  else
    $systemctl_bin --user disable "$unit" >/dev/null 2>&1 || true
  fi
  if [[ "$parsed_active" == active ]]; then
    $systemctl_bin --user start "$unit" || note_failure "start $unit"
  else
    $systemctl_bin --user stop "$unit" >/dev/null 2>&1 || true
  fi
}

# The ONE restore of `--rollback` and of a failed `--install`: disarm, put the
# baseline unit files back, reload, then restore the baseline states.  Called
# as a plain command so every step runs with errexit live; each step carries
# its own guard.
restore_refresh_baseline() {
  local unit
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  $systemctl_bin --user stop "$service" >/dev/null 2>&1 || true
  for unit in "$service" "$timer"; do
    if [[ -f "$state_root/$unit.before" ]]; then
      install -m 0644 "$state_root/$unit.before" "$unit_dir/$unit" || note_failure "restore unit file $unit"
    else
      rm -f "$unit_dir/$unit" || note_failure "remove unit file $unit"
    fi
  done
  $systemctl_bin --user daemon-reload || note_failure "daemon-reload"
  if read_refresh_baseline; then
    restore_unit_state "$timer" "$baseline_timer_state"
    restore_unit_state "$service" "$baseline_service_state"
  else
    note_failure "read $state_root/refresh.before"
  fi
}

install_failure_restore() {
  [[ $BASHPID == "$$" ]] || exit 1
  trap - ERR
  printf 'install failed; restoring the recorded refresh baseline\n' >&2
  restore_refresh_baseline
  assert_refresh_state_restored "$baseline_timer_state" "$baseline_service_state" || note_failure "install restore: refresh read-back"
  assert_scheduler_unchanged || note_failure "install restore: scheduler read-back"
  report_failure --install
  exit 1
}

# A failed `--enable` goes back to the state THIS invocation started from, so
# it never disarms a lane that was already armed.
enable_failure_restore() {
  [[ $BASHPID == "$$" ]] || exit 1
  trap - ERR
  printf 'enable failed; restoring the state this invocation started from\n' >&2
  restore_unit_state "$timer" "$invocation_timer_state"
  restore_unit_state "$service" "$invocation_service_state"
  assert_refresh_state_restored "$invocation_timer_state" "$invocation_service_state" || note_failure "enable restore: refresh read-back"
  assert_scheduler_unchanged || note_failure "enable restore: scheduler read-back"
  report_failure --enable
  exit 1
}

assert_refresh_service_inactive() {
  local active
  active=$($systemctl_bin --user is-active "$service" 2>/dev/null || true)
  [[ "${active:-inactive}" == inactive ]]
}

validate_current_receipt() {
  (
    cd "$repo"
    "$python_bin" -m scripts.scheduler_file_provider_refresh \
      --env-file "$repo/infra/env/compute.scheduler-provider-refresh.env" \
      --validate-current-receipt "$receipt" >/dev/null
  )
}

assert_refresh_service_inactive
# Per-invocation, in-memory, before any action's first mutation (D3).
scheduler_before=$(protected_state)

if [[ "$action" == --install ]]; then
  if ! refresh_units_disarmed; then
    printf 'refusing --install: %s is %s/%s; run --rollback first\n' "$armed_unit" "$armed_enabled" "$armed_active" >&2
    exit 1
  fi
  env_file="$repo/infra/env/compute.scheduler-provider-refresh.env"
  env_mode=$(stat -c '%a' "$env_file" 2>/dev/null || stat -f '%Lp' "$env_file")
  [[ -f "$env_file" && ! -L "$env_file" && "$env_mode" == 600 ]]
  if grep -Eq "^[[:space:]]*($(IFS='|'; printf '%s' "${db_selectors[*]}"))=" "$env_file"; then
    exit 2
  fi
  [[ $(grep -Ec '^NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true$' "$env_file") -eq 1 ]]
  # An existing baseline is parsed before the first mutation: a malformed one
  # (one `--rollback` would refuse) fails here with nothing changed.
  [[ ! -e "$state_root/refresh.before" && ! -L "$state_root/refresh.before" ]] || read_refresh_baseline
  # The restore baseline is captured once and kept: a recorded `refresh.before`
  # and its unit `.before` files are never rewritten by a later install.
  if [[ ! -f "$state_root/refresh.before" ]]; then
    for unit in "$service" "$timer"; do
      if [[ -f "$unit_dir/$unit" && ! -L "$unit_dir/$unit" ]]; then
        install -m 0600 "$unit_dir/$unit" "$state_root/$unit.before"
      else
        rm -f "$state_root/$unit.before"
      fi
    done
    rm -f "$state_root/refresh.before.tmp"
    {
      unit_state "$timer"
      unit_state "$service"
    } > "$state_root/refresh.before.tmp"
    mv -f "$state_root/refresh.before.tmp" "$state_root/refresh.before"
  fi
  trap install_failure_restore ERR
  for unit in "$service" "$timer"; do
    [[ -f "$repo/infra/systemd/$unit" && ! -L "$repo/infra/systemd/$unit" ]]
    install -m 0644 "$repo/infra/systemd/$unit" "$unit_dir/$unit"
    cmp -s "$repo/infra/systemd/$unit" "$unit_dir/$unit"
  done
  $systemctl_bin --user daemon-reload
  $systemctl_bin --user disable --now "$timer" >/dev/null 2>&1 || true
  $systemctl_bin --user stop "$service" >/dev/null 2>&1 || true
  assert_refresh_units_disarmed
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
  # Captured with `|| true`: `is-active` exits 3 for an inactive unit, and a
  # failure inside a command substitution must not run the handler there.
  timer_active=$($systemctl_bin --user is-active "$timer" 2>/dev/null || true)
  [[ "$timer_active" == active ]]
  [[ "$($systemctl_bin --user is-active "$service" 2>/dev/null || true)" == inactive ]]
  assert_scheduler_unchanged
  trap - ERR
  printf '{"status":"enabled_active","scheduler_unchanged":true}\n'
else
  # Before the first mutation: a missing or malformed baseline changes nothing.
  read_refresh_baseline
  restore_refresh_baseline
  assert_refresh_state_restored "$baseline_timer_state" "$baseline_service_state" || note_failure "rollback: refresh read-back"
  assert_scheduler_unchanged || note_failure "rollback: scheduler read-back"
  if [[ ${#failed_steps[@]} -ne 0 ]]; then
    report_failure --rollback
    exit 1
  fi
  printf '{"status":"rolled_back","scheduler_unchanged":true}\n'
fi
