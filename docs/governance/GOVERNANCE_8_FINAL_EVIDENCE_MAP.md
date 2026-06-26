# Governance-8 Final Evidence Map

Snapshot date: 2026-06-26

Scope: Governance-8 issue #770 final evidence document for
`governance-8-module-deepening`. This file records the task-to-issue-to-PR map
for all Governance-8 implementation slices and the final local verification
evidence. It is documentation-only; it does not change runtime behavior,
compatibility surfaces, production topology, Slurm behavior, display-readonly
capabilities, or station-MVT status.

## Authority

The OpenSpec source is
`openspec/changes/governance-8-module-deepening/`. Group inventories remain the
authority for detailed owner/facade and lane contracts:

- `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`
- `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`
- `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`
- `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`
- `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md`
- `docs/governance/entropy-burndown-triage.md`

When this summary disagrees with executable behavior, source code and tests win.
When it disagrees with the OpenSpec change, the OpenSpec change wins.

## Final Task Map

| Task | Issue | PR | Evidence authority |
|---|---:|---:|---|
| 1.1 Scheduler compatibility guard and parity fixture | #712 | #771 | Scheduler inventory guard metadata and focused scheduler parity verification. |
| 1.2 Scheduler state owner-family completion | #713 | #772 | Scheduler state owner/facade re-export and wrapper parity coverage. |
| 1.3 Scheduler lease owner-family completion | #714 | #773 | Scheduler lease compatibility lookup and re-export coverage. |
| 1.4 Scheduler discovery owner-family completion | #715 | #774 | Discovery alias, forwarder, and backfill selection parity coverage. |
| 1.5 Scheduler candidate-construction owner-family completion | #716 | #775 | Candidate construction alias, forwarder, and construction parity coverage. |
| 1.6 Scheduler execution/cohort owner-family completion | #717 | #776 | Execution wrapper and restart cohort parity coverage. |
| 1.7 Scheduler evidence-write and proof owner-family completion | #718 | #777 | Evidence direct alias, forwarder, wrapper, and proof coverage. |
| 1.8 Scheduler cancellation/status proof local-glue closure | #719 | #778 | Cancellation/status proof wrappers and retained local glue classification. |
| 1.9 Scheduler group verification and evidence closeout | #720 | #779 | Scheduler group closeout table in scheduler compatibility inventory. |
| 2.1 Chain compatibility guard and parity fixture | #721 | #780 | Chain facade guard metadata and compatibility-facade growth checks. |
| 2.2 Chain stage catalog/type owner-family completion | #722 | #781 | Stage catalog/type re-export identity and owner/facade map coverage. |
| 2.3 Chain stage execution owner-family completion | #723 | #782 | Stage-execution forwarder and dependency-field compatibility coverage. |
| 2.4 Chain array-accounting owner-family completion | #724 | #783 | Array-accounting wrapper, dependency binding, and monkeypatch-seam coverage. |
| 2.5 Chain manifest owner-family completion | #725 | #784 | Manifest alias/wrapper/method maps and quality-state monkeypatch coverage. |
| 2.6 Chain reservation owner-family completion | #726 | #785 | Reservation alias, reserve/bind wrappers, local idempotency-key glue, and durable reservation coverage. |
| 2.7 Chain retry owner-family completion | #727 | #786 | Retry alias, constructor seam, chain-local bridge, and scheduler factory coverage. |
| 2.8 Chain tile-publisher owner-family completion | #728 | #787 | Tile-publisher alias/function identity, local publish monkeypatch, and redaction coverage. |
| 2.9 Chain worker/source-identity and time-consistency owner-family completion | #729 | #788 | Worker/source identity aliases and fail-closed time-consistency coverage. |
| 2.10 Chain persistence/repository ownership decision and extraction/retention | #730 | #789 | Persistence primitive aliases and retained chain-local repository classifications. |
| 2.11 Chain group verification and evidence closeout | #731 | #790 | Chain group closeout table in chain compatibility inventory. |
| 3.1 Shared two-node evidence contracts | #732 | #791 | Shared contract guard metadata and strict identity coverage. |
| 3.2 Metadata and strict-identity lane extraction | #733 | #792 | Metadata/source-scope seeding and owner row coverage. |
| 3.3 Docker preflight lane extraction | #734 | #793 | Docker preflight owner row and resource/path guard coverage. |
| 3.4 Docker security lane extraction | #735 | #794 | Docker security, child artifact, display readonly, and capability guard coverage. |
| 3.5 Readonly DB lane extraction | #736 | #795 | Readonly DB route identity, source artifact, and no-write proof coverage. |
| 3.6 Simple live lane helper and Slurm/compute/display lanes | #737 | #796 | Simple-live Slurm, compute summary, and display summary lane coverage. |
| 3.7 API proof lane extraction | #738 | #797 | API proof lane owner and required-check coverage. |
| 3.8 Browser proof lane extraction | #739 | #798 | Browser proof lane owner and source-switch/job identity coverage. |
| 3.9 Logs lane extraction | #740 | #799 | Logs lane owner, published log URI, unavailable proof, and redaction coverage. |
| 3.10 Manual ops lane extraction | #741 | #800 | Manual ops lane owner, fail-closed/no-side-effect, and receipt artifact coverage. |
| 3.11 Cross-plane and source-scope aggregation extraction | #742 | #801 | Cross-plane/source-scope aggregation guard coverage. |
| 3.12 Two-node final aggregation extraction | #743 | #802 | Final aggregation output safety, redaction, and status-ordering coverage. |
| 3.13 Two-node group verification and evidence closeout | #744 | #803 | Two-node final implementation evidence map in lane inventory. |
| 4.1 Readiness item contract extraction | #745 | #804 | Readiness item contract owner and status/mode validation coverage. |
| 4.2 Shared artifact writers extraction | #746 | #805 | Shared artifact writer, preflight/environment payload, redaction, and safe-write coverage. |
| 4.3 Shared live-proof loader and receipts artifact extraction | #747 | #806 | Proof env/file loading, bounded no-follow reads, and receipts artifact coverage. |
| 4.4 Dependency summary reader extraction | #748 | #807 | Dependency summary owner, safe discovery/read, issue/schema/status/run-id coverage. |
| 4.5 Scheduler evidence reader extraction | #749 | #808 | Scheduler evidence owner, root/file discovery, schema/status/count/identity coverage. |
| 4.6 Proof-specific live validators extraction | #750 | #809 | Auth, alert, rollback, and target-environment proof validator coverage. |
| 4.7 Dependency live-proof binder extraction | #751 | #810 | Dependency proof alias/provenance binding and consumed-summary matching coverage. |
| 4.8 Scheduler live-proof binder extraction | #752 | #811 | Scheduler proof producer binding and live-eligible status/mode coverage. |
| 4.9 Scoped exclusion extraction | #753 | #812 | Scoped exclusion items, public exclusion artifacts, and non-blocker semantics coverage. |
| 4.10 Readiness final aggregation extraction | #754 | #813 | Final aggregation summary/release-blocker semantics and facade coverage. |
| 4.11 Readiness group verification and evidence closeout | #755 | #814 | Readiness group closeout table in readiness lane inventory. |
| 5.1 API OpenAPI patch owner-module extraction | #756 | #815 | `apps/api/openapi_patching.py` owner evidence and OpenAPI/API type checks. |
| 5.2 API role-aware route registry extraction | #757 | #816 | `apps/api/route_registry.py` owner evidence and runtime-role route coverage. |
| 5.3 API static/health/cache/startup wiring extraction | #758 | #817 | `apps/api/startup_wiring.py` owner evidence and static/health/cache coverage. |
| 5.4 API protected mutation seam retention and tests | #759 | #818 | Retained protected-mutation seam tests and no-downstream-write coverage. |
| 5.5 API group verification and evidence closeout | #760 | #819 | API bootstrap closeout table in structural inventory. |
| 6.1 M11 pure map builders extraction | #761 | #820 | `m11MapBuilders.ts` owner evidence and focused M11 builder tests. |
| 6.2 M11 MapLibre primitive extraction | #762 | #821 | `m11MapPrimitives.tsx` owner evidence and source/layer primitive tests. |
| 6.3 M11 interaction dispatch extraction | #763 | #822 | `m11MapInteractions.ts` owner evidence and click/hover priority tests. |
| 6.4 M11 camera and map-error helper extraction | #764 | #823 | `m11MapRuntime.tsx` owner evidence and camera/error/status tests. |
| 6.5 M11 popup and selection boundary stabilization | #765 | #824 | `m11MapSelection.tsx` owner evidence and selected/popup tests. |
| 6.6 Frontend group verification and evidence closeout | #766 | #825 | Frontend map surface closeout table in structural inventory. |
| 7.1 Inventory synchronization evidence | #767 | #826 | Governance-8 inventory synchronization table in structural inventory. |
| 7.2 Report-only entropy audit deltas | #768 | #827 | Governance-8 Module Deepening Delta in entropy burndown triage. |
| 7.3 Final local verification gate | #769 | #828 | Governance-8 Final Local Verification Gate in entropy burndown triage. |
| 7.4 Final implementation evidence map | #770 | #829 | This final task map and non-goal summary. |

