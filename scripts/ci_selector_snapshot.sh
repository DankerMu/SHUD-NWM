#!/usr/bin/env bash
# Parent/head byte-equality oracle for the CI test selector split (#2948,
# entropy-burndown section 6). Runs the selector once per fixed changed path and
# once on all seven together, and prints ONE JSON object: key = input label in
# order (the path itself, "all" for the union), value = exactly the four
# GitHub outputs `count`, `tests_json`, `meta_guard_only`,
# `collection_smoke_required` (the raw `key=value` strings; the fifth `tests=`
# line is dropped). Selector stderr is passed through, never captured.
#
# Local-only. MUST NOT run on node-22: it calls `uv run`, which is banned in
# that checkout.
#
# Usage: ci_selector_snapshot.sh [SELECTOR_ROOT] [REPO_ROOT]
#   SELECTOR_ROOT  tree whose scripts/select_ci_tests.py is executed
#   REPO_ROOT      tree passed as --repo-root (and the uv project)
#   Both default to `git rev-parse --show-toplevel`.
#
# Comparison method (every 6.x PR):
#   P=$(git merge-base origin/master HEAD)
#   mkdir -p <scratch>/parent
#   git archive "$P" scripts | tar -x -C <scratch>/parent
#   scripts/ci_selector_snapshot.sh "$PWD" > <scratch>/head.json
#   scripts/ci_selector_snapshot.sh <scratch>/parent "$PWD" > <scratch>/parent.json
#   cmp <scratch>/parent.json <scratch>/head.json
# Only the selector code differs between the two runs. Extract the whole
# `scripts` tree (no worktree, no stash, no checkout of single files): a lone
# parent facade would resolve its siblings from the head tree and pass falsely.
set -euo pipefail

unset GITHUB_ACTIONS

toplevel=""
if [ "$#" -lt 2 ]; then
  toplevel="$(git rev-parse --show-toplevel)"
fi
SELECTOR_ROOT="$(cd "${1:-$toplevel}" && pwd)"
REPO_ROOT="$(cd "${2:-$toplevel}" && pwd)"

INPUTS=(
  "packages/common/state_manager.py"
  "apps/api/routes/pipeline.py"
  "apps/frontend/src/App.tsx"
  "docs/runbooks/current-production-ops.md"
  "scripts/select_ci_tests.py"
  "tests/test_production_scheduler.py"
  "db/migrations/000003_enums.sql"
)

work="$(mktemp -d "${TMPDIR:-/tmp}/ci-selector-snapshot.XXXXXX")"
trap 'rm -rf "$work"' EXIT

cd "$REPO_ROOT"

run_selector() {
  # $1 = output stem; the changed-file list must already exist at $work/$1.in
  uv run python "$SELECTOR_ROOT/scripts/select_ci_tests.py" \
    --changed-file "$work/$1.in" \
    --repo-root "$REPO_ROOT" \
    --github-output "$work/$1.out" >/dev/null
}

labels=()
outputs=()
i=0
for path in "${INPUTS[@]}"; do
  printf '%s\n' "$path" > "$work/$i.in"
  run_selector "$i"
  labels+=("$path")
  outputs+=("$work/$i.out")
  i=$((i + 1))
done
printf '%s\n' "${INPUTS[@]}" > "$work/all.in"
run_selector all
labels+=("all")
outputs+=("$work/all.out")

uv run python - "${#labels[@]}" "${labels[@]}" "${outputs[@]}" <<'PY'
import json
import sys

KEYS = ("count", "tests_json", "meta_guard_only", "collection_smoke_required")
count = int(sys.argv[1])
labels = sys.argv[2 : 2 + count]
outputs = sys.argv[2 + count :]
assert len(outputs) == count, (labels, outputs)
snapshot = {}
for label, output in zip(labels, outputs):
    with open(output, encoding="utf-8") as handle:
        fields = dict(line.split("=", 1) for line in handle.read().splitlines() if "=" in line)
    snapshot[label] = {key: fields[key] for key in KEYS}
json.dump(snapshot, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
