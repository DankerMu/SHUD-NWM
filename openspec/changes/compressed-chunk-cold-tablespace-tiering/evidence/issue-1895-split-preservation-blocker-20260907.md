# #1895 split preservation blocker — 2026-09-07

## Actual state

C4 前置 #2123 已创建，fixture/strict/alignment 通过。C4 的 58 个文件已保存为本地提交 `0ed538ba4023df6950ed654de807133a24936753`；这不是 frozen implementation approval，也尚未 push/开 PR。

剩余 readiness 的 76 个路径已显式 stage。保存提交被 large-file-guard 的 PreToolUse hook 拒绝，未发生 commit，未切换分支。所有 staged 工作仍保留。没有修改 guard/exclude、没有绕过 hook，没有 stash/reset/checkout/clean，没有 node-27/node-22 访问或 live 执行。

## Confirmed diagnosis

只读 implementer `a09d72a7006a55c2e` 返回以下诊断；没有改文件或运行测试。

| File | HEAD lines | Staged lines |
| --- | ---: | ---: |
| services/production_closure/readonly_db_validation.py | 3450 | 3413 |
| tests/test_readonly_db_validation.py | 2348 | 2365 |
| tests/test_issue1895_readiness_c1_c2_c3.py | new | 1212 |
| tests/test_issue1895_readiness_performance_live.py | new | 1101 |
| tests/test_issue1895_readiness_storage.py | new | 1032 |

这些路径不在 `.large-file-guard.json` 的 exclude 内。guard 检查 staged 完整文本，历史文件已有超限不代表本次 restage 可以豁免。

canonical validator 当前 staged 的 24 add/61 del 中，诊断认定唯一生产行为变化是 EvidenceWriter 的 `mode=0o600`；其余为格式变化。为提交这一行为而拆整个历史模块将扩大变更范围。另有 `tests/test_issue1895_readiness_performance_publication.py:301` EOF 多余空行，独立于大文件门，尚未修复。

## Candidate remediation（诊断时尚未批准或实施）

诊断建议 18 文件提取：新增 5 个 production sibling（types、probe adapter、permission probes、merge、route smoke），canonical 保持 facade；新增 5 个测试分区；修改 canonical、4 个原测试、publication EOF、selector 与 selector tests。

关键兼容约束：保留 canonical 的公开/既有私有导出和 `run_display_route_smoke` 全局查找，保留 `importlib`/`psycopg2` monkeypatch 接口；测试搬迁不能减少收集与断言；selector 对新分区须保留完整路由。诊断方案未通过实施、测试或独立 review，不能称已证明行为等价。

这不是 C4 功能修复，也不只是 WIP 保存；它涉及历史生产模块和测试结构的显著重构。普通大范围 review 循环仍被父 retro 的 split 纠正动作替代，不能借门控失败重新启动未拆大树循环。

## Decision boundary

暂停源代码变更和分支切换，等待用户明确授权上述行为保持型历史模块拆分，或给出仓库门控的经批准处置。没有将 merge 预授权推定为 guard 豁免或无界重构授权。当前不采用关闭 hook、添加非 generated/vendor/data 豁免或 git plumbing 绕过等办法。

## 用户授权与实施边界

用户随后明确回复“授权你执行”，授权上述行为保持型历史模块与测试拆分。此授权不是 guard 豁免，也不改变 C4/readiness/live 的先后顺序。由 implementer 按诊断 allowlist 实施，保留所有测试/接口，按节点纪律验证，记录范围偏离与实际证据；不可把诊断可行性当验证通过。

目标：五个超限文件及新增分区均不超过 1000 行；selector 两个既有豁免文件不靠压缩格式缩行。允许新增 `readonly_db_{types,probe_adapter,permission_probes,merge,route_smoke}.py` 和五个测试分区；仅修改诊断列出的 canonical、原测试、publication EOF、selector/selector tests。不改 C4、hook、exclude 或未列入范围的 readiness 逻辑。

兼容不变量：原 canonical 导出继续可用；既有 route smoke/importlib/psycopg2 patch seam 继续控制实际执行；`mode=0o600` 与 C2 full-scope 断言保留；全部既有测试场景搬迁而非删除；source/CLI/changed-test 的 CI 路由覆盖全部测试分区。

证据：实施前后 AST 测试清单及测试体对照、公共导出/依赖接线检查、每文件行数、目标 Ruff/diff check；涉及执行的后端测试仍按仓库 node-27 oracle 纪律，不因本次授权提前访问远端，也不把静态检查或历史测试声称新结构的运行验证。尚未实施或验证的项保持未完成。
