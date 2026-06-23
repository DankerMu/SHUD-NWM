# openspec/glossary.md 格式

项目级 ubiquitous language 的单一来源。每个项目一份，位于 `openspec/glossary.md`。

## 结构

```md
# Glossary

{一两句话说明本项目领域是什么。}

## Language

**Order**:
客户提交的一次购买请求。
_Avoid_: Purchase, Transaction

**Invoice**:
交付后向客户发出的付款请求。
_Avoid_: Bill, Payment request

**Customer**:
下单的个人或组织。
_Avoid_: Client, Buyer, Account
```

## 规则

- **Be opinionated。** 多个词表示同一概念时，选最合适的作为 canonical term，其余列入 `_Avoid_`。
- **定义紧凑。** 最多一两句。定义它**是什么**，而不是它**做什么**。
- **只收本项目特有术语。** 通用编程概念（timeout、error type、utility pattern）不属于这里，即使项目大量使用。加术语前先问：这是本项目领域独有的概念，还是通用编程概念？只有前者属于这里。
- **自然成簇时用子标题分组**；若所有术语属于同一内聚区域，扁平列表即可。
- **不混实现细节。** glossary 只是术语表，不承担 spec、scratchpad 或决策仓库职责（那是 OpenSpec `specs/`、`design.md`、`docs/adr/` 的事）。

## 单 vs 多 context

- **单 context（多数项目）：** `openspec/glossary.md` 一个 `## Language` 段即可。
- **多 context：** 在**同一个** `openspec/glossary.md` 内用一个 `## Context Map` 段列出各 context 及其关系，再为每个 context 起一个二级标题、各带自己的 `## Language`。**不拆成多文件**——贴合 OpenSpec 单目录习惯与本仓库的熵治理。

  ```md
  # Glossary

  ## Context Map

  - **Ordering** — 接收并跟踪客户订单
  - **Billing** — 生成发票、处理付款
  - **Ordering → Billing**: Ordering 发出 `OrderPlaced` 事件，Billing 消费以生成发票

  ## Ordering

  **Order**: ...

  ## Billing

  **Invoice**: ...
  ```

- 话题涉及哪个 context 不清楚时，先询问，再决定术语写到哪个段。
- **懒创建**：第一个术语被解决时才创建本文件；在此之前不要为了占位而建空文件。
