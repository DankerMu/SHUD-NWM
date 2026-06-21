#!/usr/bin/env bash
set -euo pipefail

readonly API_COMMAND=(uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000)
readonly SCHEDULER_ONCE_COMMAND=(uv run nhms-pipeline plan-production --plan)
readonly ORCHESTRATOR_MODULE_PLAN_COMMAND=(uv run python -m services.orchestrator.cli plan-production --plan)
readonly DISPLAY_SAFE_PROBE_COMMAND=(true)
readonly DISPLAY_FORBIDDEN_PRESENT_ENVS=(
  SLURM_GATEWAY_URL
  SLURM_GATEWAY_BACKEND
  WORKSPACE_ROOT
  RUN_WORKSPACE_ROOT
  SHARED_LOG_ROOT
  NHMS_OBJECT_STORE_COPYBACK_ROOT
  NHMS_SCHEDULER_LOCK_ROOT
  NHMS_SCHEDULER_EVIDENCE_ROOT
  NHMS_SCHEDULER_RUNTIME_ROOT
  NHMS_SCHEDULER_TEMP_ROOT
  NHMS_BASINS_ROOT
  NHMS_MODEL_ASSET_ROOT
  SLURM_GATEWAY_TEMPLATE_DIR
  SLURM_GATEWAY_WORKSPACE_DIR
  MUNGE_SOCKET
  MUNGE_KEY
  SHUD_EXECUTABLE
  DOCKER_HOST
)
readonly DISPLAY_FORBIDDEN_COMMANDS=(
  sbatch
  scancel
  squeue
  srun
  sacct
  sinfo
  scontrol
  munge
  unmunge
  nhms-gfs
  nhms-era5
  nhms-ifs
  nhms-forcing
  nhms-shud-runtime
  nhms-production
  nhms-state
  nhms-flood
  nhms-model
  nhms-canonical
  nhms-parse
  nhms-pipeline
  services.orchestrator.cli
)

main() {
  local require_service_role
  local service_role

  if env_is_set "NHMS_REQUIRE_SERVICE_ROLE"; then
    require_service_role="$(normalize_bool_env "$NHMS_REQUIRE_SERVICE_ROLE" "NHMS_REQUIRE_SERVICE_ROLE")"
  else
    require_service_role="false"
  fi

  service_role="$(normalize_service_role "${NHMS_SERVICE_ROLE:-}" "$require_service_role")"

  validate_role_boundary "$service_role"
  validate_command_boundary "$service_role" "$@"

  if [ "$#" -eq 0 ]; then
    set -- "${API_COMMAND[@]}"
  fi

  exec "$@"
}

normalize_bool_env() {
  local raw="$1"
  local env_name="$2"
  local value
  value="$(normalize_role_gate_env_value "$raw")"
  case "$value" in
    0 | false | no | off)
      printf 'false'
      ;;
    1 | true | yes | on)
      printf 'true'
      ;;
    *)
      fail "SERVICE_ROLE_REQUIRE_FLAG_INVALID" "$env_name must be a recognized boolean value."
      ;;
  esac
}

normalize_service_role() {
  local raw="$1"
  local require_service_role="$2"
  local role
  role="$(normalize_role_gate_env_value "$raw")"

  if [ -z "$role" ]; then
    if [ "$require_service_role" = "true" ] || production_like_env; then
      fail "SERVICE_ROLE_REQUIRED" "NHMS_SERVICE_ROLE is required for production-like container startup."
    fi
    role="dev_monolith"
  fi

  case "$role" in
    dev_monolith | compute_control | display_readonly | slurm_gateway)
      printf '%s' "$role"
      ;;
    *)
      fail "SERVICE_ROLE_UNSUPPORTED" "NHMS_SERVICE_ROLE is not supported: $role"
      ;;
  esac
}

production_like_env() {
  case "$(normalize_role_gate_env_value "${NHMS_AUTH_MODE:-}")" in
    production | live | live_idp)
      return 0
      ;;
  esac
  case "$(normalize_role_gate_env_value "${AUTH_BACKEND:-}")" in
    live | live_idp | oidc | saml)
      return 0
      ;;
  esac
  return 1
}

normalize_role_gate_env_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value,,}"
}

validate_role_boundary() {
  local service_role="$1"

  if [ "$service_role" = "slurm_gateway" ]; then
    fail "SERVICE_ROLE_RESERVED" "NHMS_SERVICE_ROLE=slurm_gateway is reserved and cannot start the full API."
  fi

  if [ "$service_role" = "display_readonly" ]; then
    validate_display_environment
  fi
}

