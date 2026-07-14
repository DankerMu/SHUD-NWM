#!/usr/bin/env bash
set -euo pipefail

repo=/scratch/frd_muziyao/NWM
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
allowed_keys='^(NHMS_BASINS_ROOT|OBJECT_STORE_ROOT|NHMS_SCHEDULER_PROVIDER_STORE_ROOT|OBJECT_STORE_PREFIX|NHMS_SCHEDULER_REGISTRY_MANIFEST|NHMS_SCHEDULER_CANONICAL_READINESS_INDEX|NHMS_SCHEDULER_STATE_INDEX|NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT|NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK)$'

[[ -f "$env_file" && ! -L "$env_file" ]]
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
  NHMS_SCHEDULER_REGISTRY_MANIFEST NHMS_SCHEDULER_CANONICAL_READINESS_INDEX \
  NHMS_SCHEDULER_STATE_INDEX NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT \
  NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT \
  NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK; do
  [[ "$loaded_keys" == *"|$required|"* ]]
done

# User-manager variables are inherited independently of EnvironmentFile=. Make
# the final wrapper environment DB-free even when the manager was contaminated.
unset "${db_selectors[@]}"

cd "$repo"
exec "$repo/.venv/bin/python" -m scripts.scheduler_file_provider_refresh "$@"
