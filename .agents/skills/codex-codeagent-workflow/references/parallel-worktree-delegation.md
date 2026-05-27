# Parallel Worktree Delegation

Use this reference for any parallel code-writing delegation in the Codex + codeagent workflow, including Phase 1 implementation and Phase 6 fix passes.

This reference does not apply to read-only fixture review, cross-review, invariant audit, or final review tasks. Read-only tasks can share the PR worktree and should normally run through `codeagent-wrapper --parallel --backend codex`.

## Non-Negotiable Rules

- Parallel code-writing workers must use separate git worktrees under `.codex/worktrees/`.
- The parent Codex workflow is the only actor that integrates patches into the PR branch.
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

Before starting any parallel code-writing workers, persist a manifest under the local evidence directory, for example `.codex/workplans/<issue-or-pr>/parallel-worktree-manifest.md`.

```text
Parallel Worktree Manifest:
Issue/PR: #<N>
Phase: <Phase 1 implementation | Phase 6 fix>
Parent branch/head: <branch> <sha>
Integration owner: Codex parent workflow

Workers:
- id: <worker-id>
  purpose: <implementation slice or fix group>
  worktree: .codex/worktrees/pr-<N>-<worker-id>
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
mkdir -p .codex/worktrees
git worktree add .codex/worktrees/pr-<N>-<worker-id> HEAD
```

If two workers need different starting commits, record that explicitly in the manifest. Do not create worker worktrees from stale local branches unless the manifest explains why.

## Worker Prompt Boundary

Every parallel code-writing worker prompt must include the normal delegation guard plus this boundary:

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
git -C .codex/worktrees/pr-<N>-<worker-id> diff -- <allowed-files> > .codex/workplans/<N>/<worker-id>.patch
git apply --3way .codex/workplans/<N>/<worker-id>.patch
```

Do not integrate by copying whole files over the parent worktree unless the file is wholly owned by that worker and the manifest explicitly allows it.

## Verification

Run focused verification after each integrated worker patch. After all worker patches are integrated, run the full phase verification required by the OpenSpec fixture and workflow phase.

If one worker's patch invalidates another worker's assumptions, stop integration and update the manifest. Do not let later workers silently overwrite earlier integrated behavior.

## Cleanup

Before committing, merging, or leaving the workflow, inspect and clean delegated worktrees:

```bash
git worktree list
git -C .codex/worktrees/pr-<N>-<worker-id> status --short
git worktree remove .codex/worktrees/pr-<N>-<worker-id>
git worktree prune
```

Only remove a worktree after confirming that its useful diff has been integrated, rejected, or superseded. If removal fails because the worktree has uncommitted work, inspect it and either integrate, save a patch to the evidence directory, or document why it is intentionally discarded.

Persist any retained worktree in the local evidence directory:

```text
Retained worktree:
- path: .codex/worktrees/pr-<N>-<worker-id>
- reason: <debugging / waiting for user decision / blocked integration>
- owner: <Codex or worker id>
- cleanup trigger: <event/date/action>
```

## Forbidden Shortcuts

- No parallel workers in the parent PR worktree.
- No overlapping write sets.
- No unreviewed whole-file copy from a worker worktree into the PR worktree.
- No worker-created commits on the PR branch.
- No worker-created pushes.
- No cleanup by deleting `.codex/worktrees` recursively without checking `git worktree list` and worker status.
