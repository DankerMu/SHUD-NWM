#!/usr/bin/env bash
# Provision the node-27 write-path least-privilege roles (issue #1774).
#
# Two modes, mirroring the staged cutover in OpenSpec design D5:
#
#   --roles-only   The ADDITIVE phase.  Creates/converges nhms_ingest_rw and
#                  nhms_download_rw, their DML grants, sequence USAGE, default
#                  privileges, the cold-tablespace CREATE grant and the event
#                  trigger that refuses CREATE RULE / CREATE TRIGGER from the
#                  write roles, then runs the negative COPY ... FROM PROGRAM
#                  probes and a non-strict audit.  No `ALTER ... OWNER TO` is
#                  executed and no relation lock is taken, so it is safe to run
#                  on the live primary while every unit still connects as
#                  `nhms`.  This is the pre-merge phase.
#
#   (default)      Full mode.  Everything above, plus the ownership transfer:
#                  a before/after capture of relacl and of nhms_display_ro's
#                  effective SELECT set, the per-relation autocommitted
#                  `ALTER ... OWNER TO` loop under `SET lock_timeout = '5s'`
#                  retried over up to N passes (back-to-back by default; see
#                  --pass-interval), and a strict trailing audit.  This is the
#                  post-merge phase and must run inside a timer-stopped window.
#
# Residual after this change (design D2), stated plainly because the
# COPY ... FROM PROGRAM probe alone reads as more than it proves: the write
# credential's blast radius is "drop or truncate any application relation" --
# no DIRECT program execution, no role/database creation, no direct catalog
# escape.  What ownership DOES carry is the ability to attach a body to a
# relation (CREATE RULE / CREATE TRIGGER, a column DEFAULT, a CHECK), and such a
# body is evaluated with the authority of whoever next WRITES the row -- the
# migration, seed and replay lanes all stay on the superuser `nhms`.  Two
# preventions and one detection cover it, none replacing another:
#   * the event trigger installed by the additive phase refuses the rule/trigger
#     DDL family for the write roles -- except on a hypertable, where
#     TimescaleDB routes the command around it (measured);
#   * full mode revokes TEMP from PUBLIC, which removes `pg_temp`, the only
#     schema in which a write role can AUTHOR a function at all;
#   * the audit judges every stored expression by the PROVENANCE of the
#     functions it reaches (temp schema / non-superuser owner / not executable
#     by the write role / not on the migration ALLOW-list) and every rule and
#     trigger against the migration allow-list, with TimescaleDB's blocker keyed
#     on function identity rather than on its name.
# The function leg is an allow-list and not a deny-list because a deny-list
# enumerates effects, and `query_to_xml`'s effect is "evaluate this SQL string
# as the caller" -- measured walking straight through the deny-list at exit 0.
# The allow-list has to be EXTENDED when a migration references a new function;
# a unit test derives that set from db/migrations/** so the failure lands there
# and not on the live audit.
# Not covered, and a follow-up rather than a fix here: removing the
# superuser-write half itself.
#
# Retry window.  The passes are the whole tolerance for a lock holder that
# outlives one `ALTER`: the effective window is
# `--max-passes x lock_timeout + (--max-passes - 1) x --pass-interval`
# (default 5 x 5 s + 0 = 25 s of wall clock, back-to-back -- the interval is
# only slept BETWEEN passes, never after the last one).  Widen it with
# --max-passes / NODE27_WRITE_ROLES_MAX_PASSES, or space the passes with
# --pass-interval / NODE27_WRITE_ROLES_PASS_INTERVAL when the holder is a long
# display scan rather than a transient one.
#
# Exit codes
#   0  provisioned, audit clean
#   2  usage / environment error
#   3  provisioning refused or incomplete: any `docker exec`/psql invocation
#      failed in either mode (its own status -- 1 fatal / 2 connection / 3 script
#      error under ON_ERROR_STOP, or docker's own 125/126/127 -- is mapped to 3
#      and never leaks as a runner code), relations still owned by the old role,
#      or the trailing audit refused.  Do NOT proceed with the env cutover.
#   4  nhms_display_ro's effective SELECT set changed across the transfer, i.e.
#      the SELECT privilege set measured per relation with has_table_privilege
#      is not identical before and after.
#
# What the exit-4 gate does NOT cover: a view or matview executes as its OWNER
# (PG 15 defaults to security_invoker=false), so transferring a view whose body
# reads a relation OUTSIDE the six application schemas can make it unreadable for
# display even though nhms_display_ro's privilege set is byte-identical.  T7
# enumerates relkind v/m before the transfer for exactly that reason.
#
# Secrets: passwords are read from NODE27_INGEST_RW_PASSWORD /
# NODE27_DOWNLOAD_RW_PASSWORD and forwarded to psql by NAME only
# (`docker exec -e VAR`), never as an argument, never echoed, and never expanded
# into a `set -x` trace.  Unset or empty means "leave an EXISTING role's password
# alone"; a role this run creates then has no password and cannot log in over TCP
# until a later run sets one.

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
PASS_INTERVAL="${NODE27_WRITE_ROLES_PASS_INTERVAL:-0}"
ROLES_ONLY=0

