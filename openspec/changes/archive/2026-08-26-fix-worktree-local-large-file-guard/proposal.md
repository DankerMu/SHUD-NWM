## Why

The large-file guard receives a tool-call `cwd` for the operation it governs,
but the old implementation reads `.large-file-guard.json` through
`CLAUDE_PROJECT_DIR`. In a linked worktree this combines the main checkout's
policy with the worktree's staged paths, so a worktree-only exclusion cannot
unblock a valid commit and diagnostics point at the wrong authority.

## What Changes

- Resolve the Git top level from the tool-call `cwd` before loading guard configuration.
- Bind configuration, Git reads, direct worktree reads, merge metadata, and block diagnostics to that resolved worktree.
- Preserve exact filesystem path bytes from Git protocol output by removing only one trailing LF delimiter.
- Keep absent/non-Git `cwd` compatibility through the existing `CLAUDE_PROJECT_DIR` fallback.
- Preserve merge-parent attribution: inherited oversized files remain allowed while newly authored oversized content remains blocked.

## Capabilities

### New Capabilities

- `worktree-local-large-file-guard`: The commit guard evaluates one coherent Git worktree identity and preserves legal path bytes.

### Modified Capabilities

None.

## Impact

Affected files are the large-file hook and its shell fixture. No application, CI dependency, database, scheduler, or scientific behavior changes.
