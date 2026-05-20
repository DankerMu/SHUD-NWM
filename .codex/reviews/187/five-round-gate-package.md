# PR #187 Five-Round Gate Package

PR: #187
Issue: #179
Current head SHA: `3e97be2100bb2aa091cec146d1f0b60ff67b5d2c`
Latest reviewed head SHA: `3e97be2100bb2aa091cec146d1f0b60ff67b5d2c`
Comprehensive review rounds counted: 21

## Deep Review Failure Retro

Round SHAs/reports:
- Round 1: `.codex/reviews/187/round1/*`
- Round 2: `.codex/reviews/187/round2/*`
- Round 3: `.codex/reviews/187/round3/*`, including `review-failure-retro.md`
- Rounds 4-9: `.codex/reviews/187/round4/*` through `.codex/reviews/187/round9/*`
- Round 10: `.codex/reviews/187/round10/*`, plus `.codex/reviews/187/review-failure-retro-round10.md`
- Rounds 11-14: `.codex/reviews/187/round11/*` through `.codex/reviews/187/round14/*`
- Round 15: `.codex/reviews/187/round15/*`, plus `.codex/reviews/187/review-failure-retro-round15.md`
- Rounds 16-21: `.codex/reviews/187/round16/*` through `.codex/reviews/187/round21/*`

Repeated or moving failure classes:
- Canonical auth/audit/readiness evidence identity: surfaced around round 10 and closed by later invariant fixes.
- Public CLI/script-entry authorization boundary: surfaced repeatedly from round 13 onward across flood supersede, hindcast submit, and registry import public CLIs.
- Public preflight before attacker-controlled/resource-heavy work: moved from trusted-internal bypass, to explicit CLI evidence support, to registry manifest preflight binding, and now to flood CLI DB-session preflight ordering.

Why prior fixes did not close the invariant:
- Fixture scope gap: no. The OpenSpec selected Public API / CLI / script entry and requires denied/release-blocked protected actions to avoid mutation and preserve stable `AUTH_REQUIRED`, `RBAC_FORBIDDEN`, and `RELEASE_BLOCKED` results.
- Fix prompt too narrow: yes. Prior passes fixed one CLI or one resource boundary at a time instead of treating all public mutating CLI wrappers as one pre-resource authorization invariant.
- Reviewer contract vague/inconsistent: no. Round 21 names concrete commands, files, failure order, and tests.
- Missing regression evidence: yes. Existing flood CLI tests monkeypatch `_session_from_env()` and prove no service mutation, but they do not prove public wrapper denial occurs before DB setup when `DATABASE_URL` is absent or when `_session_from_env()` raises.
- PR too broad / should split: no for this finding. The remaining defect is within the already selected M17 public CLI auth boundary and is required for this PR's security invariant.

## Gate-Level PR Strategy Review

Direction check:
- The PR is solving the right M17 problem: canonical auth/RBAC enforcement, audit/readiness truthfulness, and public entrypoint fail-closed behavior. The repeated failures indicate incomplete invariant closure, not a wrong product direction.

Architecture/refactor check:
- A broad architecture rewrite is not required. The service-layer guards are already in the correct place as defense in depth. The missing abstraction is a small wrapper-level preflight helper for public flood CLI mutation commands so authorization is enforced before `_session_from_env()`.

Loop check:
- The workflow has been chasing symptoms across sibling public CLI surfaces. The next action must close the shared public-CLI pre-resource invariant, not patch only the cited lines.

Functionality root-cause check:
- Most user-visible feature requirements are implemented and CI is green, but public `nhms-flood hindcast-submit` and `nhms-flood fit-curves --supersede-model-id` can still return `DATABASE_URL_MISSING` before `AUTH_REQUIRED` or `RELEASE_BLOCKED`. That means the stable CLI auth contract is not yet fundamentally closed.

Security/safety root-cause check:
- The root security invariant is not fully closed until missing, unauthorized, or release-blocked public CLI invocations fail before DB session setup and before file/model/segment work. Registry import is now closed; flood CLI wrapper ordering remains open.

