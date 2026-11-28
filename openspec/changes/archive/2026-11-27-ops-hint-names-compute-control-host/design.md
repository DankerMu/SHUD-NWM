# Design: the recovery hint on `/ops` names the compute control host

## Context

The umbrella change `sweep-compute-role-narration` gives the new hint in `design.md` §5
("…交给计算控制主机处理…") and says of the probe: its main test is the element
`ops-manual-recovery-guidance`, which does not change; its fallback text follows the fake page; the
probe passes on the old and on the new page, so the C4 live receipt can be taken at any time after
the deployment. Task 8.2 assigns the whole of `display-readonly-ops-ui` to this slice.

## Decisions

### D1. The texts

| Where | Today | New |
|---|---|---|
| `JobsTable.tsx`, `display_readonly` | 失败诊断用于交给 22 compute-control 节点处理；在 27 display_readonly 页面只做只读查看和本地通知标记，不写入数据库或审计 API。 | 失败诊断用于交给计算控制主机处理；在 node-27 display_readonly 页面只做只读查看和本地通知标记，不写入数据库或审计 API。 |
| `JobsTable.tsx`, other | 失败诊断用于交给 22 compute-control 节点处理；当前运维页面复制诊断和本地通知标记不写入数据库或审计 API。 | 失败诊断用于交给计算控制主机处理；当前运维页面复制诊断和本地通知标记不写入数据库或审计 API。 |
| `BasinFailures.tsx`, `display_readonly` | 阶段失败诊断用于交给 22 compute-control 节点处理；27 display_readonly 页面只复制只读证据，不写入数据库或审计 API。 | 阶段失败诊断用于交给计算控制主机处理；node-27 display_readonly 页面只复制只读证据，不写入数据库或审计 API。 |
| `BasinFailures.tsx`, other | 阶段失败诊断用于交给 22 compute-control 节点处理；当前运维页面复制诊断不写入数据库或审计 API。 | 阶段失败诊断用于交给计算控制主机处理；当前运维页面复制诊断不写入数据库或审计 API。 |
| fake page | 失败诊断用于交给 22 compute-control 节点处理；需要恢复时在 22 compute-control 节点按恢复 runbook 处理。 | 失败诊断用于交给计算控制主机处理；需要恢复时在计算控制主机按恢复 runbook 处理。 |
| probe fallback | `includes('需要恢复时在 22 compute-control')` | `includes('需要恢复时在计算控制主机')` |
| comments | "the node-22 manual-repair marker lane" / "the node-22 marker lane" / "恢复走 node-22 manual-repair marker 车道" | "the manual-repair marker lane of the compute role" / "the marker lane of the compute role" / "恢复走计算角色的 manual-repair marker 车道" |

The hint names the host because it says where the diagnosis goes to be acted on: an operator works
on one machine. The comments name the role because they say whose lane it is. The API text of slice
8.1 makes the same split.

"27 display_readonly 页面" → "node-27 display_readonly 页面": "27" is the same kind of shorthand as
"22", in the same sentence of two of the four variants. `JobsTable.test.tsx` asserts
`toHaveTextContent('27 display_readonly')` for that variant, which the old and the new text both
satisfy: as it stands it pins neither the new name nor "node-27". It becomes
`toHaveTextContent('node-27 display_readonly')` with a second assertion for "计算控制主机". The
`display_readonly` variant of `BasinFailures` has no test at all, and it is the one the production
page shows (`MonitoringPage.tsx` passes `isDisplayReadonly` to both components): `StageCard.test.tsx`
gains a case with `diagnosticsDisplayReadonly` that asserts the same two texts. The assertions of
the other variant, `not.toHaveTextContent(/display_readonly|27/)`, hold as before.

### D2. The probe

`inspectC4OpsInPage` sets `operatorRecoveryVisible` when the element
`[data-testid="ops-manual-recovery-guidance"]` exists, or when a `role="status"` element contains
the fallback text. The production page renders the element (`JobsTable.tsx`), so:

- the probe built from this change passes on the page node-27 serves today (the element, old text);
- the probe passes on the page of this change (the element, new text);
- the fallback text is reached only on a page without the element, and then it must be the text of
  this change: a page that old has no reason to pass a new probe.

No existing test states the first two. The slice adds three unit tests, one per case so that each
fails or passes on its own: the element alone, with no text (the probe does not read the element's
text, so this stands for the deployed page and for the new one); a `role="status"` element carrying
the new fallback sentence and no element; neither (not recognised).

### D3. The spec deltas are MODIFIED blocks

Two requirements change by sentences; no scenario is dropped or renamed. Each MODIFIED block is the
whole requirement generated from the main spec's text of the day with asserted substitutions:

| Requirement | Today | New |
|---|---|---|
| Ops diagnostic copy, body | … can copy for manual 22-node recovery. | … can copy for manual recovery on the compute control host. |
| Ops diagnostic copy, scenario "Manual recovery guidance" | the page includes guidance to follow the 22 compute-control recovery runbook | the page includes guidance to follow the recovery runbook on the compute control host |
| same scenario | any suggested commands are clearly tied to 22-node execution, not 27. | any suggested commands are clearly tied to execution on the compute control host, not on node-27. |
| Display ops consumes published logs, scenario "Log modal success" | it does not expose instructions to open 22 private filesystem paths from the 27 browser. | it does not expose instructions to open private filesystem paths of the compute hosts from a browser viewing the node-27 page. |

The last line is about paths (`/scratch/…`), which exist on both compute hosts. After
`openspec archive` the two requirements in the main spec are compared with the generated blocks
byte for byte. The Purpose line of the spec has no codename and stays.

### D4. The live receipt follows the merge

node-27 serves the old page until the frontend is rebuilt and swapped; that is a production action
of slice 8.3 and needs the owner's go-ahead. This PR is verified by the frontend unit tests, the
type check and the mocked Playwright lane (locally the spec file this slice touches, in CI every
shard), and records the deviation.

## Risks

- **Another consumer of the hint text.** Searched the repository for "22 compute-control",
  "交给 22" and "需要恢复时在": the two components, the three assertions, the probe, the fake page,
  the spec and the glossary's list of retired spellings. No e2e spec asserts the hint.
- **The probe stops recognising the production page.** It keys on the element; D2's new test
  states it.
- **A mocked Playwright spec reads the hint.** None does (the hits in `e2e/` are comments); the
  mocked lane runs in CI in four shards.
