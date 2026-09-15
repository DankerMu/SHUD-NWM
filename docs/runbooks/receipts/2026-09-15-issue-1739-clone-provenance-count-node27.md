# #1739 live receipt — `hydro.state_snapshot` clone-provenance census (node-27)

Purpose: #1739 验收项「node-27 live 计数 receipt」——放宽 DB 面
`get_earliest_clone_row_for_model_source` 的谓词（去掉 `clone_gate_fingerprint
IS NOT NULL`）之前，先确认现网存量里是否真的存在「有 `cloned_from_model_id`、
无 `clone_gate_fingerprint`」的半写行。

## 采集

- 节点：node-27（`ssh -p 32099 nwm@210.77.77.27`），active primary PG :55432，
  容器 `nhms-db`，`psql -U nhms -d nhms`。
- 时间（UTC）：`2026-09-15T04:54:06Z`
- node-27 仓库 head：`415cbd1e`
- 只读：单条 `SELECT ... FROM hydro.state_snapshot`，无写入、无 DDL。

```sql
SELECT
  count(*)                                                                 AS total_rows,
  count(*) FILTER (WHERE cloned_from_model_id IS NOT NULL)                 AS with_parent,
  count(*) FILTER (WHERE cloned_from_model_id IS NOT NULL
                     AND clone_gate_fingerprint IS NULL)                   AS half_written_no_fingerprint,
  count(*) FILTER (WHERE clone_gate_fingerprint IS NOT NULL
                     AND cloned_from_model_id IS NULL)                     AS fingerprint_without_parent,
  count(*) FILTER (WHERE cloned_from_model_id IS NOT NULL
                     AND btrim(cloned_from_model_id) = '')                 AS whitespace_only_parent,
  count(*) FILTER (WHERE cloned_from_model_id = model_id)                  AS self_referential
FROM hydro.state_snapshot;
```

## 结果

| total_rows | with_parent | half_written_no_fingerprint | fingerprint_without_parent | whitespace_only_parent | self_referential |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 0 | 0 | 0 | 0 |

## 读法（这条 receipt 证明了什么、没证明什么）

- **证明**：现网 `hydro.state_snapshot` **整表为空**，因此去掉 SQL 里的
  `clone_gate_fingerprint IS NOT NULL` 对存量数据的影响恰好为零——没有任何一行
  的选中/落选结果会改变。
- **没有证明**：这不是「表里有很多行、其中零行半写」那种强 oracle。零行意味着
  该谓词在现网从未被真实数据行使过，所以本次 receipt 对「半写行在实践中是否会
  出现」这个经验问题给不出任何信息。#1739 的裁定因此靠的是契约论证
  （`clone_gate_fingerprint` 为纯 provenance、spec 从未把它列为血缘准入条件），
  而不是靠这条计数。
- `self_referential = 0` 与 `whitespace_only_parent = 0` 同理：两项各自对应
  #1741 的写入侧自克隆污染与 #1739 留下的空白串跨面分叉，现网都无存量样本，
  因此两者都是纵深防御补洞，不是在燃事故。
