#!/usr/bin/env bash
set -euo pipefail

readonly API_COMMAND=(uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000)
readonly SCHEDULER_ONCE_COMMAND=(uv run nhms-pipeline plan-production --plan)
readonly DISPLAY_FORBIDDEN_PRESENT_ENVS=(
  SLURM_GATEWAY_URL
  SLURM_GATEWAY_BACKEND
  WORKSPACE_ROOT
  RUN_WORKSPACE_ROOT
  SHARED_LOG_ROOT
  OBJECT_STORE_ROOT
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
  munge
  unmunge
  nhms-pipeline
)

main() {
  local require_service_role
  local service_role
  require_service_role="$(normalize_bool_env "${NHMS_REQUIRE_SERVICE_ROLE:-}" "NHMS_REQUIRE_SERVICE_ROLE")"
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
  value="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    "" | 0 | false | no | off)
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
  role="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"

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
  case "$(printf '%s' "${NHMS_AUTH_MODE:-}" | tr '[:upper:]' '[:lower:]')" in
    production | live | live_idp)
      return 0
      ;;
  esac
  case "$(printf '%s' "${AUTH_BACKEND:-}" | tr '[:upper:]' '[:lower:]')" in
    live | live_idp | oidc | saml)
      return 0
      ;;
  esac
  return 1
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

  if command_matches "$@" -- "${SCHEDULER_ONCE_COMMAND[@]}"; then
    fail "DISPLAY_COMMAND_FORBIDDEN" "display_readonly cannot run the compute scheduler command."
  fi

  local token
  local forbidden_command
  for token in "$@"; do
    if forbidden_command="$(matched_forbidden_command_token "$token")"; then
      fail "DISPLAY_COMMAND_FORBIDDEN" "display_readonly cannot run compute-control command $forbidden_command."
    fi
  done
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

matched_forbidden_command_token() {
  local text="$1"
  local token=""
  local char
  local index
  local length="${#text}"

  for ((index = 0; index < length; index += 1)); do
    char="${text:index:1}"
    case "$char" in
      [[:alnum:]_./=-])
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
