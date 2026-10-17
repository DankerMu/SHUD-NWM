#!/usr/bin/env bash
# node-22 env backup sweep (#2918, OpenSpec node22-config-hygiene design D5).
#
# Backup copies of env files (`*.bak*`, `*.backup-*`, `*.pre-*`) never live
# under `infra/env/`; their home is `~/NWM-presync-backup-<UTC date>/infra-env/`
# (infra/env/README.md "Backup copies"). This script lists the backup copies
# directly under --env-dir and, only with --apply, moves them there.
#
#   node22_env_backup_sweep.sh --env-dir DIR [--systemd-user-dir DIR]
#       [--backup-root DIR] [--home DIR] [--apply]
#
# Defaults: --backup-root  $HOME/NWM-presync-backup-<UTC %Y-%m-%d>
#           --home         $HOME (only used to expand `%h` in unit files)
#           --systemd-user-dir  $XDG_CONFIG_HOME/systemd/user when that is
#                          absolute, else $HOME/.config/systemd/user
#           --env-dir      REQUIRED (a forgotten flag must not sweep whatever
#                          checkout the shell happens to be in)
#
# Candidates: regular, non-symlink files directly under --env-dir whose
# basename matches `*.bak*`, `*.backup-*` or `*.pre-*` (no recursion, no dot
# files), sorted bytewise.
#
# Refusal set: every non-empty `EnvironmentFile=` value in the top-level
# `nhms-*` files of --systemd-user-dir (any suffix, `.pre-*` leftovers
# included) and in one level of `nhms-*.service.d/*.conf`. Each value has one
# leading `-` stripped and `%h` expanded with --home; both sides are
# normalized with `readlink -f` (falling back to the expanded string when that
# fails). A candidate in the set exits 2 naming the unit, dry run included,
# before anything moves. The systemd directory is only read.
#
# Only literal paths and `%h` are understood. A value holding a wildcard (`*`,
# `?`, `[`), any other specifier (`%` not followed by `h`, `%%` and a trailing
# `%` included) or a line ending in `\` (continuation) cannot be compared, so
# the refusal set would be incomplete: the run exits 2 at scan time naming the
# unit and the value, nothing moved. Not read: units loaded but not on disk
# under --systemd-user-dir, non-`nhms-` units, global `service.d/` drop-ins and
# other unit search paths.
#
# Order of checks (fixed):
#   1. arguments (stderr only, no receipt);
#   2. <backup-root>/infra-env resolving to or under --env-dir or
#      --systemd-user-dir, or <backup-root>/infra-env being a symlink -> 2,
#      nothing created, no receipt;
#   3. `mkdir -p` + `chmod 700` of <backup-root>/infra-env (failure -> 2, no
#      receipt); an already existing receipt for this second -> 2, untouched;
#   4. from here every exit writes
#      <backup-root>/infra-env/sweep-receipt-<UTC %Y%m%dT%H%M%SZ>.json
#      (schema nhms.node22.env_backup_sweep.v1, file names only, never file
#      content): unreadable/missing --env-dir or --systemd-user-dir, zero
#      `nhms-*` unit files, an unparseable `EnvironmentFile=` value, a control
#      character in a candidate basename, refusals, existing targets, a failed
#      move -> 2.
#
# --apply checks every target (`-e` or `-L`) before the first move; any hit
# exits 2 with nothing moved (a dry run reports the same conflicts and exits 2
# too). Each `mv -n` is followed by "source gone, target present", else 2,
# and by a `moved: <name>` line on stderr (the record if the receipt is lost).
# Nothing is ever deleted. Exit codes: 0 or 2 only.

set -euo pipefail
umask 077
export LC_ALL=C
shopt -s nullglob

SCHEMA_VERSION="nhms.node22.env_backup_sweep.v1"
BS='\'
DQ='"'
H_SPEC='%h'

note() { printf 'node22-env-backup-sweep: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<'EOF'
usage: node22_env_backup_sweep.sh --env-dir DIR [--systemd-user-dir DIR]
           [--backup-root DIR] [--home DIR] [--apply]
Dry run by default: prints one `plan:` line per backup copy and writes a
receipt. --apply moves the copies (mv -n) into <backup-root>/infra-env/.
EOF
}

arg_error() {
  note "argument error: $*"
  usage
  exit 2
}

has_cntrl() {
  case $1 in
  *[[:cntrl:]]*) return 0 ;;
  *) return 1 ;;
  esac
}

strip_trailing_slashes() {
  local p=$1
  while [ "${#p}" -gt 1 ] && [ "${p%/}" != "$p" ]; do
    p=${p%/}
  done
  printf '%s' "$p"
}

