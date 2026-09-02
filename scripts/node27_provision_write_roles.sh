#!/usr/bin/env bash
# Provision the node-27 write-path least-privilege roles (issue #1774).
#
# Two modes, mirroring the staged cutover in OpenSpec design D5:
#
#   --roles-only   The ADDITIVE phase.  Creates/converges nhms_ingest_rw and
#                  nhms_download_rw, their DML grants, sequence USAGE, default
#                  privileges and the cold-tablespace CREATE grant, then runs
#                  the negative COPY ... FROM PROGRAM probes and a non-strict
#                  audit.  No `ALTER ... OWNER TO` is executed and no relation
#                  lock is taken, so it is safe to run on the live primary
#                  while every unit still connects as `nhms`.  This is the
#                  pre-merge phase.
#
#   (default)      Full mode.  Everything above, plus the ownership transfer:
#                  a before/after capture of relacl and of nhms_display_ro's
#                  effective SELECT set, the per-relation autocommitted
#                  `ALTER ... OWNER TO` loop under `SET lock_timeout = '5s'`
#                  retried over up to N passes, and a strict trailing audit.
#                  This is the post-merge phase and must run inside a
#                  timer-stopped window.
#
# Exit codes
#   0  provisioned, audit clean
#   2  usage / environment error
#   3  provisioning incomplete: relations still owned by the old role, or the
#      trailing audit refused.  Do NOT proceed with the env cutover.
#   4  nhms_display_ro's effective SELECT set changed across the transfer.
#
# Secrets: passwords are read from NODE27_INGEST_RW_PASSWORD /
# NODE27_DOWNLOAD_RW_PASSWORD and forwarded to psql by NAME only
# (`docker exec -e VAR`), never as an argument and never echoed.  Unset or
# empty means "leave the existing password alone".

set -euo pipefail

readonly APP_SCHEMAS_SQL="ARRAY['core', 'hydro', 'met', 'ops', 'map', 'flood']"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SQL_FILE="${REPO_ROOT}/db/roles/node27_write_roles.sql"

DOCKER_BIN="${NODE27_WRITE_ROLES_DOCKER:-docker}"
CONTAINER="${NODE27_WRITE_ROLES_CONTAINER:-nhms-db}"
DATABASE="${NODE27_WRITE_ROLES_DATABASE:-nhms}"
SUPERUSER="${NODE27_WRITE_ROLES_SUPERUSER:-nhms}"
MAX_PASSES="${NODE27_WRITE_ROLES_MAX_PASSES:-5}"
ROLES_ONLY=0

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Usage: node27_provision_write_roles.sh [--roles-only] [--container NAME]
                                       [--database NAME] [--superuser NAME]
                                       [--max-passes N]

Environment overrides: NODE27_WRITE_ROLES_DOCKER, NODE27_WRITE_ROLES_CONTAINER,
NODE27_WRITE_ROLES_DATABASE, NODE27_WRITE_ROLES_SUPERUSER,
NODE27_WRITE_ROLES_MAX_PASSES.
EOF
}

