#!/usr/bin/env bash
# Shell-level tests for large-file-guard.sh. Creates throwaway git repos in a
# temp dir and feeds the hook the same JSON shape Claude Code sends. Run:
#   bash .claude/hooks/large-file-guard/test-large-file-guard.sh
set -euo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/large-file-guard.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail=0
# In-process assertion counter: every PASS branch increments it, so the final
# summary is exactly what THIS run executed (no process substitution / tee race).
passes=0

# run_hook <repo> <command>; echoes the hook's exit code
run_hook() {
    local repo=$1 cmd=$2 rc=0
    printf '{"tool_input":{"command":"%s"},"cwd":"%s"}' "$cmd" "$repo" \
        | CLAUDE_PROJECT_DIR="$repo" bash "$HOOK" >/dev/null 2>&1 || rc=$?
    echo "$rc"
}

check() {
    local name=$1 expected=$2 actual=$3
    if [ "$actual" = "$expected" ]; then
        echo "PASS: $name"
        passes=$((passes + 1))
    else
        echo "FAIL: $name (expected exit $expected, got $actual)"
        fail=1
    fi
}

stderr_file="$TMP/stderr.txt"

# JSON-encode <cwd> so control characters (CR/tab) inside real path bytes are
# valid JSON escapes instead of raw control bytes in the tool-call document.
json_cwd() { # <path>
    python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

# run_hook_split <project_dir> <cwd> <command>; echoes exit code, keeps stderr
run_hook_split() {
    local project_dir=$1 cwd=$2 cmd=$3 rc=0
    printf '{"tool_input":{"command":"%s"},"cwd":%s}' "$cmd" "$(json_cwd "$cwd")" \
        | CLAUDE_PROJECT_DIR="$project_dir" bash "$HOOK" >/dev/null 2>"$stderr_file" \
        || rc=$?
    echo "$rc"
}

# Raw-byte contiguous substring assertion: the needle may carry CR/LF path
# bytes, so it is fsencoded and searched as one byte sequence in the captured
# stderr bytes.  grep -qF would split an LF-bearing needle into patterns and
# prefix-match, silently passing a stripped diagnostic.
check_stderr_contains() {
    local name=$1 needle=$2
    if python3 - "$needle" "$stderr_file" <<'PY'
import os, sys
needle = os.fsencode(sys.argv[1])
stderr = open(sys.argv[2], "rb").read()
sys.exit(0 if needle in stderr else 1)
PY
    then
        echo "PASS: $name"
        passes=$((passes + 1))
    else
        echo "FAIL: $name (stderr lacks exact bytes: $(printf '%q' "$needle"))"
        fail=1
    fi
}

big_file() { # <path> <lines>
    seq "$2" | sed 's/^/line /' > "$1"
}

new_repo() {
    local repo=$1
    git init -q -b main "$repo"
    git -C "$repo" config user.email t@t && git -C "$repo" config user.name t
    echo '{"maxLines": 10}' > "$repo/.large-file-guard.json"
    echo base > "$repo/conflict.txt"
    git -C "$repo" add -A && git -C "$repo" commit -qm base
}

# --- 1. plain commit with a staged large file is still blocked -------------
repo="$TMP/plain"
new_repo "$repo"
big_file "$repo/big.txt" 20
git -C "$repo" add big.txt
check "plain commit blocks large file" 2 "$(run_hook "$repo" "git commit -m x")"

# --- 2. plain commit with only small files passes --------------------------
repo="$TMP/small"
new_repo "$repo"
echo tiny > "$repo/small.txt"
git -C "$repo" add small.txt
check "plain commit passes small file" 0 "$(run_hook "$repo" "git commit -m x")"

# Shared merge fixture: feature branch adds big.txt (upstream-side content)
# and edits conflict.txt; main edits conflict.txt differently -> conflict.
merge_repo() {
    local repo=$1
    new_repo "$repo"
    git -C "$repo" checkout -qb feature
    big_file "$repo/big.txt" 20
    echo feature > "$repo/conflict.txt"
    git -C "$repo" add -A && git -C "$repo" commit -qm feature
    git -C "$repo" checkout -q main
    echo main > "$repo/conflict.txt"
    git -C "$repo" add -A && git -C "$repo" commit -qm main
    git -C "$repo" merge feature >/dev/null 2>&1 || true  # conflict expected
    printf 'resolved line 1\nresolved line 2\n' > "$repo/conflict.txt"
    git -C "$repo" add conflict.txt
}

# --- 3. concluding a merge must NOT flag files merged from the other side --
repo="$TMP/merge-clean"
merge_repo "$repo"
check "merge conclusion ignores other-side large file" 0 \
    "$(run_hook "$repo" "git commit --no-edit")"
check "git merge --continue same" 0 \
    "$(run_hook "$repo" "git merge --continue")"

# --- 4. genuinely new large content during a merge is still blocked --------
repo="$TMP/merge-dirty"
merge_repo "$repo"
big_file "$repo/newbig.txt" 20
git -C "$repo" add newbig.txt
check "merge conclusion blocks newly authored large file" 2 \
    "$(run_hook "$repo" "git commit --no-edit")"
check "git merge --continue blocks it too" 2 \
    "$(run_hook "$repo" "git merge --continue")"

# --- 5. linked worktree: the JSON cwd's worktree, not CLAUDE_PROJECT_DIR ----
# CLAUDE_PROJECT_DIR points at the main checkout while the tool call runs in a
# nested directory of a linked worktree; config must come from the worktree's
# Git top level.  The config lives at the worktree top, not at the nested cwd.
main="$TMP/worktree-main"
new_repo "$main"
git -C "$main" worktree add -q -b wt "$TMP/worktree-wt"
wt="$TMP/worktree-wt"
wt_top="$(git -C "$wt" rev-parse --show-toplevel)"
mkdir -p "$wt/nested/deep"

# 5a. an exclusion that exists only in the worktree config must pass there.
echo '{"maxLines": 10, "exclude": ["big.txt"]}' > "$wt_top/.large-file-guard.json"
big_file "$wt/big.txt" 20
git -C "$wt" add big.txt
check "worktree-only exclusion passes from nested cwd" 0 \
    "$(run_hook_split "$main" "$wt/nested/deep" "git commit -m x")"

# 5b. a block must be decided by the worktree config (main's disables the
# guard entirely) and name the exact effective config path on stderr.
echo '{"enabled": false}' > "$main/.large-file-guard.json"
echo '{"maxLines": 10}' > "$wt_top/.large-file-guard.json"
check "worktree config blocks despite disabled main config" 2 \
    "$(run_hook_split "$main" "$wt/nested/deep" "git commit -m x")"
check_stderr_contains "block stderr names the worktree config path" \
    "$wt_top/.large-file-guard.json"

# --- 6. legacy callers: no JSON cwd falls back to CLAUDE_PROJECT_DIR --------
repo="$TMP/legacy"
new_repo "$repo"
big_file "$repo/big.txt" 20
git -C "$repo" add big.txt
rc=0
printf '{"tool_input":{"command":"git commit -m x"}}' \
    | CLAUDE_PROJECT_DIR="$repo" bash "$HOOK" >/dev/null 2>&1 || rc=$?
check "absent cwd falls back to CLAUDE_PROJECT_DIR" 2 "$rc"

# --- 7. non-Git JSON cwd falls back to CLAUDE_PROJECT_DIR -------------------
# The probe fails, so config/Git/file reads and diagnostics must all use the
# fallback root -- proven both by a block (with its exact path) and a pass.
repo="$TMP/nongit-fallback"
new_repo "$repo"
big_file "$repo/big.txt" 20
git -C "$repo" add big.txt
nongit="$TMP/not-a-repo"
mkdir -p "$nongit"
check "non-Git cwd blocks via fallback config" 2 \
    "$(run_hook_split "$repo" "$nongit" "git commit -m x")"
check_stderr_contains "non-Git block stderr names the fallback config path" \
    "$repo/.large-file-guard.json"
git -C "$repo" reset -q
echo tiny > "$repo/small.txt"
git -C "$repo" add small.txt
check "non-Git cwd passes small file via fallback" 0 \
    "$(run_hook_split "$repo" "$nongit" "git commit -m x")"

# --- 8. trailing-space linked worktree keeps exact path bytes ---------------
# A linked worktree whose top-level dir ends in an ASCII space is legal; the
# space is path data and only the protocol newline may be removed.  The
# truncated (space-stripped) form would resolve to the sibling main checkout.
sp_main="$TMP/sp-main"
new_repo "$sp_main"
git -C "$sp_main" worktree add -q -b spwt "$TMP/sp-main "
sp_wt="$TMP/sp-main "
sp_top="$(git -C "$sp_wt" rev-parse --show-toplevel)"
case "$sp_top" in
    *" ") : ;;
    *) echo "FAIL: setup did not produce a trailing-space worktree top ($sp_top)"; fail=1 ;;
