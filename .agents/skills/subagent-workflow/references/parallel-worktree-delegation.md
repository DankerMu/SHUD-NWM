# Parallel Worktree Delegation

Use this reference for any parallel code-writing delegation in the subagent workflow, including Phase 1 implementation and Phase 6 fix passes.

This reference does not apply to read-only fixture review, cross-review, invariant audit, or final review tasks. Read-only tasks can share the PR worktree and should normally run as parallel read-only subagents.

## Non-Negotiable Rules

- Parallel code-writing workers must use separate git worktrees under `.worktrees/`.
- Add `.worktrees/` to the target repository's `.gitignore` so delegated worktrees are never accidentally committed.
- The parent orchestrator workflow is the only actor that integrates patches into the PR branch.
- Workers must not write directly in the parent PR worktree.
- Workers must not commit, push, merge, rebase, reset, or run destructive git commands.
- Workers must not revert or overwrite changes made by other workers.
- Each worker must have a disjoint allowed write set.
- Shared helper roots, schema/contract files, public API boundaries, state-machine files, and high-conflict tests require one owner per round.
- If a worker needs an unassigned file, it must stop and report the needed file instead of editing it.
- A worker diff that touches files outside its allowed write set must be rejected or reworked before integration.
- Every delegated worktree must be removed or explicitly documented as retained before the PR is finished.

## When To Parallelize

Parallel code-writing is appropriate when work can be split into independent slices with non-overlapping write sets:

- Disjoint modules or packages.
- Disjoint test files.
- Independent worker/business lanes behind a stable shared contract.
- Documentation/evidence updates separate from code.

Do not parallelize code writing when the next change centers on one shared invariant:

- One state machine or retry/cancellation policy.
- One shared helper root.
- One schema, migration, contract, or public API boundary.
- One shared test fixture or test file.
- A fix whose design is still uncertain or likely to move across sibling surfaces.

In those cases, use one code-writing owner for the shared implementation. Other workers may do read-only audit, prepare non-overlapping tests, or inspect sibling surfaces.

## Parallel Worktree Manifest

Before starting any parallel code-writing workers, persist a manifest under the local evidence directory, for example `.workplans/<issue-or-pr>/parallel-worktree-manifest.md`.

```text
Parallel Worktree Manifest:
Issue/PR: #<N>
Phase: <Phase 1 implementation | Phase 6 fix>
Parent branch/head: <branch> <sha>
Integration owner: orchestrator parent workflow

Workers:
- id: <worker-id>
  purpose: <implementation slice or fix group>
  worktree: .worktrees/pr-<N>-<worker-id>
  base sha: <sha>
  allowed write set:
  - <file/glob>
  forbidden files:
  - <file/glob>
  expected output: <patch/report/tests>
  verification:
  - <focused command>
  integration order: <1..n>

Single-owner shared files:
- <file/glob>: <worker-id>

Integration plan:
- <worker-id>: inspect diff -> reject/rework if out of scope -> apply patch -> run focused verification

Cleanup plan:
- Remove each worktree after integrated, rejected, or superseded.
- Run `git worktree prune` before finishing the PR.
- If a worktree is retained for debugging, record the path, reason, owner, and cleanup trigger.
```

## Worktree Creation

Create worker worktrees from the current PR head. Use unique paths.

```bash
mkdir -p .worktrees
git worktree add .worktrees/pr-<N>-<worker-id> HEAD
```

If two workers need different starting commits, record that explicitly in the manifest. Do not create worker worktrees from stale local branches unless the manifest explains why.

## Worktree Guard Hook (mechanical fence)

When the `worktree-guard` hook is installed in the host project, bracket every worktree-delegation window with its manifest file so out-of-root writes are denied mechanically instead of by prompt discipline:

1. **Entry** — immediately after creating the worker worktrees and persisting the parallel worktree manifest, write `.worktree-guard.json` at the project root. `allowedRoots` lists every delegated worktree plus the roots the orchestrator legitimately writes during the window (evidence and spec directories):