# readlink -f, falling back to the string itself (macOS fails on a missing
# final component, GNU on a missing parent).
normalize() {
  local r
  if r=$(readlink -f -- "$1" 2>/dev/null) && [ -n "$r" ]; then
    printf '%s' "$r"
  else
    printf '%s' "$1"
  fi
}

# Canonical spelling of a path that may not exist yet: resolve the nearest
# existing ancestor directory and re-append the rest. A `.` or `..` left in
# the unresolved rest cannot be judged and fails.
resolve_path() {
  local p=$1 rest="" r
  while [ ! -d "$p" ]; do
    case $p in
    */*)
      rest="/${p##*/}$rest"
      p=${p%/*}
      [ -n "$p" ] || p=/
      ;;
    *)
      rest="/$p$rest"
      p=.
      ;;
    esac
  done
  case "$rest/" in
  */./* | */../*) return 1 ;;
  esac
  r=$(readlink -f -- "$p") || return 1
  [ -n "$r" ] || return 1
  [ "$r" != / ] || r=""
  r="$r$rest"
  [ -n "$r" ] || r=/
  printf '%s' "$r"
}

is_at_or_under() {
  [ "$2" = / ] && return 0
  [ "$1" = "$2" ] && return 0
  case $1 in
  "$2"/*) return 0 ;;
  *) return 1 ;;
  esac
}

# --- arguments --------------------------------------------------------------

ENV_DIR=""
SYSTEMD_DIR=""
BACKUP_ROOT=""
HOME_DIR=""
APPLY=0

while [ $# -gt 0 ]; do
  case $1 in
  --env-dir | --systemd-user-dir | --backup-root | --home)
    [ $# -ge 2 ] || arg_error "$1 needs a value"
    [ -n "$2" ] || arg_error "$1 must not be empty"
    ! has_cntrl "$2" || arg_error "$1 contains a control character"
    case $1 in
    --env-dir) ENV_DIR=$2 ;;
    --systemd-user-dir) SYSTEMD_DIR=$2 ;;
    --backup-root) BACKUP_ROOT=$2 ;;
    --home) HOME_DIR=$2 ;;
    esac
    shift 2
    ;;
  --apply)
    APPLY=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    arg_error "unknown argument (use --help)"
    ;;
  esac
done

[ -n "$ENV_DIR" ] || arg_error "--env-dir is required"

if ! STAMP=$(date -u +%Y%m%dT%H%M%SZ); then
  note "date failed"
  exit 2
fi
case $STAMP in
[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
*)
  note "unexpected UTC timestamp from date"
  exit 2
  ;;
esac
GENERATED_AT="${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2}T${STAMP:9:2}:${STAMP:11:2}:${STAMP:13:2}Z"
UTC_DATE="${STAMP:0:4}-${STAMP:4:2}-${STAMP:6:2}"

HOME_ENV=${HOME:-}
if [ -n "$HOME_ENV" ] && has_cntrl "$HOME_ENV"; then
  arg_error "HOME contains a control character"
fi
if [ -z "$HOME_DIR" ]; then
  [ -n "$HOME_ENV" ] || arg_error "--home not given and HOME is unset"
  HOME_DIR=$HOME_ENV
fi
if [ -z "$SYSTEMD_DIR" ]; then
  XDG=${XDG_CONFIG_HOME:-}
  case $XDG in
  /*)
    ! has_cntrl "$XDG" || arg_error "XDG_CONFIG_HOME contains a control character"
    SYSTEMD_DIR="$XDG/systemd/user"
    ;;
  *)
    [ -n "$HOME_ENV" ] || arg_error "--systemd-user-dir not given and HOME is unset"
    SYSTEMD_DIR="$HOME_ENV/.config/systemd/user"
    ;;
  esac
fi
if [ -z "$BACKUP_ROOT" ]; then
  [ -n "$HOME_ENV" ] || arg_error "--backup-root not given and HOME is unset"
  BACKUP_ROOT="$HOME_ENV/NWM-presync-backup-$UTC_DATE"
fi

ENV_DIR=$(strip_trailing_slashes "$ENV_DIR")
SYSTEMD_DIR=$(strip_trailing_slashes "$SYSTEMD_DIR")
BACKUP_ROOT=$(strip_trailing_slashes "$BACKUP_ROOT")
HOME_DIR=$(strip_trailing_slashes "$HOME_DIR")
if [ "$BACKUP_ROOT" = / ]; then
  BACKUP_DIR=/infra-env
else
  BACKUP_DIR="$BACKUP_ROOT/infra-env"
fi
MODE=dry_run
[ "$APPLY" = 0 ] || MODE=apply

# --- backup location (before anything is created) ---------------------------

if ! BACKUP_CANON=$(resolve_path "$BACKUP_DIR"); then
  note "refused: cannot resolve backup directory $BACKUP_DIR"
  exit 2
fi
for guarded in "$ENV_DIR" "$SYSTEMD_DIR"; do
  if ! GUARD_CANON=$(resolve_path "$guarded"); then
    note "refused: cannot resolve $guarded"
    exit 2
  fi
  if is_at_or_under "$BACKUP_CANON" "$GUARD_CANON"; then
    note "refused: backup directory $BACKUP_DIR resolves under $guarded"
    exit 2
  fi
done

# chmod would follow a link and change a directory this script did not create.
if [ -L "$BACKUP_DIR" ]; then
  note "refused: backup directory is a symlink: $BACKUP_DIR"
  exit 2
fi
if ! mkdir -p -- "$BACKUP_DIR" || ! chmod -- 700 "$BACKUP_DIR"; then
  note "cannot create backup directory $BACKUP_DIR"
  exit 2
fi
RECEIPT="$BACKUP_DIR/sweep-receipt-$STAMP.json"
if [ -e "$RECEIPT" ] || [ -L "$RECEIPT" ]; then
  note "refused: receipt already exists, not overwriting: $RECEIPT"
  exit 2
fi

# --- receipt ----------------------------------------------------------------

CANDIDATES=()
MOVED=()
REFUSED_NAMES=()
REFUSED_UNITS=()
CONFLICTS=()
ERRORS=()
REF_PATHS=()
REF_UNITS=()

json_str() {
  local s=$1
  if has_cntrl "$s"; then
    s="<name with control character>"
  fi
  s=${s//"$BS"/"$BS$BS"}
  s=${s//"$DQ"/"$BS$DQ"}
  printf '"%s"' "$s"
}

json_list() {
  local out="" item
  for item in "$@"; do
    [ -z "$out" ] || out="$out, "
    out="$out$(json_str "$item")"
  done
  printf '[%s]' "$out"
}

json_refusals() {
  local out="" i=0
  while [ "$i" -lt "${#REFUSED_NAMES[@]}" ]; do
    [ -z "$out" ] || out="$out, "
    out="$out{\"candidate\": $(json_str "${REFUSED_NAMES[$i]}"), \"unit\": $(json_str "${REFUSED_UNITS[$i]}")}"
    i=$((i + 1))
  done
  printf '[%s]' "$out"
}

write_receipt() {
  local code=$1 status=$2 doc
  doc="{
  \"schema_version\": $(json_str "$SCHEMA_VERSION"),
  \"generated_at\": $(json_str "$GENERATED_AT"),
  \"mode\": $(json_str "$MODE"),
  \"status\": $(json_str "$status"),
  \"exit_code\": $code,
  \"env_dir\": $(json_str "$ENV_DIR"),
  \"systemd_user_dir\": $(json_str "$SYSTEMD_DIR"),
  \"home\": $(json_str "$HOME_DIR"),
  \"backup_dir\": $(json_str "$BACKUP_DIR"),
  \"candidates\": $(json_list ${CANDIDATES[@]+"${CANDIDATES[@]}"}),
  \"moved\": $(json_list ${MOVED[@]+"${MOVED[@]}"}),
  \"refusals\": $(json_refusals),
  \"conflicts\": $(json_list ${CONFLICTS[@]+"${CONFLICTS[@]}"}),
  \"errors\": $(json_list ${ERRORS[@]+"${ERRORS[@]}"})
}"
  if [ -e "$RECEIPT" ] || [ -L "$RECEIPT" ]; then
    note "receipt appeared meanwhile, not overwriting: $RECEIPT"
    return 1
  fi
  (
    set -C
    printf '%s\n' "$doc" >"$RECEIPT"
  ) 2>/dev/null
}

finish() {
  local code=$1 status=$2
  if write_receipt "$code" "$status"; then
    note "receipt: $RECEIPT"
  else
    note "receipt write failed: $RECEIPT"
    code=2
  fi
  note "mode=$MODE status=$status exit=$code"
  exit "$code"
}

fail() {
  ERRORS+=("$1")
  note "$1"
  finish 2 error
}

# --- inputs -----------------------------------------------------------------

if [ ! -d "$ENV_DIR" ] || [ ! -r "$ENV_DIR" ] || [ ! -x "$ENV_DIR" ]; then
  fail "env dir missing or unreadable: $ENV_DIR"
fi
if [ ! -d "$SYSTEMD_DIR" ] || [ ! -r "$SYSTEMD_DIR" ] || [ ! -x "$SYSTEMD_DIR" ]; then
  fail "systemd user dir missing or unreadable: $SYSTEMD_DIR"
fi

UNIT_COUNT=0

# Collects the EnvironmentFile= values of one unit file into REF_PATHS/REF_UNITS.
# A value that is not a literal path or `%h` fails the run (see the header).
scan_unit() {
  local file=$1 rel=$2 line value rest
  [ -r "$file" ] || return 1
  UNIT_COUNT=$((UNIT_COUNT + 1))
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    line=${line#"${line%%[![:space:]]*}"}
    case $line in
    EnvironmentFile*) ;;
    *) continue ;;
    esac
    value=${line#EnvironmentFile}
    value=${value#"${value%%[![:space:]]*}"}
    case $value in
    =*) value=${value#=} ;;
    *) continue ;;
    esac
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    [ -n "$value" ] || continue
    value=${value#-}
    # Checked on the trimmed value so trailing blanks after the `\` count too.
    case $value in
    *"$BS")
      fail "unparseable EnvironmentFile= value in $rel (line continues with a backslash): $value"
      ;;
    esac
    case $value in
    *\** | *\?* | *\[*)
      fail "unparseable EnvironmentFile= value in $rel (wildcard): $value"
      ;;
    esac
    rest=${value//"$H_SPEC"/}
    case $rest in
    *%*)
      fail "unparseable EnvironmentFile= value in $rel (specifier other than %h): $value"
      ;;
    esac
    value=${value//"$H_SPEC"/"$HOME_DIR"}
    REF_PATHS+=("$(normalize "$value")")
    REF_UNITS+=("$rel")
  done <"$file"
}

for unit in "$SYSTEMD_DIR"/nhms-*; do
  [ -f "$unit" ] || continue
  if ! scan_unit "$unit" "${unit##*/}"; then
    fail "unit file unreadable: ${unit##*/}"
  fi
