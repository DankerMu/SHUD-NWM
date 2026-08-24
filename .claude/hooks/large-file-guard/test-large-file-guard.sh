#!/usr/bin/env bash
# Shell-level tests for large-file-guard.sh. Creates throwaway git repos in a
# temp dir and feeds the hook the same JSON shape Claude Code sends. Run:
#   bash .claude/hooks/large-file-guard/test-large-file-guard.sh
set -euo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/large-file-guard.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail=0

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
    else
        echo "FAIL: $name (expected exit $expected, got $actual)"
        fail=1
    fi
}

stderr_file="$TMP/stderr.txt"

# run_hook_split <project_dir> <cwd> <command>; echoes exit code, keeps stderr
run_hook_split() {
    local project_dir=$1 cwd=$2 cmd=$3 rc=0
    printf '{"tool_input":{"command":"%s"},"cwd":"%s"}' "$cmd" "$cwd" \
        | CLAUDE_PROJECT_DIR="$project_dir" bash "$HOOK" >/dev/null 2>"$stderr_file" \
        || rc=$?
    echo "$rc"
}

check_stderr_contains() {
    local name=$1 needle=$2
    if grep -qF -- "$needle" "$stderr_file"; then
        echo "PASS: $name"
    else
        echo "FAIL: $name (stderr lacks: $needle)"
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

exit "$fail"