die() {
  echo "node27-write-roles: $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --roles-only) ROLES_ONLY=1; shift ;;
    --container) [[ $# -ge 2 ]] || die "--container needs a value"; CONTAINER="$2"; shift 2 ;;
    --database) [[ $# -ge 2 ]] || die "--database needs a value"; DATABASE="$2"; shift 2 ;;
    --superuser) [[ $# -ge 2 ]] || die "--superuser needs a value"; SUPERUSER="$2"; shift 2 ;;
    --max-passes) [[ $# -ge 2 ]] || die "--max-passes needs a value"; MAX_PASSES="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -r "${SQL_FILE}" ]] || die "provision SQL not readable: ${SQL_FILE}"
[[ "${MAX_PASSES}" =~ ^[1-9][0-9]*$ ]] || die "--max-passes must be a positive integer, got: ${MAX_PASSES}"

# Password env vars are forwarded by NAME. Only when non-empty: `docker exec -e
# VAR` on an empty value would set an EMPTY password, not skip the change.
PASSWORD_ENV_ARGS=()
for var in NODE27_INGEST_RW_PASSWORD NODE27_DOWNLOAD_RW_PASSWORD; do
  if [[ -n "${!var:-}" ]]; then
    PASSWORD_ENV_ARGS+=(-e "${var}")
  else
    echo "node27-write-roles: ${var} unset -- that role's password will be left unchanged" >&2
  fi
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/node27-write-roles.XXXXXX")"
cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT

# Run the provision SQL file. $1 is the phase tag (also passed to psql as -v
# phase=..., which makes every invocation attributable in a receipt).
run_sql_file() {
  local phase="$1"; shift
  "${DOCKER_BIN}" exec -i "${PASSWORD_ENV_ARGS[@]+"${PASSWORD_ENV_ARGS[@]}"}" "${CONTAINER}" \
    psql -U "${SUPERUSER}" -d "${DATABASE}" -X -q \
    -v ON_ERROR_STOP=1 -v "phase=${phase}" "$@" \
    < "${SQL_FILE}"
}

# Run one ad-hoc query, unaligned/tuples-only, for the captures and counters.
run_query() {
  local phase="$1" sql="$2"
  "${DOCKER_BIN}" exec -i "${CONTAINER}" \
    psql -U "${SUPERUSER}" -d "${DATABASE}" -X -q -t -A \
    -v ON_ERROR_STOP=1 -v "phase=${phase}" -c "${sql}" \
    < /dev/null
}

RELACL_SQL="SELECT n.nspname || '.' || c.relname || ' | ' || c.relkind::text || ' | ' \
|| c.relowner::regrole::text || ' | ' || coalesce(array_to_string(c.relacl, ' '), '(default)') \
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
WHERE n.nspname = ANY (${APP_SCHEMAS_SQL}) AND c.relkind IN ('r', 'p', 'S', 'v', 'm') \
ORDER BY 1"

DISPLAY_SELECT_SQL="SELECT n.nspname || '.' || c.relname \
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
JOIN pg_roles r ON r.rolname = 'nhms_display_ro' \
WHERE n.nspname = ANY (${APP_SCHEMAS_SQL}) AND c.relkind IN ('r', 'p', 'v', 'm') \
AND has_table_privilege(r.oid, c.oid, 'SELECT') ORDER BY 1"

REMAINING_SQL="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
WHERE n.nspname = ANY (${APP_SCHEMAS_SQL}) AND c.relkind IN ('r', 'p', 'S', 'v', 'm') \
AND c.relowner <> 'nhms_ingest_rw'::regrole"

echo "node27-write-roles: container=${CONTAINER} database=${DATABASE} superuser=${SUPERUSER}"

if [[ "${ROLES_ONLY}" -eq 1 ]]; then
  echo "node27-write-roles: mode=roles-only (additive; no ownership transfer, no relation lock)"
  run_sql_file roles -v do_roles=on -v do_ownership=off -v do_audit=on -v strict_audit=off
  echo "node27-write-roles: roles-only phase complete; ownership transfer deferred to the post-merge run"
  exit 0
fi

echo "node27-write-roles: mode=full (additive phase + ownership transfer)"

# --- before capture -----------------------------------------------------------
# The display role's grants have no source of truth in db/; they exist only in
# the live catalog, so the boundary is asserted as a before/after capture.
run_query display_before "${DISPLAY_SELECT_SQL}" > "${WORK_DIR}/display-before.txt"
run_query relacl_before "${RELACL_SQL}" > "${WORK_DIR}/relacl-before.txt"
echo "node27-write-roles: captured $(wc -l < "${WORK_DIR}/display-before.txt" | tr -d ' ') display-visible relation(s) before the transfer"

# --- additive phase + ownership passes ---------------------------------------
remaining=""
pass=1
while [[ "${pass}" -le "${MAX_PASSES}" ]]; do
  echo "node27-write-roles: ownership pass ${pass}/${MAX_PASSES}"
  if [[ "${pass}" -eq 1 ]]; then
    run_sql_file ownership -v do_roles=on -v do_ownership=on -v do_audit=off -v strict_audit=off -v "pass=${pass}"
  else
    run_sql_file ownership -v do_roles=off -v do_ownership=on -v do_audit=off -v strict_audit=off -v "pass=${pass}"
  fi
  remaining="$(run_query remaining "${REMAINING_SQL}" | tr -d '[:space:]')"
  echo "node27-write-roles: after pass ${pass}: ${remaining} relation(s) still not owned by nhms_ingest_rw"
  if [[ "${remaining}" == "0" ]]; then
    break
  fi
  pass=$((pass + 1))
done

# --- after capture + diffs ----------------------------------------------------
run_query display_after "${DISPLAY_SELECT_SQL}" > "${WORK_DIR}/display-after.txt"
run_query relacl_after "${RELACL_SQL}" > "${WORK_DIR}/relacl-after.txt"

echo "node27-write-roles: relacl diff across the transfer (informational -- ALTER ... OWNER TO rewrites grantor references):"
if diff -u "${WORK_DIR}/relacl-before.txt" "${WORK_DIR}/relacl-after.txt"; then
  echo "  (no relacl change)"
fi

display_regression=0
echo "node27-write-roles: nhms_display_ro effective SELECT set diff (must be empty):"
if diff -u "${WORK_DIR}/display-before.txt" "${WORK_DIR}/display-after.txt"; then
  echo "  (identical -- read-side boundary preserved)"
else
  display_regression=1
  echo "node27-write-roles: DISPLAY GRANT REGRESSION -- the read-side SELECT set changed" >&2
fi

# --- strict trailing audit ----------------------------------------------------
audit_rc=0
set +e
run_sql_file audit -v do_roles=off -v do_ownership=off -v do_audit=on -v strict_audit=on
audit_rc=$?
set -e

if [[ "${display_regression}" -eq 1 ]]; then
  echo "node27-write-roles: FAILED -- nhms_display_ro lost or gained SELECT privileges; do not cut the env files over" >&2
  exit 4
fi

if [[ "${remaining}" != "0" ]]; then
  echo "node27-write-roles: FAILED -- ${remaining} relation(s) still owned by the old role after ${MAX_PASSES} pass(es)." >&2
  echo "node27-write-roles: this is a PARTIAL, audit-visible transfer, not a rollback: every unit still connects as the superuser," >&2
  echo "node27-write-roles: so tiering and ANALYZE keep working. Re-run this script; do NOT cut the env files over." >&2
  exit 3
fi

if [[ "${audit_rc}" -ne 0 ]]; then
  echo "node27-write-roles: FAILED -- trailing audit refused (psql exit ${audit_rc}); do not cut the env files over" >&2
  exit 3
fi

echo "node27-write-roles: full provision complete; audit clean"