usage() {
  # Print the whole header comment block and stop at the first non-`#` line, so
  # --help cannot silently truncate when the header grows (a fixed sed range did).
  awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
  cat <<'EOF'

Usage: node27_provision_write_roles.sh [--roles-only] [--container NAME]
                                       [--database NAME] [--superuser NAME]
                                       [--max-passes N] [--pass-interval SECONDS]

  --max-passes N            ownership retry passes (default 5, positive integer,
                            at most 100)
  --pass-interval SECONDS   seconds to sleep between ownership passes (default 0
                            = back-to-back; non-negative integer, at most 3600).
                            Only slept when another pass will actually run.

Environment overrides: NODE27_WRITE_ROLES_DOCKER, NODE27_WRITE_ROLES_CONTAINER,
NODE27_WRITE_ROLES_DATABASE, NODE27_WRITE_ROLES_SUPERUSER,
NODE27_WRITE_ROLES_MAX_PASSES, NODE27_WRITE_ROLES_PASS_INTERVAL.
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
    --pass-interval) [[ $# -ge 2 ]] || die "--pass-interval needs a value"; PASS_INTERVAL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -r "${SQL_FILE}" ]] || die "provision SQL not readable: ${SQL_FILE}"
[[ "${MAX_PASSES}" =~ ^[1-9][0-9]*$ ]] || die "--max-passes must be a positive integer, got: ${MAX_PASSES}"
[[ "${PASS_INTERVAL}" =~ ^(0|[1-9][0-9]*)$ ]] || die "--pass-interval must be a non-negative integer number of seconds, got: ${PASS_INTERVAL}"
# Upper bounds are an operator-typo guard, not a policy: the cutover runs inside
# a timer-stopped window, so a mistyped `--pass-interval 36000` (or
# `--max-passes 5000`) must be refused up front rather than silently holding
# that window open for hours.
readonly MAX_PASSES_LIMIT=100
readonly PASS_INTERVAL_LIMIT=3600
[[ "${MAX_PASSES}" -le "${MAX_PASSES_LIMIT}" ]] || die "--max-passes must be <= ${MAX_PASSES_LIMIT}, got: ${MAX_PASSES}"
[[ "${PASS_INTERVAL}" -le "${PASS_INTERVAL_LIMIT}" ]] || die "--pass-interval must be <= ${PASS_INTERVAL_LIMIT} seconds, got: ${PASS_INTERVAL}"

# Password env vars are forwarded by NAME. Only when non-empty: `docker exec -e
# VAR` on an empty value would set an EMPTY password, not skip the change.
#
# `${!var:+x}` and not `${!var:-}`: the latter expands to the PASSWORD itself, so
# an operator debugging this script with `bash -x` would get the cleartext in the
# execution trace (`+ [[ -n hunter2 ]]`) and, typically, into a ticket. The `:+`
# form traces as `+ [[ -n x ]]` and tests the same "set and non-empty" condition.
PASSWORD_ENV_ARGS=()
for var in NODE27_INGEST_RW_PASSWORD NODE27_DOWNLOAD_RW_PASSWORD; do
  if [[ -n "${!var:+x}" ]]; then
    PASSWORD_ENV_ARGS+=(-e "${var}")
  else
    echo "node27-write-roles: ${var} unset -- that role keeps its existing password; a role CREATED by this run has none and cannot log in over TCP until one is set" >&2
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

# Map ANY docker/psql failure to the documented exit 3.  Without this, `set -e`
# propagates psql's own status (1 fatal / 2 connection / 3 script error) or
# docker's (125/126/127) as this script's exit code -- and 2 reads as "usage /
# environment error" while 1 is not a documented code at all.
run_or_fail() {
  local what="$1"; shift
  local rc=0
  set +e
  "$@"
  rc=$?
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    echo "node27-write-roles: FAILED -- ${what} (docker exec/psql exit ${rc}); do not cut the env files over" >&2
    exit 3
  fi
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
  # Any refusal here is a refused provision run -> 3 (run_or_fail), matching the
  # full-mode paths below; psql's own status never reaches the caller.
  run_or_fail "roles-only provision refused" \
    run_sql_file roles -v do_roles=on -v do_ownership=off -v do_audit=on -v strict_audit=off
  echo "node27-write-roles: roles-only phase complete; ownership transfer deferred to the post-merge run"
  exit 0
fi

echo "node27-write-roles: mode=full (additive phase + ownership transfer)"

# --- before capture -----------------------------------------------------------
# The display role's grants have no source of truth in db/; they exist only in
# the live catalog, so the boundary is asserted as a before/after capture.
run_or_fail "display SELECT-set capture before the transfer" \
  run_query display_before "${DISPLAY_SELECT_SQL}" > "${WORK_DIR}/display-before.txt"
run_or_fail "relacl capture before the transfer" \
  run_query relacl_before "${RELACL_SQL}" > "${WORK_DIR}/relacl-before.txt"
echo "node27-write-roles: captured $(wc -l < "${WORK_DIR}/display-before.txt" | tr -d ' ') display-visible relation(s) before the transfer"

# --- additive phase + ownership passes ---------------------------------------
remaining=""
pass=1
while [[ "${pass}" -le "${MAX_PASSES}" ]]; do
  echo "node27-write-roles: ownership pass ${pass}/${MAX_PASSES}"
  if [[ "${pass}" -eq 1 ]]; then
    run_or_fail "ownership pass ${pass}" \
      run_sql_file ownership -v do_roles=on -v do_ownership=on -v do_audit=off -v strict_audit=off -v "pass=${pass}"
  else
    run_or_fail "ownership pass ${pass}" \
      run_sql_file ownership -v do_roles=off -v do_ownership=on -v do_audit=off -v strict_audit=off -v "pass=${pass}"
  fi
  run_or_fail "remaining-relation count after pass ${pass}" \
    run_query remaining "${REMAINING_SQL}" > "${WORK_DIR}/remaining.txt"
  remaining="$(tr -d '[:space:]' < "${WORK_DIR}/remaining.txt")"
  echo "node27-write-roles: after pass ${pass}: ${remaining} relation(s) still not owned by nhms_ingest_rw"
  if [[ "${remaining}" == "0" ]]; then
    break
  fi
  if [[ "${PASS_INTERVAL}" -gt 0 && "${pass}" -lt "${MAX_PASSES}" ]]; then
    echo "node27-write-roles: waiting ${PASS_INTERVAL}s before the next ownership pass"
    sleep "${PASS_INTERVAL}"
  fi
  pass=$((pass + 1))
done

# --- after capture + diffs ----------------------------------------------------
run_or_fail "display SELECT-set capture after the transfer" \
  run_query display_after "${DISPLAY_SELECT_SQL}" > "${WORK_DIR}/display-after.txt"
run_or_fail "relacl capture after the transfer" \
  run_query relacl_after "${RELACL_SQL}" > "${WORK_DIR}/relacl-after.txt"

echo "node27-write-roles: relacl diff across the transfer (informational -- ALTER ... OWNER TO rewrites grantor references):"
if diff -u "${WORK_DIR}/relacl-before.txt" "${WORK_DIR}/relacl-after.txt"; then
  echo "  (no relacl change)"
fi

display_regression=0
echo "node27-write-roles: nhms_display_ro effective SELECT set diff (must be empty):"
if diff -u "${WORK_DIR}/display-before.txt" "${WORK_DIR}/display-after.txt"; then
  # What is measured, and only this: has_table_privilege(nhms_display_ro, rel,
  # 'SELECT') per relation in the six schemas. A view still EXECUTES as its
  # owner, so this gate cannot see a view over a relation outside those schemas
  # becoming unreadable after the transfer -- see the header note and T7.
  echo "  (SELECT privilege set unchanged -- has_table_privilege per relation, identical before and after)"
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
  echo "node27-write-roles: FAILED -- nhms_display_ro's SELECT privilege set (has_table_privilege per relation) changed; do not cut the env files over" >&2
  exit 4
fi

if [[ "${remaining}" != "0" ]]; then
  echo "node27-write-roles: FAILED -- ${remaining} relation(s) still owned by the old role after ${MAX_PASSES} pass(es)." >&2
  echo "node27-write-roles: this is a PARTIAL, audit-visible transfer, not a rollback: every unit still connects as the superuser," >&2
  echo "node27-write-roles: so tiering and ANALYZE keep working. Re-run this script; do NOT cut the env files over." >&2
  exit 3
fi

if [[ "${audit_rc}" -ne 0 ]]; then
  echo "node27-write-roles: FAILED -- trailing audit refused (docker exec/psql exit ${audit_rc}); do not cut the env files over" >&2
  exit 3
fi

echo "node27-write-roles: full provision complete; audit clean"
