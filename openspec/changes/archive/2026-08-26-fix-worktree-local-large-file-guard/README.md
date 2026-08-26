# fix-worktree-local-large-file-guard

Resolve #1634 by binding the large-file guard's configuration, Git inspection, filesystem reads, merge metadata, and diagnostics to the Git worktree that owns the tool-call `cwd`.
