## Context

Issue #1634 is an identity mismatch: the hook acts on the Git operation named
by raw tool input `cwd`, but previously selected policy through an ambient
main-checkout path. Linked worktrees make those identities diverge. Repair
intensity is high because this hook can block every commit and because Git paths
may legally contain whitespace, carriage return, or line feed bytes.

## Goals / Non-Goals

**Goals:**

- Use the operation worktree's Git top level as the single authority for config, Git, files, merge metadata, and diagnostics.
- Preserve legal Git path bytes exactly except for the single LF protocol delimiter.
- Preserve fallback callers, direct tracked-file checks, `commit -a`, and merge-parent attribution.

**Non-Goals:**

- Do not change `maxLines`, broad exclusions, merge authorship semantics, or the hook's allow/block protocol.
- Do not modify unrelated repository tooling.

## Decisions

1. Probe `git rev-parse --show-toplevel` using the raw JSON `cwd`; only a missing or non-Git `cwd` falls back to `CLAUDE_PROJECT_DIR`.
2. Run all later config/Git/MERGE_HEAD/file operations against the resolved root. Diagnostics name the exact effective config path.
3. Capture path-valued Git subprocess output as bytes, remove exactly one terminal `b"\n"`, then use `os.fsdecode`. Avoid `.strip()`, `.rstrip()`, and universal-newline translation.
4. Exercise real linked-worktree, merge, commit-`-a`, fallback, trailing-space, CR, and LF-suffix fixtures. A fixture must mutate the same seam the hook consumes rather than claim coverage through an ambient process directory.

## Invariant Matrix

- Governing invariant: every hook decision SHALL use the same Git worktree identity as the operation it governs.
- Producer: tool-call JSON `cwd` and Git path protocol output.
- Validator: top-level probe and `.large-file-guard.json` loader.
- Consumers: staged-file discovery, direct worktree reads, MERGE_HEAD lookup, merge-parent filtering, and diagnostics.
- Failure paths: absent/non-Git `cwd`, nested linked-worktree directories, legal path suffix bytes, `commit -a`, merge conclusion, and fallback merge metadata.
- Evidence: the shell fixture's 44 named scenarios plus strict OpenSpec validation.

## Risks / Trade-offs

- Git path bytes can be changed before explicit delimiter handling; byte capture prevents locale/newline normalization.
- Fallback compatibility can silently rebind MERGE_HEAD to raw `cwd`; fixture roots are deliberately distinct so that regression turns red.
- Linked-worktree commits inspect both staged and directly modified tracked files; config and file reads must share the same resolved root.

## Migration Plan

1. Replace the mixed-root hook logic and add the full identity/path matrix.
2. Run the shell fixture and repository lint/spec gates.
3. Roll back by reverting this change; no persisted data migration exists.

## Open Questions

None.
