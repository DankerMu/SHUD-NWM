# Design: the retry refusal and two OpenAPI descriptions name the compute role

## Context

The umbrella change `sweep-compute-role-narration` fixes the vocabulary (`design.md` §1) and lists
this slice in §5: "retry 409 message and the two `suggested_action` texts of the control-plane
refusal" and "two OpenAPI description chains, each patching code → `openapi/nhms.v1.yaml` →
generated types". This document says which term each sentence takes, how the generated chain moves,
and what is checked.

## Decisions

### D1. The term follows what the sentence is about

| Text | Today | New | Why this term |
|---|---|---|---|
| retry message | "Control-plane retry is a manual node-22 action; this API does not submit retries." | "Control-plane retry is a manual action on the compute control host; this API does not submit retries." | it says where the action is done: an operator runs the script on one machine |
| suggested action, retry | "Ask a node 22 operator to record a manual repair for this run with scripts/node22_manual_retry_failed_runs.py (FileJournalRetryService.record_manual_repair); the scheduler mints the retry." | "Ask an operator of the compute role to record a manual repair …" (the rest unchanged) | it says whom to ask |
| suggested action, fallback branch (no caller since #3394 removed the cancel route; both callers pass `"retry"`) | "Ask a node 22 operator to handle this control-plane action from the compute_control runbook." | "Ask an operator of the compute role to handle this control-plane action from the compute_control runbook." | whom to ask |
| description of `retryRun` | "… Failed-run recovery is the node-22 manual-repair marker lane (scripts/node22_manual_retry_failed_runs.py, runbook node22-control-plane-manual-recovery)." | "… Failed-run recovery is the manual-repair marker lane of the compute role (scripts/…, runbook …)." | the lane belongs to the role |
| description of `ControlPlaneManualActionRequired` | "… the authorized actor must perform the action from the compute_control runbook on node 22." | "… from the compute_control runbook on the compute control host." | where the action is done |
| `ops-monitoring-api`, retry requirement | "(the node-22 manual-repair marker lane `FileJournalRetryService.record_manual_repair`, driven by …)" | "(the manual-repair marker lane of the compute role, `FileJournalRetryService.record_manual_repair`, driven by …)" | as the description |
| `ops-monitoring-api`, Purpose | "… the retry endpoint that fails closed to a manual node-22 action." | "… fails closed to a manual action on the compute control host." | as the message |

The message and the response description name the host, as the UI text of slice 8.2 does
("交给计算控制主机处理").

Which API returns which text: a `display_readonly` API answers the retry with its own message
("Control-plane retry requires manual action in display_readonly mode.", unchanged) and the retry
suggestion; a `compute_control` or `dev_monolith` API answers with the reworded message and the
same suggestion. The only API deployed today is the display API on node-27 (`compute-api` was
retired by #3394), so in production the reworded message is served nowhere; what changes there is
the suggestion and the two descriptions.

### D2. Names stay

`scripts/node22_manual_retry_failed_runs.py`, `node22-control-plane-manual-recovery`,
`compute_control`, `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and every key of the response are
identifiers that callers, probes and runbooks read. The scenario "Compute-control retry returns the
stable manual-action error" pins two of them and is not touched.

### D3. The generated chain moves by hand at one link and by a tool at the next

- `openapi/nhms.v1.yaml` has no generator. `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema`
  loads it and compares it with `app.openapi()` as data, so the two descriptions are edited in the
  YAML to the same strings the patching module produces; the line folding of the YAML is free.
- `apps/frontend/src/api/types.ts` is generated: `pnpm generate:api` writes it and
  `pnpm check:api-types` (CI) regenerates and diffs. It is never edited by hand.

### D4. The spec delta is a MODIFIED block

Only one sentence of the requirement body changes; no scenario is dropped or renamed, so a MODIFIED
block with the whole requirement (body and seven scenarios, generated from the main spec's text of
the day) is enough. After `openspec archive` the requirement in the main spec is compared with the
generated block byte for byte. The Purpose line is not reachable by a delta and is edited in the
main spec in the same push.

### D5. The live receipt follows the merge

The display API on node-27 serves the old suggestion and the old two descriptions until it is
restarted (the reworded message is not among what it serves, see D1). The restart is a production
action and belongs to slice 8.3, which needs the owner's go-ahead. This PR is verified by the drift
test, the contract tests and the route test locally and on node-27 (a temporary worktree, no
service touched), and records the deviation.

## Risks

- **A client or probe compares the message text.** Searched: no test, script, probe or frontend
  module compares any of the five texts except the one test of this change (the retry message
  itself is pinned by no test: its evidence is the printed text); the deny-write probes
  read the status and the code. The frontend shows its own hint (slice 8.2), not the API's.
- **The YAML and the runtime schema drift apart.** The drift test fails on any difference; it is in
  the Evidence Floor with a red run (the code changed, the YAML not yet).
- **The gate.** `openspec/` is inside the topology gate's scanned set; the new sentences put the
  role term beside "operator", "runbook" and "lane", not beside a database word. The hard gate is
  compared before and after.