done
for dropin_dir in "$SYSTEMD_DIR"/nhms-*.service.d; do
  [ -d "$dropin_dir" ] || continue
  for dropin in "$dropin_dir"/*.conf; do
    [ -f "$dropin" ] || continue
    if ! scan_unit "$dropin" "${dropin_dir##*/}/${dropin##*/}"; then
      fail "drop-in unreadable: ${dropin_dir##*/}/${dropin##*/}"
    fi
  done
done
if [ "$UNIT_COUNT" -eq 0 ]; then
  fail "no nhms-* unit files under $SYSTEMD_DIR; refusing to sweep with an empty refusal set"
fi

BAD_NAMES=0
for path in "$ENV_DIR"/*; do
  name=${path##*/}
  case $name in
  *.bak* | *.backup-* | *.pre-*) ;;
  *) continue ;;
  esac
  { [ -f "$path" ] && [ ! -L "$path" ]; } || continue
  if has_cntrl "$name"; then
    BAD_NAMES=$((BAD_NAMES + 1))
    continue
  fi
  CANDIDATES+=("$name")
done
if [ "$BAD_NAMES" -gt 0 ]; then
  fail "$BAD_NAMES candidate basename(s) contain a control character; rename them by hand first"
fi

# --- refusals ---------------------------------------------------------------

