#!/usr/bin/env bash
# display-c1-receipt.sh — the node-27 display deployment receipt (C1), read from
# the running user unit nhms-display-api.service (#3231).
#
# Read-only: it starts, stops and restarts nothing, writes no file (no temp file,
# no here-document, no git index refresh), does not source infra/env/display.env
# and prints no value of that file except OBJECT_STORE_ROOT and
# NHMS_PUBLISHED_ARTIFACT_ROOT. It does not call docker.
#
# Usage: bash scripts/ops/display-c1-receipt.sh [<repo-root>]
#   <repo-root> defaults to the checkout this script lives in (scripts/ops -> ../..).
#   Run it from a login shell of the service user: the Slurm client scan uses the
#   invoking shell's PATH, as the unit's `bash -lc` does.
#
# Output: the observed inputs, one `<claim>=ok` or `<claim>=FAIL <reason>` line per
# claim (every claim is evaluated, also after a failure; an input that cannot be
# read is FAIL, never skipped), then `C1=PASS` (exit 0) or `C1=FAIL` (exit 1).
#
# Portable to bash 3.2 and BSD userland (no `date -d`, no `stat -c`, no /proc).
# Deliberately no `set -e`: the verdict line must always be printed.

set -u

UNIT=nhms-display-api.service
CURL_MAX_TIME=10

if [ "$#" -ge 1 ]; then
    root_arg=$1
else
    # `CDPATH=`: with CDPATH exported, `cd` to a relative dirname prints the target.
    root_arg=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." 2>/dev/null && pwd)
fi
REPO_ROOT=$(CDPATH='' cd -- "$root_arg" 2>/dev/null && pwd)
[ -n "$REPO_ROOT" ] || REPO_ROOT=$root_arg
ENV_FILE="$REPO_ROOT/infra/env/display.env"

# The observation clock is read once, before the process's elapsed time, so the
# derived start time can only be early, never late (a tick between the two reads
# makes the staleness check stricter, not looser).
now=$(date +%s 2>/dev/null)
case "$now" in '' | *[!0-9]*) now= ;; esac

is_uint() {
    case "$1" in '' | *[!0-9]*) return 1 ;; esac
    return 0
}

strip_spaces() {
    printf '%s' "$1" | tr -d ' \t\r\n'
}

# -- checkout --------------------------------------------------------------------
checkout_head=$(git -C "$REPO_ROOT" rev-parse --verify --quiet HEAD 2>/dev/null)

# -- unit and main process ---------------------------------------------------------
unit_state=$(systemctl --user is-active "$UNIT" 2>/dev/null)
main_pid=$(strip_spaces "$(systemctl --user show "$UNIT" -p MainPID --value 2>/dev/null)")
is_uint "$main_pid" || main_pid=
[ "$main_pid" = 0 ] && main_pid=

have_main=0
no_main_reason=
if [ "$unit_state" != active ]; then
    no_main_reason="unit not active (${unit_state:-unreadable})"
elif [ -z "$main_pid" ]; then
    no_main_reason="unit has no main process"
else
    have_main=1
fi
# A pid of a unit that is not active is nobody's main process: not reported, not read.
[ "$have_main" = 1 ] || main_pid=

main_args=
etimes=
port=
if [ "$have_main" = 1 ]; then
    main_args=$(ps -o args= -p "$main_pid" 2>/dev/null)
    etimes=$(strip_spaces "$(ps -o etimes= -p "$main_pid" 2>/dev/null)")
    is_uint "$etimes" || etimes=
    # The port is the one on the observed process's command line (D4), parsed
    # whatever the command is, so a foreign command still names the port it holds.
    set -f
    want_port=0
    # shellcheck disable=SC2086
    for word in $main_args; do
        if [ "$want_port" = 1 ]; then
            port=$word
            want_port=0
            continue
        fi
        case "$word" in
            --port) want_port=1 ;;
            --port=*) port=${word#--port=} ;;
        esac
    done
    set +f
    if ! is_uint "$port" || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        port=
    fi
fi

process_started=
if [ -n "$now" ] && [ -n "$etimes" ]; then
    process_started=$((now - etimes))
fi

# -- HEAD move and env file time -------------------------------------------------------
head_moved=$(git -C "$REPO_ROOT" log -g -1 --date=unix --format=%gd HEAD 2>/dev/null)
head_moved=${head_moved#HEAD@\{}
head_moved=${head_moved%\}}
is_uint "$head_moved" || head_moved=

env_modified=
if [ -f "$ENV_FILE" ]; then
    env_modified=$(strip_spaces "$(date -r "$ENV_FILE" +%s 2>/dev/null)")
    is_uint "$env_modified" || env_modified=
fi

# -- the two storage roots of display.env (matched by key, never sourced) --------------
object_store_root=
published_artifact_root=
have_object_store_root=0
have_published_artifact_root=0
env_readable=0

strip_quotes() {
    case "$1" in
        \"*\") value=${1#\"}; value=${value%\"} ;;
        \'*\') value=${1#\'}; value=${value%\'} ;;
        *) value=$1 ;;
    esac
    printf '%s' "$value"
}