esac
mkdir -p "$sp_wt/nested/deep"
echo '{"maxLines": 10, "exclude": ["big.txt"]}' > "$sp_top/.large-file-guard.json"
echo '{"enabled": false}' > "$sp_main/.large-file-guard.json"
big_file "$sp_wt/big.txt" 20
git -C "$sp_wt" add big.txt
check "trailing-space worktree exclusion passes from nested cwd" 0 \
    "$(run_hook_split "$sp_main" "$sp_wt/nested/deep" "git commit -m x")"
echo '{"maxLines": 10}' > "$sp_top/.large-file-guard.json"
check "trailing-space worktree blocks non-excluded oversized file" 2 \
    "$(run_hook_split "$sp_main" "$sp_wt/nested/deep" "git commit -m x")"
check_stderr_contains "trailing-space block stderr names exact config path" \
    "$sp_top/.large-file-guard.json"

# --- 9. linked worktree commit -a reads tracked files at the worktree root --
# Split root like the other nested-cwd cases: fallback is the main checkout,
# active cwd is the linked worktree's nested dir.
main="$TMP/commit-a-main"
new_repo "$main"
git -C "$main" worktree add -q -b cawt "$TMP/commit-a-wt"
ca_wt="$TMP/commit-a-wt"
ca_top="$(git -C "$ca_wt" rev-parse --show-toplevel)"
mkdir -p "$ca_wt/nested/deep"
echo seed > "$ca_wt/tracked.txt"
git -C "$ca_wt" add tracked.txt
git -C "$ca_wt" commit -qm seed
echo '{"maxLines": 10}' > "$ca_top/.large-file-guard.json"
big_file "$ca_wt/tracked.txt" 20
check "linked worktree commit -a blocks unstaged oversized tracked file" 2 \
    "$(run_hook_split "$main" "$ca_wt/nested/deep" "git commit -a -m x")"

