---
name: handoff
description: >
  把当前会话压缩成一份交接文档（handoff.md），让新会话或另一个 agent 无缝接续工作。
  适用：上下文将满要开新会话、任务干到一半换 agent/换机器、issue 工作流跨会话续跑。
  手动调用（/handoff），可带参数说明下个会话的重点。模型不会自动触发。
argument-hint: "下个会话要做什么？（可选）"
disable-model-invocation: true
invocation_posture: manual
version: 0.1.0
---

# Handoff

把当前会话的**会话独有状态**写成一份新 agent 能直接开工的交接文档。与原生 compact 的分工：同一会话内的阶段过渡用 compact（隐式、不可携带）；跨会话、跨 agent、跨机器续跑用 handoff——产物是显式文件，新会话第一条消息就能加载。

## 写到哪

- 会话正处于 issue/PR 工作流（存在 `.workplans/<issue-or-pr>/`）→ 写 `.workplans/<issue-or-pr>/handoff.md`，与证据束同地存放，直接覆盖旧文件——交接文档只有最新一份有意义，历史状态在 git 和证据束里。
- 其他情况 → 写 OS 临时目录（macOS 用 `$TMPDIR`，Linux 用 `/tmp`），文件名 `handoff-<repo>-<yyyymmdd-hhmm>.md`，不污染工作区。
- 无论写到哪，结束时必须把**绝对路径**和一行接续指令打给用户（见"接续协议"）。

## 文档模板

```markdown
# Handoff: <一句话任务>

生成: <日期> | 仓库: <绝对路径> @ <branch> <short-sha> | 脏工作区: 否 / 是（列文件）

## 目标与当前状态
<两三句：最终目标、现在停在哪一步、离完成还差什么>

## 已定决策
- <决策>: <一句理由>
（只列本对话新达成、且未沉淀到其他工件的；每条一行）

## 工作流状态
（处于 subagent-workflow / issue-controller 等工作流时必填，否则删除本节）
- Skill/流: <名称> Phase <N>
- Review round counter: <n>（跨会话不得归零——三轮硬 gate 依赖它）
- Gate 状态: <未触发 / 已触发，retro 路径: ...>
- Last clean reviewed SHA: <sha 或 无>
- 证据束: <.workplans/... 路径>

## 工件索引（引用不复制）
- <spec/plan/ADR/issue/PR/diff/评审报告>: <路径或 URL> — <一句它是什么>

## 已排除路径
- <试过但失败或被否决的方案>: <为什么>
（防止新会话重走死路；没有就写"无"）

## 下一步
1. <必须是可直接执行的动作，含具体命令或文件>
2. <...>

## 建议 skills
- <skill 名>: <为什么下个会话需要它>
（不确定选哪个，就写"从 /ask-danker 进"）
```

## 硬规则

1. **引用不复制**：已沉淀在 spec、plan、ADR、issue、commit、diff、评审报告里的内容一律给路径或 URL。交接文档是索引加状态快照，真相留在原始工件——复制出来的第二份会腐烂。
2. **只写别处没有的**：判断标准——新会话丢了这条会犯错或重做的，写；能从工件重建的，引用。本对话独有的状态（决策理由、计数器、已排除路径、口头约定）才是正文。
3. **工作流计数器随身带**：review round counter、gate 状态、`Last clean reviewed SHA` 只存在于会话记忆中，丢了会破坏 `subagent-workflow` 三轮硬 gate 的不归零语义。处于工作流时这一节不可省略。
4. **脱敏**：API key、密码、token、PII 一律删除或以 `<REDACTED>` 占位；写完后按常见 secret 形状（`sk-`、`ghp_`、`AKIA`、`Bearer `）grep 自查一遍。
5. **带参数时按参数裁剪**：`/handoff 下个会话只做评审修复` → 全文围绕该重点组织，无关背景压到一行。
6. **交付前自检**：假装自己是只读这份文档的新 agent——"下一步"第一条能直接开工吗？不能就修完再交。

## 接续协议

写完后向用户输出：

```text
交接文档: <绝对路径>
新会话第一条消息: 请读 <绝对路径>，按"下一步"第一条继续。
```

新会话读到 handoff 后：若含"工作流状态"节，先恢复 round counter 与 gate 状态、定位证据束，再执行"下一步"；不得因为是新会话而重置任何计数器。

## Non-goals

- 不替代同会话内的原生 compact。
- 不是给人看的进度汇报——受众是 agent，可读性服务于"能开工"。
- 不是证据存储——评审报告、验证输出属于工作流证据束，这里只放索引。