Decision:
- Continue with a root-cause invariant closure, not another narrow review. Add a shared or consistently applied flood CLI preflight policy check before `_session_from_env()` for `hindcast-submit` and non-dry-run `fit-curves --supersede-model-id`. Preserve service-layer checks as defense in depth.

Execution plan:
- Delegate one codeagent fix task for `workers/flood_frequency/cli.py`, `tests/test_hindcast.py`, and `tests/test_flood_frequency.py`.
- Required tests:
  - No `DATABASE_URL`, missing auth on argparse `hindcast-submit` returns `AUTH_REQUIRED`, not `DATABASE_URL_MISSING`.
  - No `DATABASE_URL`, production/live or SAML blocked auth on argparse `hindcast-submit` returns `RELEASE_BLOCKED`.
  - No `DATABASE_URL`, missing auth on argparse `fit-curves --supersede-model-id` returns `AUTH_REQUIRED`, not `DATABASE_URL_MISSING`.
  - No `DATABASE_URL`, production/live or SAML blocked auth on argparse `fit-curves --supersede-model-id` returns `RELEASE_BLOCKED`.
  - `_session_from_env()` sentinel is not reached for missing-auth, unauthorized-role, and release-blocked flood mutation CLI paths.
- Verification command: `uv run pytest -q tests/test_hindcast.py tests/test_flood_frequency.py`

## Invariant Surface Inventory

- Shared helper roots: `workers/flood_frequency/cli.py::_cli_policy_decision`, optional new preflight helper; `apps.api.auth.cli_policy_decision_from_evidence`; service-layer `require_policy_evidence` checks.
- Public entrypoints: click and argparse `hindcast-submit`; click and argparse `fit-curves` when `--supersede-model-id` is supplied and `--dry-run` is false.
- Read surfaces: CLI auth flags/env inputs; `DATABASE_URL`; model/supersede target ids.
- Write/delete/overwrite surfaces: hydro run creation, Slurm submission, frequency curve supersede updates, report output.
- Staging/publish/rollback surfaces: release-blocked live/SAML modes and no-mutation denial paths.
- Producer/consumer evidence boundaries: CLI `PolicyDecision` evidence emitted in successful reports; stderr stable auth error codes.
- Stale-state/idempotency boundaries: repeated denied/release-blocked CLI calls must not open DB sessions or mutate rows.
- Unchanged downstream consumers: service-level `submit_hindcast()` and `fit_curves()` guards remain defense in depth for non-CLI callers.

## Regression Matrix

- `hindcast-submit` missing auth + no `DATABASE_URL` -> stderr contains `AUTH_REQUIRED`; `_session_from_env()` not reached.
- `hindcast-submit` live/SAML blocked auth + no `DATABASE_URL` -> stderr contains `RELEASE_BLOCKED`; `_session_from_env()` not reached.
- `hindcast-submit` unauthorized role + no `DATABASE_URL` -> stderr contains `RBAC_FORBIDDEN`; `_session_from_env()` not reached.
- `fit-curves --supersede-model-id` missing auth + no `DATABASE_URL` -> stderr contains `AUTH_REQUIRED`; `_session_from_env()` not reached.
- `fit-curves --supersede-model-id` live/SAML blocked auth + no `DATABASE_URL` -> stderr contains `RELEASE_BLOCKED`; `_session_from_env()` not reached.
- `fit-curves --supersede-model-id` unauthorized role + no `DATABASE_URL` -> stderr contains `RBAC_FORBIDDEN`; `_session_from_env()` not reached.
- `fit-curves --dry-run --supersede-model-id` remains compatible and does not require supersede policy evidence.
- Allowed CLI policy paths still open DB/session only after an allow decision and continue to include `auth_policy_decision` in successful JSON output.

## Post-Gate Budget

After this corrective action, run local verification and at most one comprehensive cross-review. If that review reports another critical/major issue in the same public CLI authorization boundary family, re-enter this strategy review and choose a stronger refactor/split decision instead of returning to narrow line-item repair.