# --- 10. linked worktree merges: inherited pass, authored block -------------
# Same shape as the plain merge fixtures, but staged inside the linked
# worktree; exercises MERGE_HEAD resolution at the worktree's own admin
# directory.  The same-root pair (JSON cwd == fallback root) catches rejection
# of an absolute MERGE_HEAD path outside the worktree top; the split-root pair
# below uses a distinct main fallback with a nested linked cwd.
mk_link_merge() {
    local main=$1 wt=$2
    new_repo "$main"
    git -C "$main" worktree add -q -b lm "$wt"
    git -C "$wt" checkout -qb feature
    big_file "$wt/big.txt" 20
    echo feature > "$wt/conflict.txt"
    git -C "$wt" add -A && git -C "$wt" commit -qm feature
    git -C "$wt" checkout -q main 2>/dev/null || git -C "$wt" checkout -q lm
    echo main > "$wt/conflict.txt"
    git -C "$wt" add -A && git -C "$wt" commit -qm main
    git -C "$wt" merge feature >/dev/null 2>&1 || true
    printf 'resolved line 1\nresolved line 2\n' > "$wt/conflict.txt"
    git -C "$wt" add conflict.txt
}
lm_main="$TMP/link-merge-clean"
mk_link_merge "$lm_main" "$TMP/link-merge-clean-wt"
check "linked merge conclusion ignores other-side large file" 0 \
    "$(run_hook "$TMP/link-merge-clean-wt" "git commit --no-edit")"
check "linked git merge --continue same" 0 \
    "$(run_hook "$TMP/link-merge-clean-wt" "git merge --continue")"

lm_main="$TMP/link-merge-dirty"
mk_link_merge "$lm_main" "$TMP/link-merge-dirty-wt"
big_file "$TMP/link-merge-dirty-wt/newbig.txt" 20
git -C "$TMP/link-merge-dirty-wt" add newbig.txt
check "linked merge conclusion blocks newly authored large file" 2 \
    "$(run_hook "$TMP/link-merge-dirty-wt" "git commit --no-edit")"
check "linked git merge --continue blocks it too" 2 \
    "$(run_hook "$TMP/link-merge-dirty-wt" "git merge --continue")"

