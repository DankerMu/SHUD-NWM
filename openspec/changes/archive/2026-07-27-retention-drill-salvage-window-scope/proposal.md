# Proposal: retention-drill-salvage-window-scope

Issue: #1162 · Suggested fixture level: compact · Minimal mergeable slice: 单 PR(判定跨度一处改动 + 单测 + 三处文档对齐)

## Why

node-27 retention 链路的最后一个死锁点:`check_drill_gate` 的 db-export 腿(`scripts/node27_timeseries_retention.py:679-682`)要求 drill 的 db-export tuple 并集覆盖**整个 drop window**,而 db-export 恢复对象只存在于 #850 salvage 时代区间(实际归档止于 ~06-21);salvage 时代之后的区间由真实 product archive 覆盖,**不存在也不应存在** db-export 包。结果:任何跨 salvage/正常时代边界的 drop window 在证据齐备(drill PASS、forcing/runs 全覆盖、audit 无 blocked)时被永久拒 `DRILL_COVERAGE_DB_EXPORT_MISSING`(node-27 实测 refusal receipt 2026-07-26)。该整窗语义严于规格(`tier-node27-timeseries-storage` 的 `archive-rebuild-drill/spec.md:63-74`(修订前 :62-69)原文只写存在性)、也与同文件把 db-export tuple 并入 forcing 并集的设计意图(`:738-740`)自相矛盾。热存储在修复前持续增长(retention timer 只能保持 disabled)。

## What Changes

- **D1(判定跨度,唯一代码行为改动)**:`check_drill_gate` db-export 腿改为——driver(`_completeness_has_db_export_overlap`,verdict-agnostic)命中时,目标窗集合取 `derive_salvage_backed_windows(completeness_receipt, drop_window)`(H9 复用,函数本体字节不动),**每个目标窗先与 drop window 求交(clip)**,再逐窗要求 `_drill_covers(coverage, "db-export", <clipped>)`;任一目标窗覆盖不足 → 拒 `DRILL_COVERAGE_DB_EXPORT_MISSING`(wire code 不变)。
- **D2(verdict 口径显式处理,fail-closed)**:driver 命中但目标窗集合为空(仅有 verdict ≠ `complete` 的 db-export overlap subject)→ **显式拒** `DRILL_COVERAGE_DB_EXPORT_MISSING`,不依赖"completeness gate 先行拒绝所以不可达"的默认;该分支有专属单测。
- **D3(规格与文档对齐,三处收敛同一语义)**:
  - 本 change 的 `archive-rebuild-drill` spec delta 新增 salvage-scoped 覆盖要求(normative 落点)。
  - active change `tier-node27-timeseries-storage`:`specs/archive-rebuild-drill/spec.md:63-74` 覆盖规则句、`design.md` H2(:1895)与不变量表(:1959)的 db-export 措辞改为 salvage-scoped(orchestrator 直接编辑)。
  - `docs/runbooks/tier-node27-timeseries-storage.md`:`:1461-1463` 整窗主句、`:1473-1479` 通用句限定、`:1608-1610` §8.2 释义,并在 §7.5 补 `--salvage-manifest` 覆盖每个窗内 db-export subject 窗的 ops 后果(详见 tasks 1.3)。
- **非改动**:forcing / runs 两腿整窗并集语义、`_tuples_cover_window` 区间合并算法、`_drill_covers` 的 forcing∪db-export 参与规则、`derive_salvage_backed_windows` 本体与 receipt `salvage_backed_windows[]` 字段、检查顺序(STALE → FAIL → forcing → runs → db-export)全部字节不动。

## Risk Triage

- Level: **compact**(S 规模;单函数调用点改动 + 单测 + 文档;无新 IO/契约面缩减)。
- 风险轴:
  - 放松方向错误 = 数据丢失风险:db-export 腿从整窗收窄到 salvage 窗,若收窄过头(如漏掉多 salvage 窗中的一个、clip 算错)会让未验证恢复的区间被 drop——每个目标窗独立判定 + 缺口/多窗单测钉死。
  - fail-closed 回归:D2 空目标窗分支若写成放行,非 complete subject 情形静默通过——专属单测。
  - 文档漂移:三处表面(spec delta、tier draft、runbook)必须同语义,任一处保留"整窗"措辞即复发 #1129 类漂移。

## Must-Preserve

- `DRILL_COVERAGE_DB_EXPORT_MISSING` wire code 与 refusal receipt 形状不变;refusal 仍单 shortfall 单 code。
- forcing / runs 腿:整窗并集语义、代码路径、既有单测全部不动。
- `derive_salvage_backed_windows` 字节不动(receipt `salvage_backed_windows[]` 消费面不变;receipt 记录**原始未 clip** subject 窗、gate 判定用 clip 窗,差异是刻意的)。
- `_completeness_has_db_export_overlap` 字节不动(driver 谓词保持 verdict-agnostic,口径处理收敛在 D2 调用点)。
- 检查顺序与早退结构(每 reason 即 return)不变。
- completeness gate(H1)行为不动——D2 不是它的替代,是 drill 腿自身的 fail-closed。

## Seams Under Test(上游声明,消费不重谈)

- `check_drill_gate(receipt, completeness_receipt, drop_window, max_age_days, now)`(`scripts/node27_timeseries_retention.py:640`)— 唯一行为改动落点。
- `_drill_covers` / `derive_salvage_backed_windows` — 被复用、不被修改;clip 逻辑在 `check_drill_gate` 内部(或私有小 helper)。
- Ops oracle:node-27 实机同 cutoff `--dry-run` receipt(issue AC 末条;评审 clean 后、合并前经临时 worktree 执行,主工作树保持 master)。

## Review Packs

- Selected: **contract**(ops gate 的 refusal 语义是 runbook/receipt 消费契约;三处文档必须字节收敛)、**test-integrity**(本 loop 连续多单 recurring class 为 test-oracle 强度;既有断言迁移清单为**空**——两条相邻既有测试经核验零修改保持绿,任何改动即语义弱化须单独举证)。
- Not selected: security/performance(无新输入面、每次 gate 一次列表遍历)、migration(receipt 形状不变)。

## Evidence Mapping(AC → 交付物)

| Issue AC | 交付物 |
|---|---|
| db-export 腿只对 salvage-backed 窗(∩ drop)要求覆盖;forcing/runs 不变 | D1 + tasks 2.1/2.6 |
| 混合窗放行回归锚点 | tasks 2.1(红前证据) |
| salvage 子区间缺口仍拒 | tasks 2.2 |
| 无 db-export subject 不要求(既有测试保持绿) | tasks 2.5 |
| verdict 非 complete 口径显式覆盖 | D2 + tasks 2.4 |
| runbook §8.2 / design H2 / spec 文本对齐 | D3 + tasks 1.3/3.4 |
| pytest / ruff / openspec validate | tasks §3 |
| node-27 同 cutoff dry-run receipt(放行,或"正确拒绝 + 补 drill 后放行"双 receipt) | tasks 4.1 双分支(评审 clean 后合并前) |

## Non-Goals

- 不动 forcing/runs 腿与区间合并算法;不做 spec 字面的"存在性"弱化(放弃跨度校验)。
- 不删除独立 db-export 腿(备选 B 的证据降级已被 issue 裁定不取)。
- 不启用 `nhms-node27-timeseries-retention.timer`(ADR 0002 gated enforce,超出本 issue)。
- freed_bytes 统计(#1125)、#1158 已修的 containment 语义不触碰。
