# delete-zero-reference-dead-code (#3147)

## Why

The round-3 entropy audit (2026-11-16) ran vulture (min-confidence 60) over `apps/api`, `services`, `packages`,
`workers`, `scripts` and `db`, then adjudicated every unused function, class and method by a whole-repo word grep.
Production, tests and docs were counted separately, and decorator-registered routes, CLI commands and validators were
excluded as false positives.

40 helpers turned up with **zero references in production code and in tests**. On top of those:

- 9 `services/orchestrator/scheduler.py` forwarders whose removal condition in
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` (`scheduler-model-discovery-forwarders`, "callers migrated to
  `scheduler_models`") is now met;
- one orphan script, `scripts/m24_grib_preflight_proof.py`, which nothing references.

Dead code misleads readers and reviewers, inflates the structural file budget, and keeps stale concepts alive. One
example is `_legacy_strict_warm_start_evidence` for retired topology.

## What

- Delete each symbol listed in issue #3147 after re-verifying that nothing references it, and remove anything that
  becomes unused as a result (iterate).
- Delete the 9 forwarders and remove only their names from the compatibility inventory rows. The facade guard
  stays unchanged: it never detects stale inventory names, so a grep catches them (task 2.6). Keep every forwarder
  that is still referenced (for example `_coerce_registered_model`).
- Delete the orphan script.
- Update any living doc that names a deleted symbol.

No runtime behaviour changes.

## Non-goals

- Test-only legacy helpers. Those are #3148, which depends on this issue for `_query_comment_accounting`.
- Retired-route or template branches (#3149).
- Splitting oversized files.
- Symbols that turn out to have a reference. Those are kept, with the reason recorded in the PR.

## Triage

Issue type: tech-debt cleanup.
Fixture level: expanded. The legacy-compatibility trigger applies: the 9 forwarders are the old
`services.orchestrator.scheduler._*` import path that `SCHEDULER_COMPATIBILITY_INVENTORY.md` governs, and deleting the
orphan script touches a script entry. Fixture review round 1 raised the level from compact.
Upstream suggested level: absent.
Blast radius: a symbol reached dynamically gets deleted, which would surface as an `ImportError` or `AttributeError` at
runtime. Dynamic reach means a `getattr` dispatch, a string entry point, a `monkeypatch` target, sbatch or systemd, or a
compatibility re-export.
Selected risk packs: Legacy compatibility (forwarders + inventory/guard); Public API / CLI / script entry (orphan script,
`pyproject` entry points); Documentation.
Evidence floor: `uv run ruff check .`; the selector-chosen pytest set for the touched modules, plus
`tests/test_select_ci_tests.py` and the compatibility-facade guard tests (local and node-27 temp worktree); vulture
diff before and after.