# Split root: CLAUDE_PROJECT_DIR is the distinct main checkout while the tool
# call runs in a nested dir of the linked worktree mid-merge.  Fallback and
# active worktree states/config are intentionally distinguishable.
sp_main="$TMP/split-merge-main"
mk_link_merge "$sp_main" "$TMP/split-merge-wt"
mkdir -p "$TMP/split-merge-wt/nested/deep"
echo '{"enabled": false}' > "$sp_main/.large-file-guard.json"
check "split-root linked merge ignores other-side large file" 0 \
    "$(run_hook_split "$sp_main" "$TMP/split-merge-wt/nested/deep" "git commit --no-edit")"
check "split-root linked git merge --continue same" 0 \
    "$(run_hook_split "$sp_main" "$TMP/split-merge-wt/nested/deep" "git merge --continue")"

sp_main="$TMP/split-merge-dirty-main"
mk_link_merge "$sp_main" "$TMP/split-merge-dirty-wt"
mkdir -p "$TMP/split-merge-dirty-wt/nested/deep"
big_file "$TMP/split-merge-dirty-wt/newbig.txt" 20
git -C "$TMP/split-merge-dirty-wt" add newbig.txt
check "split-root linked merge blocks newly authored large file" 2 \
    "$(run_hook_split "$sp_main" "$TMP/split-merge-dirty-wt/nested/deep" "git commit --no-edit")"
check "split-root linked git merge --continue blocks it too" 2 \
    "$(run_hook_split "$sp_main" "$TMP/split-merge-dirty-wt/nested/deep" "git merge --continue")"

# --- 11. CR-bearing linked worktree keeps exact path bytes ----------------
# A worktree whose top-level dir contains a carriage return (legal on POSIX
# and APFS) must survive Git's protocol byte transport: CR is path data, only
# the trailing LF is protocol.  text=True would translate CR to LF and assign
# a wrong root, failing open on oversized files and false-blocking merges.
cr_main="$TMP/cr-main"
new_repo "$cr_main"
cr_wt="$TMP/$(printf 'cr\x0dwt')"
git -C "$cr_main" worktree add -q -b crwt "$cr_wt"
cr_top="$(git -C "$cr_wt" rev-parse --show-toplevel)"
case "$cr_top" in
    *$'\r'*) : ;;
    *) echo "FAIL: setup did not produce a CR-bearing worktree top ($cr_top)"; fail=1 ;;
esac
mkdir -p "$cr_wt/nested/deep"
echo '{"maxLines": 10, "exclude": ["big.txt"]}' > "$cr_top/.large-file-guard.json"
echo '{"enabled": false}' > "$cr_main/.large-file-guard.json"
big_file "$cr_wt/big.txt" 20
git -C "$cr_wt" add big.txt
check "CR worktree exclusion passes from nested cwd" 0 \
    "$(run_hook_split "$cr_main" "$cr_wt/nested/deep" "git commit -m x")"
echo '{"maxLines": 10}' > "$cr_top/.large-file-guard.json"
check "CR worktree blocks non-excluded oversized file" 2 \
    "$(run_hook_split "$cr_main" "$cr_wt/nested/deep" "git commit -m x")"
check_stderr_contains "CR block stderr names exact config path" \
    "$cr_top/.large-file-guard.json"

# CR-bearing main repo root + ordinary linked worktree mid-merge: the CR
# lands inside the absolute --git-path MERGE_HEAD path; it must survive.
cr_merge_main="$TMP/$(printf 'cr\x0dmerge-main')"
new_repo "$cr_merge_main"
git -C "$cr_merge_main" worktree add -q -b crmerge "$TMP/cr-merge-wt"
cr_mwt="$TMP/cr-merge-wt"
git -C "$cr_mwt" checkout -qb feature
big_file "$cr_mwt/big.txt" 20
echo feature > "$cr_mwt/conflict.txt"
git -C "$cr_mwt" add -A && git -C "$cr_mwt" commit -qm feature
git -C "$cr_mwt" checkout -q main 2>/dev/null || git -C "$cr_mwt" checkout -q crmerge
echo main > "$cr_mwt/conflict.txt"
git -C "$cr_mwt" add -A && git -C "$cr_mwt" commit -qm main
git -C "$cr_mwt" merge feature >/dev/null 2>&1 || true
printf 'resolved line 1\nresolved line 2\n' > "$cr_mwt/conflict.txt"
git -C "$cr_mwt" add conflict.txt
mkdir -p "$cr_mwt/nested/deep"
check "CR main merge ignores other-side large file" 0 \
    "$(run_hook_split "$cr_merge_main" "$cr_mwt/nested/deep" "git commit --no-edit")"