if [ -f "$ENV_FILE" ] && [ -r "$ENV_FILE" ]; then
    env_readable=1
    while IFS= read -r line || [ -n "$line" ]; do
        # Leading blanks and an `export ` prefix are not part of the key.
        while :; do
            case "$line" in
                ' '* | '	'*) line=${line#?} ;;
                *) break ;;
            esac
        done
        case "$line" in export\ *) line=${line#export } ;; esac
        case "$line" in
            OBJECT_STORE_ROOT=*)
                object_store_root=$(strip_quotes "${line#OBJECT_STORE_ROOT=}")
                have_object_store_root=1
                ;;
            NHMS_PUBLISHED_ARTIFACT_ROOT=*)
                published_artifact_root=$(strip_quotes "${line#NHMS_PUBLISHED_ARTIFACT_ROOT=}")
                have_published_artifact_root=1
                ;;
        esac
    done <"$ENV_FILE" 2>/dev/null
fi

# -- claims ------------------------------------------------------------------------
failures=0
claim_lines=
add_claim() {
    # add_claim <name> <reason>; an empty reason means ok.
    if [ -n "$2" ]; then
        failures=$((failures + 1))
        claim_lines="$claim_lines$1=FAIL $2
"
    else
        claim_lines="$claim_lines$1=ok
"
    fi
}

# unit_active
add_claim unit_active "$no_main_reason"

# process_of_checkout
reason=
if [ "$have_main" != 1 ]; then
    reason=$no_main_reason
elif [ -z "$main_args" ]; then
    reason="main process command line unreadable"
else
    case "$main_args" in
        "$REPO_ROOT/.venv/bin/python -m uvicorn apps.api.main:app "*)
            [ -n "$port" ] || reason="main process command line has no valid --port"
            ;;
        *) reason="main process is not this checkout's uvicorn apps.api.main:app" ;;
    esac
fi
add_claim process_of_checkout "$reason"

# checkout_clean (GIT_OPTIONAL_LOCKS=0: status must not refresh .git/index)
reason=
status_out=$(GIT_OPTIONAL_LOCKS=0 git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null)
status_rc=$?
if [ "$status_rc" != 0 ]; then
    reason="git status failed (rc=$status_rc)"
elif [ -n "$status_out" ]; then
    reason="tracked files modified, staged or deleted"
fi
add_claim checkout_clean "$reason"

# process_loaded_head (strictly later than the HEAD move and the env file change)
reason=
if [ "$have_main" != 1 ]; then
    reason=$no_main_reason
elif [ -z "$process_started" ]; then
    reason="main process start time unreadable"
elif [ -z "$head_moved" ]; then
    reason="checkout reflog unreadable"
elif [ -z "$env_modified" ]; then
    reason="env file modification time unreadable"
elif [ "$process_started" -le "$head_moved" ]; then
    reason="main process started at or before the checkout's HEAD last moved"
elif [ "$process_started" -le "$env_modified" ]; then
    reason="main process started at or before infra/env/display.env was last modified"
fi
add_claim process_loaded_head "$reason"

# listener_of_unit
reason=
if [ "$have_main" != 1 ]; then
    reason=$no_main_reason
elif [ -z "$port" ]; then
    reason="no port on the main process command line"
else
    ss_out=$(ss -Hltnp "sport = :$port" 2>/dev/null)
    ss_rc=$?
    if [ "$ss_rc" != 0 ]; then
        reason="listener query failed (rc=$ss_rc)"
    else
        listening_lines=0
        # A newline-split `for` instead of a here-document: bash 3.2 backs
        # here-documents with a temporary file, and this script writes nothing.
        saved_ifs=$IFS
        IFS='
