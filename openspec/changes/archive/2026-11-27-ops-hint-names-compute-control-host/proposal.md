# The recovery hint on `/ops` names the compute control host (#3424)

Slice 8.2 of `openspec/changes/sweep-compute-role-narration` (#3345). Fixture level: expanded (text a
user reads on the production page, and the fallback text of the probe that produces the C4 live
receipt of that page). Risk packs: Deployment / live evidence - selected (node-27 serves the old
page until the frontend is rebuilt and swapped, which is slice 8.3 and needs the owner's go-ahead;
the probe must pass on the old page and on the new one). UI text asserted by tests - selected (three
assertions state the old words). Legacy compatibility - selected (the element id the probe keys on,
and the shape of the receipt, do not change). Others - not selected.

## Why

When a job or a stage has failed, `/ops` shows a hint that says the diagnosis is handed to "22
compute-control 节点". "22" was the short name of the host that carried the compute role until
2026-11-23; the role now runs on two other hosts and an operator acts on one of them,
the compute control host. The glossary lists "22 compute-control", "22-node" and "22 节点" as retired spellings.
The spec `display-readonly-ops-ui` says the same words in two requirements.

## What Changes

- **The hint** (`JobsTable.tsx`, `BasinFailures.tsx`, the `display_readonly` variant and the other
  variant of each): "交给 22 compute-control 节点处理" becomes "交给计算控制主机处理". In the two
  `display_readonly` variants "27 display_readonly 页面" becomes "node-27 display_readonly 页面"
  (the same shorthand for the other host, in the same sentence).
- **The assertions** follow and every variant gets one: the three that state the old words
  (`JobsTable.test.tsx`, two; `StageCard.test.tsx`, one) expect "计算控制主机"; the assertion of the
  `display_readonly` variant of `JobsTable` also expects it and "node-27 display_readonly"; a new
  case in `StageCard.test.tsx` covers the `display_readonly` variant of `BasinFailures`, which no
  test reached (it is the variant node-27 shows).
- **The C4 probe** (`src/lib/c4DisplayEvidence/dom.ts`): it recognises the hint by the element
  `ops-manual-recovery-guidance`, which does not change, and otherwise by a text; that fallback text
  becomes "需要恢复时在计算控制主机". **The fake page** (`src/test/c4DisplayFakePage.ts`) carries the
  new sentence.
- **Three new unit tests** of the probe: a page that carries only the element, whatever its text,
  is recognised (that is the page node-27 serves today and the page of this change); so is a page
  that carries only the new fallback text; a page with neither is not.
- **Four comment lines** that narrate the codename (`JobsTable.tsx`, `e2e/monitoring.mocked.spec.ts`
  twice, `e2e/support/monitoring.mocked.ts`) name the compute role.
- **`display-readonly-ops-ui`**: the requirement "Ops diagnostic copy" (its body and two lines of
  the scenario "Manual recovery guidance") and one line of "Display ops consumes published logs"
  name the compute control host, the compute hosts and node-27.

## What does not change

- The element id `ops-manual-recovery-guidance` and `ops-stage-manual-recovery-guidance`, the
  observation `operatorRecoveryVisible`, the receipt's key `operator_recovery_visible`.
- What the hint says besides the name: nothing is written to the database or the audit API.
- Generated API types (slice 8.1) and what node-27 serves (slice 8.3).

## Impact

- Frontend: two components (one of them also by a comment), the probe module, the fake page, three
  test files, two comment-only files under `e2e/`. No API call, route or state changes.
- Specs: `display-readonly-ops-ui`, two requirements (four lines).
