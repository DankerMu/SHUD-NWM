# Proposal — playwright-ubuntu-2604-support (#3198)

Closes #3198.

```text
Issue type: dependency upgrade
Fixture level: standard
Upstream suggested level: absent
Blast radius: frontend E2E lanes in CI and node-27 (mocked, live-display, live-river-click, live-c4-display, m15 visual) and scripts/node27_display_v2_browser_evidence.mjs (uses the active tree's Playwright via --playwright-root)
Selected risk packs: Legacy compatibility, CI / environment
Evidence floor: E1-E6 below
```

## Why

GitHub's `ubuntu-latest` is now Ubuntu 26.04. `@playwright/test@1.59.1` has no chromium build for `ubuntu26.04-x64`, so
the mocked E2E shards failed at install. #3196 pinned the two Playwright jobs to `ubuntu-24.04` as a stopgap, but 24.04
images will eventually be retired.

## What

1. **Upgrade.** Set `@playwright/test` to `^1.64.1`, keeping the caret, with the lockfile resolving 1.64.1, in `apps/frontend/package.json` and update `pnpm-lock.yaml` (pnpm
   only; `uv.lock` is untouched).
   - Support was checked in the published `playwright-core` registry tables: `ubuntu26.04-x64` is supported from
     1.61.0 onward and missing in 1.60.0; 1.64.1 also still supports `ubuntu22.04-x64`, which is node-27's OS.
   - 1.64.1 was chosen as the newest release at least two weeks old (published 2026-10-14). It ships chromium revision
     1248 (156.0.8078.4); 1.59.1 shipped 1217.
   - The repository has no `toHaveScreenshot` baselines, so the chromium bump cannot fail a pixel comparison.
2. **Unpin `ci.yml` `frontend-e2e`** back to `ubuntu-latest` and drop the stopgap comment.
3. **Keep `m15-visual-evidence.yml` on `ubuntu-24.04`, and rewrite its comment.** Deviation from the issue: that
   workflow checks out `inputs.evidence_sha`, which is an arbitrary historical commit, and installs that commit's
   lockfile.
   - Any evidence SHA from before this change carries Playwright 1.59, which cannot install on 26.04.
   - The pin therefore stays until the 24.04 image is retired. The comment says why, so nobody unpins it by analogy
     with `ci.yml`.
4. **node-27 browsers.** The live lanes run on node-27 (Ubuntu 22.04, `~/.cache/ms-playwright` holds
   `chromium-1217`). After the upgrade they need chromium revision 1248.
   - Install it without `--with-deps` (no sudo). That the system libraries satisfy chromium there was only verified for
     1217, so E4 checks 1248 with `ldd`.
   - Playwright garbage-collects browsers. Each `playwright install` drops `.links` entries whose checkout is gone, then
     deletes any revision no link references.
     - 1217 survives only while a 1.59 checkout still links it, and is reclaimed at the next install after that. This is
       expected.
     - Likewise, a 1248 installed only from a temporary worktree is reclaimed once that worktree is gone.
     - So the active tree `/home/nwm/NWM` is brought to 1248 after merge (E6). Until then, nothing in this change runs
       `playwright install` in the active tree.

5. **Guard.** `tests/test_ci_shard_tests.py` asserts:
   - `frontend-e2e` `runs-on == "ubuntu-latest"`;
   - m15 `runs-on == "ubuntu-24.04"`, with a comment naming the `evidence_sha` reason.

   A later re-pin, or an unexplained unpin, then fails a test.
6. **Owner-run lanes.** live-river-click and live-c4-display need receipt pins and are run by the display owner under
   `docs/runbooks/node-27-bringup-checklist.md` C4 at the next display receipt. They are not part of this PR's evidence;
   the PR states this.

## Non-goals

- Changing any E2E spec, Playwright config or sharding. The CI-shape test gains runner-image assertions (What 5).
- Upgrading other frontend dependencies, except what pnpm needs to resolve `@playwright/test`.

## Must preserve

- Every mocked E2E spec passes unchanged, on every shard.
- `pnpm typecheck`, `pnpm test`, `pnpm build` and `check:types` stay green.

## Evidence Floor

- [x] E1 Local: `cd apps/frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build && pnpm check:types`;
  the lockfile diff is limited to Playwright packages.
- [x] E2 Local: `pnpm exec playwright install chromium`, then the full mocked lane `pnpm test:e2e:mocked-regression`
  passes.
- [ ] E3 CI: `Frontend E2E (mocked)`, all shards, green on `ubuntu-latest` (26.04), with an install log showing chromium
  1248.
- [ ] E4 node-27, in a temporary worktree:
  - `corepack pnpm install --frozen-lockfile`, then `corepack pnpm exec playwright install chromium`;
  - `ldd` on the 1248 `chrome` and `chrome-headless-shell` binaries shows no `not found`;
  - the **full** `corepack pnpm test:e2e:mocked-regression` passes, including the WebGL/MapLibre specs, on node-27
    without a GPU;
  - a read-only `test:e2e:live-display` run passes, with both `PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL` set (`https://test.nwm.ac.cn`);
  - `df -h / /home` before and after.
- [ ] E5 Before merge:
  - `openspec validate playwright-ubuntu-2604-support --strict --no-interactive`;
  - `m15-visual-evidence` dispatched twice from the branch, once with `evidence_sha` = the PR head and once with a
    pre-upgrade SHA (`1a4b82f6f…`, in full), both green on `ubuntu-24.04`, with both run URLs in the PR;
  - `uv run pytest -q tests/test_ci_shard_tests.py`.
- [ ] E6 After merge, at the node-27 active tree:
  - `git status --porcelain` gate, then `git pull --ff-only`;
  - `cd apps/frontend && corepack pnpm install --frozen-lockfile && corepack pnpm exec playwright install chromium`;
  - `~/.cache/ms-playwright` holds `chromium-1248` and `chromium_headless_shell-1248`, and `.links` points at
    `playwright-core@1.64.1`;
  - the result is recorded in the archive PR.