validate_display_environment() {
  local env_name
  for env_name in "${DISPLAY_FORBIDDEN_PRESENT_ENVS[@]}"; do
    if env_is_set "$env_name"; then
      fail "DISPLAY_BOUNDARY_CONFIG_UNSAFE" "display_readonly must not configure compute-control env $env_name."
    fi
  done

}

validate_command_boundary() {
  local service_role="$1"
  shift || true

  if [ "$service_role" != "display_readonly" ]; then
    return 0
  fi

  if [ "$#" -eq 0 ]; then
    return 0
  fi

  if command_matches "$@" -- "${API_COMMAND[@]}"; then
    return 0
  fi

  if command_matches "$@" -- "${DISPLAY_SAFE_PROBE_COMMAND[@]}"; then
    return 0
  fi

  # Static contract tokens: uv run nhms-pipeline plan-production --plan; uv run python -m services.orchestrator.cli plan-production --plan.
  local forbidden_command
  if forbidden_command="$(matched_forbidden_command_from_argv "$@")"; then
    fail "DISPLAY_COMMAND_FORBIDDEN" "display_readonly cannot run compute-control command $forbidden_command."
  fi
  if forbidden_command="$(matched_forbidden_command_from_env)"; then
    fail "DISPLAY_COMMAND_FORBIDDEN" "display_readonly cannot run env-indirected compute-control command $forbidden_command."
  fi

  fail "DISPLAY_COMMAND_FORBIDDEN" "display_readonly can only run the default API command or an audited safe probe."
}

command_matches() {
  local -a left=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    left+=("$1")
    shift
  done
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  shift
  local -a right=("$@")

  if [ "${#left[@]}" -ne "${#right[@]}" ]; then
    return 1
  fi

  local index
  for index in "${!left[@]}"; do
    if [ "${left[$index]}" != "${right[$index]}" ]; then
      return 1
    fi
  done
  return 0
}

contains_word() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    if [ "$needle" = "$item" ]; then
      return 0
    fi
  done
  return 1
}

matched_forbidden_command_from_argv() {
  local token

  if command_matches "$@" -- "${SCHEDULER_ONCE_COMMAND[@]}"; then
    printf '%s\n' "nhms-pipeline"
    return 0
  fi
  if command_matches "$@" -- "${ORCHESTRATOR_MODULE_PLAN_COMMAND[@]}"; then
    printf '%s\n' "services.orchestrator.cli"
    return 0
  fi

  for token in "$@"; do
    if matched_forbidden_command_token "$token"; then
      return 0
    fi
  done
  return 1
}

matched_forbidden_command_from_env() {
  local env_name
  local env_value

  while IFS='=' read -r env_name env_value; do
    case "$env_name" in
      NHMS_* | SLURM_* | WORKSPACE_ROOT | RUN_WORKSPACE_ROOT | SHARED_LOG_ROOT | OBJECT_STORE_ROOT | MUNGE_* | SHUD_EXECUTABLE | DOCKER_HOST)
        if matched_forbidden_command_token "$env_value"; then
          return 0
        fi
        ;;
    esac
  done < <(env)
  return 1
}

matched_forbidden_command_token() {
  local text
  local token=""
  local char
  local index
  local length

  text="$(normalize_command_scan_text "$1")"
  length="${#text}"

  for ((index = 0; index < length; index += 1)); do
    char="${text:index:1}"
    case "$char" in
      [[:alnum:]_./=:-])
        token+="$char"
        ;;
      *)
        if print_forbidden_command_match "$token"; then
          return 0
        fi
        token=""
        ;;
    esac
  done

  print_forbidden_command_match "$token"
}

normalize_command_scan_text() {
  local text="$1"
  text="${text//\'/}"
  text="${text//\"/}"
  text="${text//\\/}"
  printf '%s' "$text"
}

print_forbidden_command_match() {
  local token="$1"
  local base

  if [ -z "$token" ]; then
    return 1
  fi

  base="${token##*/}"
  if contains_word "$base" "${DISPLAY_FORBIDDEN_COMMANDS[@]}"; then
    printf '%s\n' "$base"
    return 0
  fi
  return 1
}

env_is_set() {
  local env_name="$1"
  [ "${!env_name+x}" = "x" ]
}

fail() {
  local code="$1"
  local message="$2"
  printf 'nhms-entrypoint[%s]: %s\n' "$code" "$message" >&2
  exit 64
}

main "$@"