for name in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  candidate_canon=$(normalize "$ENV_DIR/$name")
  i=0
  while [ "$i" -lt "${#REF_PATHS[@]}" ]; do
    if [ "${REF_PATHS[$i]}" = "$candidate_canon" ]; then
      REFUSED_NAMES+=("$name")
      REFUSED_UNITS+=("${REF_UNITS[$i]}")
      note "refused: $name is an EnvironmentFile= of ${REF_UNITS[$i]}"
    fi
    i=$((i + 1))
  done
done
if [ "${#REFUSED_NAMES[@]}" -gt 0 ]; then
  finish 2 refused
fi

# --- plan, conflicts, moves -------------------------------------------------

for name in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  printf 'plan: %s/%s -> %s/%s\n' "$ENV_DIR" "$name" "$BACKUP_DIR" "$name"
done
for name in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  if [ -e "$BACKUP_DIR/$name" ] || [ -L "$BACKUP_DIR/$name" ]; then
    CONFLICTS+=("$name")
    note "conflict: target already exists: $BACKUP_DIR/$name"
  fi
done
if [ "${#CONFLICTS[@]}" -gt 0 ]; then
  finish 2 conflict
fi

if [ "$APPLY" = 1 ]; then
  for name in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
    src="$ENV_DIR/$name"
    dst="$BACKUP_DIR/$name"
    if ! mv -n -- "$src" "$dst"; then
      fail "mv failed: $name"
    fi
    if [ -e "$src" ] || [ -L "$src" ] || [ ! -e "$dst" ]; then
      fail "move not confirmed (source still present or target missing): $name"
    fi
    MOVED+=("$name")
    note "moved: $name"
  done
fi

finish 0 ok