check "CR main git merge --continue same" 0 \
    "$(run_hook_split "$cr_merge_main" "$cr_mwt/nested/deep" "git merge --continue")"

# --- 12. fallback merges from an unrelated process cwd ----------------------
# MERGE_HEAD must be queried at the resolved active/fallback root, not the raw
# JSON cwd: with an absent or non-Git cwd the merge metadata lives at the
# fallback repo, and querying the raw cwd would see no MERGE_HEAD, skip the
# parent intersection and false-block every inherited other-side file.  The
# shell runs from an unrelated process cwd so an absent-cwd defect cannot be
# masked by a coincidentally-right process directory.
process_cwd="$TMP/unrelated-process-cwd"
mkdir -p "$process_cwd"
# Runner from <process_cwd> with CLAUDE_PROJECT_DIR=fallback repo.
run_hook_from() { # <process_cwd> <project_dir> <cwd_json> <command>
    local pcwd=$1 project_dir=$2 cwd_json=$3 cmd=$4 rc=0
    ( cd "$pcwd" && printf '{"tool_input":{"command":"%s"},"cwd":%s}' "$cmd" "$cwd_json" \
        | CLAUDE_PROJECT_DIR="$project_dir" bash "$HOOK" >/dev/null 2>"$stderr_file" ) \
        || rc=$?
    echo "$rc"
}
absent_json='""'

fb_main="$TMP/fb-merge-clean"
mk_link_merge "$fb_main" "$TMP/fb-merge-wt"
check "absent-cwd fallback merge ignores other-side large file" 0 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-wt" "$absent_json" "git commit --no-edit")"
check "absent-cwd fallback git merge --continue same" 0 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-wt" "$absent_json" "git merge --continue")"
check "non-Git-cwd fallback merge ignores other-side large file" 0 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-wt" "$(json_cwd "$TMP/not-a-repo")" "git commit --no-edit")"
check "non-Git-cwd fallback git merge --continue same" 0 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-wt" "$(json_cwd "$TMP/not-a-repo")" "git merge --continue")"

fb_main="$TMP/fb-merge-dirty"
mk_link_merge "$fb_main" "$TMP/fb-merge-dirty-wt"
big_file "$TMP/fb-merge-dirty-wt/newbig.txt" 20
git -C "$TMP/fb-merge-dirty-wt" add newbig.txt
check "absent-cwd fallback merge blocks newly authored large file" 2 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-dirty-wt" "$absent_json" "git commit --no-edit")"
check "absent-cwd fallback git merge --continue blocks it too" 2 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-dirty-wt" "$absent_json" "git merge --continue")"
check "non-Git-cwd fallback merge blocks newly authored large file" 2 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-dirty-wt" "$(json_cwd "$TMP/not-a-repo")" "git commit --no-edit")"
check "non-Git-cwd fallback git merge --continue blocks it too" 2 \
    "$(run_hook_from "$process_cwd" "$TMP/fb-merge-dirty-wt" "$(json_cwd "$TMP/not-a-repo")" "git merge --continue")"

# --- 13. worktree roots ending in an actual CR or LF byte -------------------
# A root whose final byte is CR (or LF) must survive the exact-one-LF removal:
# that byte is path data and only the LF after it is protocol.  The suffix is
# passed as a literal shell byte (command substitution would strip LF), the
# worktree name is built by direct concatenation, and Git's canonical top
# level is read through a byte-preserving helper so the path LF is not lost to
# ordinary command substitution.
git_toplevel_bytes() { # <worktree-path> -> stdout path + NUL (protocol LF removed)
    python3 - "$1" <<'PY'
import subprocess, sys
probed = subprocess.run(["git", "-C", sys.argv[1], "rev-parse", "--show-toplevel"], capture_output=True)
if probed.returncode != 0:
    sys.exit(probed.returncode)
raw = probed.stdout
raw = raw[:-1] if raw.endswith(b"\n") else raw
sys.stdout.buffer.write(raw + b"\0")
PY
}

# realpath_canonical <path> -> byte-preserving canonical absolute path + NUL
# (macOS /var -> /private/var), matching git's own canonicalization.
realpath_canonical() {
    python3 - "$1" <<'PY'
import os, sys
sys.stdout.buffer.write(os.path.realpath(os.fsencode(sys.argv[1])) + b"\0")
PY
}

