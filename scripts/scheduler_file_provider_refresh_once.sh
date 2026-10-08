#!/usr/bin/env bash
set -euo pipefail

repo=/scratch/frd_muziyao/NWM
# The wait for a running scheduler pass (#2749). Fixed values, not environment
# overrides: the bound plus a refresh must fit the unit's start timeout.
systemctl_bin=/usr/bin/systemctl
scheduler_unit=nhms-compute-scheduler.service
scheduler_wait_bound_seconds=5400
scheduler_wait_poll_seconds=15
env_file="$repo/infra/env/compute.scheduler-provider-refresh.env"
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
allowed_keys='^(NHMS_BASINS_ROOT|OBJECT_STORE_ROOT|NHMS_SCHEDULER_PROVIDER_STORE_ROOT|OBJECT_STORE_PREFIX|NHMS_SCHEDULER_REGISTRY_MANIFEST|NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST|NHMS_SCHEDULER_CANONICAL_READINESS_INDEX|NHMS_SCHEDULER_STATE_INDEX|NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK|NHMS_SCHEDULER_REQUIRE_DIRECT_GRID|NHMS_REGISTRY_CUTOVER_DECLARATION_PATH)$'

[[ -f "$env_file" && ! -L "$env_file" ]]
# A refresh must not run beside a scheduler pass, so a running pass is waited
# for. The scheduler service is a oneshot: during a pass its state is
# `activating`, for which `is-active` exits non-zero as it does for `inactive`.
# The state is therefore what `is-active` prints; its exit status is ignored.
# The two lists are NOT_RUNNING_STATES and RUNNING_STATES of
# scripts/model_succession/systemd.py. Anything else (nothing printed, the
# binary missing, another word) is an unknown state: refuse.
# Under systemd the unit's Before= keeps the next pass queued behind this job.
scheduler_waited=0
while :; do
  scheduler_state=$("$systemctl_bin" --user is-active "$scheduler_unit") || true
  case "$scheduler_state" in
    inactive|failed)  # NOT_RUNNING_STATES
      break
      ;;
    active|activating|deactivating|reloading)  # RUNNING_STATES
      ;;
    *)
      echo "refusing to refresh: $systemctl_bin --user is-active $scheduler_unit printed '$scheduler_state'; the state of the scheduler service is unknown, so nothing was refreshed" >&2
      exit 3
      ;;
  esac
  if [[ "$scheduler_waited" -ge "$scheduler_wait_bound_seconds" ]]; then
    echo "refusing to refresh: $scheduler_unit is still $scheduler_state after waiting $scheduler_waited s (bound $scheduler_wait_bound_seconds s); nothing was refreshed" >&2
    exit 3
  fi
  if [[ $((scheduler_waited % 60)) -eq 0 ]]; then
    echo "waiting for the running scheduler pass before refreshing: $scheduler_unit is $scheduler_state, waited $scheduler_waited s (poll $scheduler_wait_poll_seconds s, bound $scheduler_wait_bound_seconds s)" >&2
  fi
  sleep "$scheduler_wait_poll_seconds"
  scheduler_waited=$((scheduler_waited + scheduler_wait_poll_seconds))
done
mode=$(stat -c '%a' "$env_file" 2>/dev/null || stat -f '%Lp' "$env_file")
[[ "$mode" == "600" ]]
if grep -Eq "^[[:space:]]*($(IFS='|'; printf '%s' "${db_selectors[*]}"))=" "$env_file"; then
  exit 2
fi

# The EnvironmentFile is parsed as data instead of sourced as shell. Only the
# fixed refresh keys are accepted, so mode-0600 configuration cannot execute
# commands or smuggle an unrelated runtime selector into the service.
loaded_keys='|'
while IFS= read -r line || [[ -n "$line" ]]; do
  line=${line%$'\r'}
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" == *=* ]]
  key=${line%%=*}
  value=${line#*=}
  [[ "$key" =~ $allowed_keys && -n "$value" && "$loaded_keys" != *"|$key|"* ]]
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]]
  export "$key=$value"
  loaded_keys+="$key|"
done < "$env_file"

for required in \
  NHMS_BASINS_ROOT OBJECT_STORE_ROOT NHMS_SCHEDULER_PROVIDER_STORE_ROOT OBJECT_STORE_PREFIX \
  NHMS_SCHEDULER_REGISTRY_MANIFEST NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST \
  NHMS_SCHEDULER_CANONICAL_READINESS_INDEX \
  NHMS_SCHEDULER_STATE_INDEX NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT \
  NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT \
  NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK \
  NHMS_SCHEDULER_REQUIRE_DIRECT_GRID; do
  [[ "$loaded_keys" == *"|$required|"* ]]
done
[[ "$NHMS_SCHEDULER_REQUIRE_DIRECT_GRID" == true ]]

# User-manager variables are inherited independently of EnvironmentFile=. Make
# the final wrapper environment DB-free even when the manager was contaminated.
unset "${db_selectors[@]}"

cd "$repo"
exec "$repo/.venv/bin/python" -m scripts.scheduler_file_provider_refresh "$@"
