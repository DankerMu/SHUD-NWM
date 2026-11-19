## REMOVED Requirements

### Requirement: Production configuration is validated before release

**Reason**: The production ops (`validate-ops`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: Monitoring and alerts cover closure risks

**Reason**: Its only implementation and test were the deleted `validate-ops` lane's hard-coded alert table (#3221). No live code defines the object-store write failure, stale analysis, tile publication error or API p95 metrics it names; the live monitor (`services/orchestrator/monitoring.py`) raises five alerts of its own, pinned by `tests/test_live_monitoring.py`. Found at PR review.
**Migration**: None. The four alert runbooks that described the undefined metrics are deleted with it.

### Requirement: Runbooks and rollback drills are present

**Reason**: The rollback drills were simulated only by the deleted `validate-ops` lane (#3221), and `docs/runbooks/rollback-drills.md` documented `nhms-admin` commands that do not exist. Found at PR review.
**Migration**: None. `docs/runbooks/rollback-drills.md` is deleted with it.
