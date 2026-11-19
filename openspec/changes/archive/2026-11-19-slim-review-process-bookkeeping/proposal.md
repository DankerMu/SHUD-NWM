# Slim review-process bookkeeping (#3218)

## Why

Process bookkeeping costs more than it protects (measured 2026-11-19 on master c18f131fc):

- last 60 days: 301 of 790 non-merge commits (38%) touch only `openspec/**` and/or `.review-gate-issues.json`;
  114 of 410 merges are archive PRs, so every issue pays two PRs and two CI rounds;
- `.review-gate-issues.json` holds 618 issues; 15 ever hit a ceiling and 639 of 647 close records are `merged`.
  `check` reads only `ceilingPrs`, so a clean `merged` record carries no decision;
- 198 of 295 capability specs still have a `TBD` Purpose and about 59 describe a test, CI or process mechanism.

## What changes

1. `scripts/review_gate.py record` does not persist a `merged` close unless `--ceiling` is given.
2. The committed memory is pruned to the entries that carry a decision (a ceiling PR or a non-`merged` close): 618 -> 17 issues.
3. Repository instructions (source `instructions/agents/shared.md`, regenerated `CLAUDE.md` / `AGENTS.md`) and
   `openspec/project-profile.md` state three rules:
   - the OpenSpec change is archived inside the implementation PR's final push; there is no post-merge archive PR;
   - an OpenSpec change is written only when product behaviour or an existing capability spec changes; test-only,
     CI-only, docs-only and deletion-only issues use the issue body (`Verification:`) as their fixture;
   - `review_gate.py record` runs only for a non-`merged` close or a ceiling hit.

## Fixture

- Level: standard. Risk packs: none of the domain packs; governance tooling only.
- Must preserve: `check` exit-2 behaviour for an issue with a prior ceiling PR; the closed outcome vocabulary; the
  byte-identical idempotent re-record; `gateEntries` never modified; the loader's bare-key folding and refusal.
- Out of scope: existing archived changes and existing spec files other than `review-governance-records`;
  `docs/review-loop-log.jsonl` and its audit.

## Decisions

- D1. A `merged` record without `--ceiling` exits 0, prints one line saying nothing was recorded, and leaves the
  file bytes unchanged (a missing file stays missing). It is not an error: agents with the old habit must not fail a merge.
  The no-op runs after argument validation and after the memory loads through the structure guard; a malformed or
  conflicting file still exits 1.
- D2. A `merged` record with `--ceiling` behaves exactly as today.
- D3. Prune rule: keep an issue entry iff `ceilingPrs` is non-empty or any `closed` record has an outcome other
  than `merged`. Kept entries are byte-for-byte unchanged, including `gateEntries` and their `merged` records.
- D4. The prune is done once by hand in this change; the CLI gains no prune subcommand.