'
        set -f
        for ss_line in $ss_out; do
            [ -n "$(strip_spaces "$ss_line")" ] || continue
            listening_lines=$((listening_lines + 1))
            pids=$(printf '%s\n' "$ss_line" | grep -o 'pid=[0-9][0-9]*')
            if [ -z "$pids" ]; then
                [ -n "$reason" ] || reason="a listener on port $port names no process"
                continue
            fi
            for pid_word in $pids; do
                pid=${pid_word#pid=}
                [ "$pid" = "$main_pid" ] && continue
                ppid=$(strip_spaces "$(ps -o ppid= -p "$pid" 2>/dev/null)")
                if [ "$ppid" != "$main_pid" ]; then
                    [ -n "$reason" ] || reason="pid $pid on port $port is neither the main process nor its child"
                fi
            done
        done
        set +f
        IFS=$saved_ifs
        if [ -z "$reason" ] && [ "$listening_lines" = 0 ]; then
            reason="nothing listens on port $port"
        fi
    fi
fi
add_claim listener_of_unit "$reason"

# HTTP: GET only, loopback, bounded; no request without the main process and its port.
http_code=
http_body=
http_get() {
    http_code=
    http_body=
    local response
    response=$(curl -q -sS --noproxy '*' --max-time "$CURL_MAX_TIME" -w '\n%{http_code}' \
        "http://127.0.0.1:$port$1" 2>/dev/null)
    local rc=$?
    if [ "$rc" != 0 ]; then
        http_code=
        return 1
    fi
    http_code=${response##*$'\n'}
    http_body=${response%$'\n'*}
    is_uint "$http_code" || http_code=
    [ -n "$http_code" ]
}

probe_reason() {
    if [ "$have_main" != 1 ]; then
        printf '%s' "$no_main_reason"
    elif [ -z "$port" ]; then
        printf '%s' "no port on the main process command line"
    fi
}

# health
reason=$(probe_reason)
if [ -z "$reason" ]; then
    if ! http_get /health; then
        reason="GET /health got no answer"
    elif [ "$http_code" != 200 ]; then
        reason="GET /health answered $http_code"
    fi
fi
add_claim health "$reason"

# runtime_role (fixed strings of the compact JSON the API answers)
reason=$(probe_reason)
if [ -z "$reason" ]; then
    if ! http_get /api/v1/runtime/config; then
        reason="GET /api/v1/runtime/config got no answer"
    elif [ "$http_code" != 200 ]; then
        reason="GET /api/v1/runtime/config answered $http_code"
    else
        case "$http_body" in
            *'"service_role":"display_readonly"'[,'}']*) ;;
            *) reason="service_role is not display_readonly" ;;
        esac
        case "$http_body" in
            *'"display_readonly":true'[,'}']*) ;;
            *) [ -n "$reason" ] || reason="display_readonly is not true" ;;
        esac
        case "$http_body" in
            *'"slurm_routes_enabled":false'[,'}']*) ;;
            *) [ -n "$reason" ] || reason="slurm_routes_enabled is not false" ;;
        esac
        case "$http_body" in
            *'"control_mutations_enabled":false'[,'}']*) ;;
            *) [ -n "$reason" ] || reason="control_mutations_enabled is not false" ;;
        esac
    fi
fi
add_claim runtime_role "$reason"

# slurm_routes_absent
reason=$(probe_reason)
if [ -z "$reason" ]; then
    if ! http_get /api/v1/slurm/health; then
        reason="GET /api/v1/slurm/health got no answer"
    elif [ "$http_code" != 404 ]; then
        reason="GET /api/v1/slurm/health answered $http_code"
    fi
fi
add_claim slurm_routes_absent "$reason"

# no_slurm_client (the invoking shell's PATH; fixed host paths)
found=
for command_name in sbatch scancel squeue scontrol sacct srun sinfo; do
    if command -v "$command_name" >/dev/null 2>&1; then
        found="$found $command_name"
    fi
done
[ -e /etc/slurm ] && found="$found /etc/slurm"
[ -e /etc/slurm-llnl ] && found="$found /etc/slurm-llnl"
[ -e /run/munge/munge.socket.2 ] && found="$found /run/munge/munge.socket.2"
reason=
[ -z "$found" ] || reason="present:$found"
add_claim no_slurm_client "$reason"

# storage_not_writable (the two root directories themselves)
storage_reason() {
    # storage_reason <key> <present 0|1> <value>
    if [ "$2" != 1 ]; then
        printf '%s' "$1 missing"
    elif [ -z "$3" ]; then
        printf '%s' "$1 empty"
    else
        case "$3" in
            /*) ;;
            *) printf '%s' "$1 is not an absolute path"; return ;;
        esac
        if [ ! -d "$3" ]; then
            printf '%s' "$1 is not an existing directory"
        elif [ -w "$3" ]; then
            printf '%s' "$1 is writable"
        fi
    fi
}
reason=
if [ "$env_readable" != 1 ]; then
    reason="infra/env/display.env unreadable"
else
    reason=$(storage_reason OBJECT_STORE_ROOT "$have_object_store_root" "$object_store_root")
    [ -n "$reason" ] || reason=$(storage_reason NHMS_PUBLISHED_ARTIFACT_ROOT \
        "$have_published_artifact_root" "$published_artifact_root")
fi
add_claim storage_not_writable "$reason"

# -- receipt -------------------------------------------------------------------------
printf 'repo_root=%s\n' "$REPO_ROOT"
printf 'checkout_head=%s\n' "$checkout_head"
printf 'main_pid=%s\n' "$main_pid"
printf 'port=%s\n' "$port"
printf 'process_started=%s\n' "$process_started"
printf 'head_moved=%s\n' "$head_moved"
printf 'env_modified=%s\n' "$env_modified"
printf 'object_store_root=%s\n' "$object_store_root"
printf 'published_artifact_root=%s\n' "$published_artifact_root"
printf '%s' "$claim_lines"
if [ "$failures" = 0 ]; then
    printf 'C1=PASS\n'
    exit 0
fi
printf 'C1=FAIL\n'
exit 1