```json
{
  "enabled": true,
  "allowedRoots": [
    ".worktrees/pr-<N>-<worker-id>",
    ".workplans/",
    "openspec/"
  ]
}
```

2. **Exit** — after integration and worktree cleanup (`git worktree prune`), delete `.worktree-guard.json`. Never leave it behind: a stale guard file blocks normal work in the next session.
3. **Session restart / context compaction** — if `.worktree-guard.json` exists at session start, the workflow is mid-delegation: reload the parallel worktree manifest before writing any file. This is exactly the drift the hook exists to stop.

Scope notes: the hook guards file-edit tools only (`Edit|Write|MultiEdit|NotebookEdit` on Claude Code, `apply_patch` on Codex). Patch integration via `git apply` in the shell is unaffected, and shell-level `mv`/`cp`/redirects are not covered — the manifest inspection rules above still apply. Without the hook installed, this entire section is a no-op and the discipline stays orchestrator-enforced.

## Worker Prompt Boundary

Every parallel code-writing worker brief must include the Required Subagent Boundary from `SKILL.md` plus this parallel-worktree boundary:

```text
Parallel worktree boundary:
- You are not alone in this repository.
- Work only in your assigned worktree: <worktree path>.
- Edit only the files in your allowed write set.
- Do not edit forbidden files or unrelated files.
- Do not commit, push, merge, rebase, reset, or modify the parent PR worktree.
- Do not revert or overwrite changes made by other workers.
- If the fix requires an unassigned file, stop and report the needed file instead of editing it.
- Report changed files and verification results.
```

For implementation tasks, also require implementation and tests for that slice to ship together. For fix tasks, require the worker to close the assigned failure class, not only the cited line.

## Integration

After each worker finishes:

1. Inspect the worker worktree status and diff.
2. Reject or re-delegate if the diff touches files outside the allowed write set.
3. Generate a patch from the worker worktree.
4. Apply the patch to the parent PR worktree in manifest order, preferably with `git apply --3way`.
5. Resolve conflicts only when they are inside the worker's ownership scope and consistent with the manifest. Otherwise reject and re-plan.
6. Run the worker's focused verification in the parent PR worktree after applying the patch.
7. Update the local evidence with applied/rejected status, patch path if saved, and verification result.

Example patch flow:

```bash
git -C .worktrees/pr-<N>-<worker-id> diff -- <allowed-files> > .workplans/<N>/<worker-id>.patch
git apply --3way .workplans/<N>/<worker-id>.patch
```

Do not integrate by copying whole files over the parent worktree unless the file is wholly owned by that worker and the manifest explicitly allows it.

## Verification

Run focused verification after each integrated worker patch. After all worker patches are integrated, run the full phase verification required by the OpenSpec fixture and workflow phase.

If one worker's patch invalidates another worker's assumptions, stop integration and update the manifest. Do not let later workers silently overwrite earlier integrated behavior.

## Cleanup

Before committing, merging, or leaving the workflow, inspect and clean delegated worktrees:

```bash
git worktree list
git -C .worktrees/pr-<N>-<worker-id> status --short
git worktree remove .worktrees/pr-<N>-<worker-id>
git worktree prune
```

If the worktree-guard protocol is active, delete `.worktree-guard.json` after the last worktree is removed (see "Worktree Guard Hook" above).

Only remove a worktree after confirming that its useful diff has been integrated, rejected, or superseded. If removal fails because the worktree has uncommitted work, inspect it and either integrate, save a patch to the evidence directory, or document why it is intentionally discarded.

Persist any retained worktree in the local evidence directory:

```text
Retained worktree:
- path: .worktrees/pr-<N>-<worker-id>
- reason: <debugging / waiting for user decision / blocked integration>
- owner: <orchestrator or worker id>
- cleanup trigger: <event/date/action>
```

## Forbidden Shortcuts

- No parallel workers in the parent PR worktree.
- No overlapping write sets.
- No unreviewed whole-file copy from a worker worktree into the PR worktree.
- No worker-created commits on the PR branch.
- No worker-created pushes.
- No cleanup by deleting `.worktrees` recursively without checking `git worktree list` and worker status.
