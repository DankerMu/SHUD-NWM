#!/usr/bin/env bash
# large-file-guard: PreToolUse hook that blocks `git commit` (and
# `git merge --continue`) when the commit would include text files over a
# line-count threshold (default 1000 lines). When concluding a merge, only
# files whose staged content differs from every parent count — files merged
# in from the other side are not authored by the commit.
# Reads the tool-call JSON on stdin (Claude Code and Codex use the same
# shape) and exits 2 with a stderr explanation to deny.
#
# Optional config at <project>/.large-file-guard.json:
#   { "enabled": true, "maxLines": 1000, "exclude": ["docs/data/*.csv"] }
set -euo pipefail

input=$(cat)
root="${CLAUDE_PROJECT_DIR:-$PWD}"

LFG_INPUT="$input" LFG_ROOT="$root" python3 - <<'PY'
import fnmatch
import json
import os
import re
import subprocess
import sys

data = json.loads(os.environ.get("LFG_INPUT") or "{}")
tool_input = data.get("tool_input") or {}
command = tool_input.get("command") or ""
if isinstance(command, list):
    command = " ".join(str(part) for part in command)
if not re.search(r"\bgit\b[^|;&]*\bcommit\b", command) and not re.search(
    r"\bgit\b[^|;&]*\bmerge\b[^|;&]*--continue\b", command
):
    sys.exit(0)

root = os.environ["LFG_ROOT"]
cwd = data.get("cwd") or root

config = {}
cfg_path = os.path.join(root, ".large-file-guard.json")
if os.path.isfile(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        config = {}

if not config.get("enabled", True):
    sys.exit(0)

max_lines = int(config.get("maxLines", 1000))
DEFAULT_EXCLUDE = [
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "*.min.*",
    "*.svg",
    "*.map",
    "*.snap",
    "dist/**",
    "build/**",
    "vendor/**",
    "node_modules/**",
]
exclude = list(config.get("exclude", [])) + DEFAULT_EXCLUDE


def git(*args):
    result = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def numstat_paths(*extra):
    paths = set()
    for line in git("diff", "--numstat", *extra).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] != "-":  # "-" marks binary
            paths.add(parts[2])
    return paths


def excluded(path):
    return any(
        fnmatch.fnmatch(path, pattern)
        or fnmatch.fnmatch(os.path.basename(path), pattern)
        for pattern in exclude
    )


def merge_parent_shas():
    # Non-empty only while a merge awaits conclusion (git commit /
    # git merge --continue). May list several heads for octopus merges.
    path = git("rev-parse", "--git-path", "MERGE_HEAD").strip()
    if not path:
        return []
    try:
        with open(os.path.join(cwd, path), encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except OSError:
        return []


staged = numstat_paths("--cached")
# Concluding a merge: the index legitimately carries every file changed on
# the other side. Only content that differs from ALL parents was actually
# authored by this commit (conflict resolutions, files added by hand).
for sha in merge_parent_shas():
    staged &= numstat_paths("--cached", sha)
# With -a/--all, tracked modified files are committed at their working-tree
# state even if unstaged (or partially staged).
worktree = set()
if re.search(
    r"\bgit\b[^|;&]*\bcommit\b[^|;&]*\s(--all\b|-[a-zA-Z]*a[a-zA-Z]*\b)", command
):
    worktree = numstat_paths()

offenders = []
for path in sorted(staged | worktree):
    if excluded(path):
        continue
    if path in worktree:
        try:
            with open(
                os.path.join(cwd, path), encoding="utf-8", errors="ignore"
            ) as fh:
                content = fh.read()
        except OSError:
            continue
    else:
        content = git("show", f":{path}")
    count = len(content.splitlines())
    if count > max_lines:
        offenders.append((path, count))

if offenders:
    listing = "; ".join(f"{p} ({n} lines)" for p, n in offenders)
    sys.stderr.write(
        f"large-file-guard: commit blocked — file(s) exceed {max_lines} lines: "
        f"{listing}. Split the file into smaller modules before committing. If it "
        f'is legitimately large (generated/vendored/data), add it to "exclude" in '
        f".large-file-guard.json at the project root and retry.\n"
    )
    sys.exit(2)
PY
