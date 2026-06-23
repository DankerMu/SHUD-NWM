# ADR 格式

ADR（Architecture Decision Record）放在 `docs/adr/`，连续编号：`0001-slug.md`、`0002-slug.md`，以此类推。

**懒创建** `docs/adr/`：只有第一个 ADR 确实需要时才创建该目录。

## 与 OpenSpec design.md 的分工

- `openspec/changes/<name>/design.md` 记**某次 change 的**技术决策（选型理由、备选、风险），随该 change 归档。
- ADR 记**项目级、跨 change 长期有效**的决策——它是长期账本，不随单次 change 消失。
- 同一决策若已在 `design.md` 充分说明、且只作用于该 change，**不必**再开 ADR。只有当决策会约束未来多个 change 时，才升级为 ADR。

## 模板

```md
# {决策的简短标题}

{1-3 句：背景是什么、我们决定了什么、为什么。}
```

就这些。一个 ADR 可以只有一段。价值在于记录**做出了**某决策以及**为什么**，而不是填满各个 section。

## 可选 section

只在带来真实价值时包含；多数 ADR 不需要。

- **Status** frontmatter（`proposed | accepted | deprecated | superseded by ADR-NNNN`）——决策被重新审视时有用
- **Considered Options**——只有被拒方案值得记住时才写
- **Consequences**——只有需要说明非显而易见的下游影响时才写

## 编号

扫描 `docs/adr/`，取现有最大编号加一。

## 何时提议 ADR（三门槛，必须全部为真）

1. **难回退** — 之后改主意的成本有意义
2. **无背景会困惑** — 未来读者看到代码会想"为什么这样做？"
3. **真实权衡的结果** — 确实存在可选方案，且你基于具体原因选择了其中一个

三者缺一就跳过：容易撤销的决策直接撤；不出人意料的没人会追问；没有真实替代方案的除了"做了显而易见的事"无可记录。

### 哪些够格

- **架构形态**：如"采用 monorepo"、"写模型 event-sourced，读模型投影到 Postgres"。
- **context 间集成模式**：如"Ordering 与 Billing 通过 domain events 而非同步 HTTP 通信"。
- **带锁定的技术选型**：数据库、message bus、auth provider、部署目标——不是每个 library，只记替换起来要花一个季度的那些。
- **边界与范围决策**：如"Customer 数据由 Customer context 拥有，其他 context 只通过 ID 引用"。明确的"不做"和"做"一样有价值。
- **刻意偏离显而易见路径**：如"不用 ORM 而用手写 SQL，因为 X"——防止下一个工程师把刻意选择"修掉"。
- **代码里看不到的约束**：如"因合规不能用 AWS"、"因 partner API 合约，响应须低于 200ms"。
- **非显而易见的被拒方案**：考虑过 GraphQL 但因细微原因选 REST，就记下来；否则半年后又有人提 GraphQL。
