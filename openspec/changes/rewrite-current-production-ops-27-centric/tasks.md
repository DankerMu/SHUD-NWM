## 1. Live Fact Collection

- [x] 1.1 Verify node-27 active DB/display env without leaking secrets.
- [x] 1.2 Verify node-27 ingest trigger and recent autopipe evidence.
- [x] 1.3 Verify node-22 Slurm Gateway, diagnostic API, NFS paths, and historical DB boundary.
- [x] 1.4 Correct node-27 display port drift to match repo/nginx `8080`, then verify local and public `/health`.

## 2. OpenSpec Fixture

- [x] 2.1 Create OpenSpec change `rewrite-current-production-ops-27-centric`.
- [x] 2.2 Add production ops readiness spec delta for 27-centric runbook truth.
- [x] 2.3 Run focused OpenSpec review and fix any P0/P1 findings.

## 3. Runbook Rewrite

- [x] 3.1 Remove the top stale warning banner from `docs/runbooks/current-production-ops.md`.
- [x] 3.2 Rewrite §1, §2, §3.1, §3.2, §3.3, and §5 around verified 27-centric topology.
- [x] 3.3 Keep known-cardinality troubleshooting sections only where they are clearly current or explicitly historical.
- [x] 3.4 Ensure commands use node-27 DB `:55432`, node-27 `/home/ghdc/nwm/...`, node-22 `/ghdc/data/nwm/...`, and public/local display health checks correctly.
- [x] 3.5 Preserve safe cross-references to `docs/governance/ROLE_BOUNDARY.md` as current physical source of truth and `docs/runbooks/two-node-deployment-overview.md` as design-intent background.
- [x] 3.6 Ensure env/DB inspection examples redact or avoid credential values.

## 4. Verification

- [x] 4.1 `! rg -n "STALE WARNING" docs/runbooks/current-production-ops.md` PASS.
- [x] 4.2 `! rg -n "55433|10\\.0\\.2\\.100:55433" docs/runbooks/current-production-ops.md` PASS; any future retained match must be explicitly historical/do-not-connect/archived/stopped rollback-only.
- [x] 4.3 `rg -n "node27_autopipe|scripts/node27_autopipe_cron.sh|scripts/node27_autopipeline.py|/home/nwm/NWM|127\\.0\\.0\\.1:55432|127\\.0\\.0\\.1:8080|https://test\\.nwm\\.ac\\.cn/health" docs/runbooks/current-production-ops.md` shows required node-27 DB/ingest/display facts.
- [x] 4.4 `! rg -n "services\\.orchestrator\\.cli plan-production|plan-production --submit|/scratch/frd_muziyao/NWM.*plan-production" docs/runbooks/current-production-ops.md` PASS; any future retained match must be explicitly historical.
- [x] 4.5 `rg -n "services\\.slurm_gateway|/ghdc/data/nwm/object-store|/home/ghdc/nwm/object-store|/ghdc/data/nwm/published|/home/ghdc/nwm/published" docs/runbooks/current-production-ops.md` shows Slurm Gateway and both NFS path perspectives.
- [x] 4.6 `rg -n "ROLE_BOUNDARY.md|two-node-deployment-overview.md" docs/runbooks/current-production-ops.md` confirms both cross-references remain.
- [x] 4.7 `! rg -n "postgresql://[^:@]+:[^@]+@" docs/runbooks/current-production-ops.md` PASS, and env examples use redaction/sanitizing commands.
- [x] 4.8 `openspec validate rewrite-current-production-ops-27-centric --strict --no-interactive` PASS.
- [x] 4.9 `openspec validate --all --strict --no-interactive` PASS.
- [x] 4.10 `corepack pnpm dlx markdownlint-cli2 "docs/**/*.md"` PASS.
- [x] 4.11 `git diff --check` PASS.