## Final Local Verification Summary

The final local verification gate is recorded in
`docs/governance/entropy-burndown-triage.md` under
`Governance-8 Final Local Verification Gate`.

| Verification | Result |
|---|---|
| `uv run ruff check .` | PASS |
| Scheduler/backend pytest chunk | PASS, 641 tests |
| Chain pytest chunk | PASS, 239 tests, 7 skipped |
| Two-node/readiness pytest chunk | PASS, 1181 tests, 2 skipped |
| API pytest chunk | PASS, 240 tests |
| `cd apps/frontend && pnpm test` | BLOCKED before Vitest by local `ERR_PNPM_IGNORED_BUILDS` for `esbuild@0.25.12` |
| `cd apps/frontend && pnpm build` | BLOCKED before Vite by local `ERR_PNPM_IGNORED_BUILDS` for `esbuild@0.25.12` |
| `cd apps/frontend && corepack pnpm test` | PASS, 34 files / 616 tests |
| `cd apps/frontend && corepack pnpm build` | PASS |
| `openspec validate --all --strict --no-interactive` | PASS, 184 items |
| `openspec validate governance-8-module-deepening --strict --no-interactive` | PASS |
| `npx --yes markdownlint-cli2 "docs/**/*.md"` | PASS, 117 files, 0 errors |
| `git diff --check` | PASS |

## Explicit Remaining Non-Goals

- No station-MVT backend endpoint closure or station-MVT live evidence claim.
- No production topology change and no node-22/node-27 role migration.
- No Slurm scheduling behavior change.
- No display-readonly capability expansion.
- No API route behavior change, generated API type change, DB/schema change, or
  frontend product behavior change.
- No `.entropy-baseline/latest.json` write and no entropy hard-gate enablement.
- No compatibility export removal unless a future inventory-backed caller
  migration issue owns it.

## Review Checklist For #770

- Every Governance-8 subtask from 1.1 through 7.4 has an issue and PR mapping.
- Every completed owner group has an inventory authority or closeout table.
- Every final verification command has a recorded result.
- Non-goals remain explicit, especially station-MVT, production topology, Slurm
  behavior, and display-readonly expansion.
