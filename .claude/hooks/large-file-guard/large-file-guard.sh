#!/usr/bin/env bash
# large-file-guard: PreToolUse hook that blocks `git commit` (and
# `git merge --continue`) when the commit would include text files over a
# line-count threshold (default 1000 lines). When concluding a merge, only
# files whose staged content differs from every parent count — files merged
# in from the other side are not authored by the commit.
# Reads the tool-call JSON on stdin (Claude Code and Codex use the same
# shape) and exits 2 with a stderr explanation to deny.
#
# The tool-call JSON `cwd` decides which worktree is being committed: its Git
# top level is resolved (the probe runs at the raw JSON cwd) before anything
# else and is the root for the config file, all `git -C` operations, worktree
# file reads and the MERGE_HEAD lookup (which runs at the resolved root, so an
# absent or non-Git cwd still sees the active/fallback repo's merge metadata).
# CLAUDE_PROJECT_DIR is a fallback only (JSON cwd absent, or not inside a Git
# worktree), which preserves legacy/non-worktree callers.
#
# Path-valued Git output (`--show-toplevel`, `--git-path MERGE_HEAD`) is
# captured as raw bytes, stripped of exactly one protocol LF byte, and decoded
# losslessly via os.fsdecode — generic whitespace stripping would truncate
# legal trailing-space/CR path bytes and text mode would translate CR to LF.
#
# Optional config at <git-top-level>/.large-file-guard.json:
#   { "enabled": true, "maxLines": 1000, "exclude": ["docs/data/*.csv"] }
set -euo pipefail

input=$(cat)

LFG_INPUT="$input" LFG_FALLBACK_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}" python3 - <<'PY'
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

root = os.environ["LFG_FALLBACK_ROOT"]
cwd = data.get("cwd") or ""


def git_path_output(raw):
    # Git emits one path plus a protocol LF byte; the path itself is legal
    # filesystem bytes (trailing spaces/tabs, CR, even embedded newlines) and
    # must survive intact, so exactly that one trailing byte is removed and
    # the rest is decoded losslessly through the filesystem encoding.  Text
    # mode is not used: universal-newline translation would rewrite legal CR
    # path bytes before this helper could see them.
    raw = raw[:-1] if raw.endswith(b"\n") else raw
    return os.fsdecode(raw)


def git_path(*args, run_cwd=None):
    return subprocess.run(
        ["git", "-C", run_cwd if run_cwd is not None else cwd, *args],
        capture_output=True,
    )


if cwd:
    # The tool call names the worktree it acts on; its Git top level is the
    # single root for config, git operations and worktree file reads. A cwd
    # outside any Git worktree falls back to the legacy root.
    probed = git_path("rev-parse", "--show-toplevel")
    if probed.returncode == 0:
        root = git_path_output(probed.stdout)

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
        ["git", "-C", root, *args], capture_output=True, text=True
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
    # MERGE_HEAD lives in the resolved repo's admin directory, so it is
    # queried at the active/fallback root — not the raw JSON cwd, which may be
    # absent or outside any Git repository.
    path = git_path_output(
        git_path("rev-parse", "--git-path", "MERGE_HEAD", run_cwd=root).stdout
    )
    if not path:
        return []
    try:
        merge_head = path if os.path.isabs(path) else os.path.join(root, path)
        with open(merge_head, encoding="utf-8") as fh:
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
                os.path.join(root, path), encoding="utf-8", errors="ignore"
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
        f"{cfg_path} and retry.\n"
    )
    sys.exit(2)
PY