suffix_case() { # <name> <suffix-byte-literal>
    local name=$1 byte=$2
    # An empty suffix is a fixture bug (a stripped byte), never a valid case:
    # reject it before any pattern or equality check so it cannot silently
    # pass as "any suffix".
    if [ -z "$byte" ]; then
        echo "FAIL: $name-suffix invoked with an empty suffix byte"
        fail=1
        return
    fi
    local s_main="$TMP/suf-$name-main"
    local s_wt="$TMP/suf-${name}-wt${byte}"
    new_repo "$s_main"
    git -C "$s_main" worktree add -q -b "suf${name}wt" "$s_wt"
    local s_top
    IFS= read -r -d '' s_top < <(git_toplevel_bytes "$s_wt")
    # The canonical root read from git must equal the expected canonical
    # worktree path byte-for-byte, preserving the suffix.  A pattern check
    # like `case *"$byte"` would match an empty byte unconditionally and a
    # stripped root would pass.
    local s_expected
    IFS= read -r -d '' s_expected < <(realpath_canonical "$s_wt")
    if [ "$s_top" != "$s_expected" ]; then
        echo "FAIL: $name-suffix setup mismatch: got $(printf '%q' "$s_top"), expected $(printf '%q' "$s_expected")"
        fail=1
    fi
    mkdir -p "$s_wt/nested/deep"
    echo '{"maxLines": 10, "exclude": ["big.txt"]}' > "$s_top/.large-file-guard.json"
    echo '{"enabled": false}' > "$s_main/.large-file-guard.json"
    big_file "$s_wt/big.txt" 20
    git -C "$s_wt" add big.txt
    check "$name-suffix worktree exclusion passes from nested cwd" 0 \
        "$(run_hook_split "$s_main" "$s_wt/nested/deep" "git commit -m x")"
    echo '{"maxLines": 10}' > "$s_top/.large-file-guard.json"
    check "$name-suffix worktree blocks non-excluded oversized file" 2 \
        "$(run_hook_split "$s_main" "$s_wt/nested/deep" "git commit -m x")"
    check_stderr_contains "$name-suffix block stderr names exact config path" \
        "$s_top/.large-file-guard.json"
}
suffix_case "cr" $'\r'
suffix_case "lf" $'\n'

# --- 14. exact generated-file exemption vs non-exempt sibling YAML ----------
# #1684: `openapi/nhms.v1.yaml` is an exact exemption in `.large-file-guard.json`;
# the exemption is filename-exact, NOT a `*.yaml` glob, so a sibling
# `openapi/other.yaml` over the limit must still be rejected. Both cases are
# load-bearing for the checked-in `openapi/**` contract.
repo="$TMP/yaml-exempt"
new_repo "$repo"
# Real repo config mirrors the repository's default 1000-line limit and the
# exact exemption (the fixture doesn't need the full 1000: use the same 10-line
# fixture limit with the exact same exemption shape).
echo '{"maxLines": 10, "exclude": ["openapi/nhms.v1.yaml"]}' > "$repo/.large-file-guard.json"
mkdir -p "$repo/openapi"
big_file "$repo/openapi/nhms.v1.yaml" 20
git -C "$repo" add openapi/nhms.v1.yaml
check "exact exempt openapi/nhms.v1.yaml over limit is accepted" 0 \
    "$(run_hook "$repo" "git commit -m x")"

repo="$TMP/yaml-sibling"
new_repo "$repo"
echo '{"maxLines": 10, "exclude": ["openapi/nhms.v1.yaml"]}' > "$repo/.large-file-guard.json"
mkdir -p "$repo/openapi"
big_file "$repo/openapi/other.yaml" 20
git -C "$repo" add openapi/other.yaml
check "non-exempt sibling openapi/other.yaml over limit is rejected" 2 \
    "$(run_hook "$repo" "git commit -m x")"

# --- 15. honest deterministic summary ---------------------------------------
# `passes` is incremented in-process by every PASS branch of `check` /
# `check_stderr_contains`, so the count is exactly this run's executed
# assertions (no tee/process-substitution race, no hard-coded number).
echo "summary: $passes PASS assertions executed"
if [ "$fail" -ne 0 ]; then
    echo "summary: FAILURES PRESENT ($fail failed)"
else
    echo "summary: ALL CHECKS PASSED"
fi

exit "$fail"
