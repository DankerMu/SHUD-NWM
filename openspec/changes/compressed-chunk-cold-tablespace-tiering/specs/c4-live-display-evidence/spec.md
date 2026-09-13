# Proposed survivor delta

This is the target contract for R1's transfer, not a claim that the replacement
production-acceptance entrypoint already exists. The existing display deployment
owner's **Bringup-C4 production acceptance** seam owns the retained outer check;
the independent C4 producer and local binder remain unchanged.

## MODIFIED Requirements

### Requirement: 私有闭集证据接受链

producer、schema、semantic validator、publisher 和 binder SHALL 一起交付，状态为 PASS/BLOCKED/FAIL。
字段 MUST 闭集、脱敏、限长/限深/限大小；跨字段 source/job/log/时间关系由 semantic 与 binder 验证。
输出 MUST 使用 euid-owned 0700 parent 与 0600 regular single-link 文件，
no-follow/exclusive/no-clobber 发布、fsync/readback，拒绝交换/变化；清理只允许本次创建的对象。
C4 binder MUST 绑定当次五输入、秒粒度执行 bracket 与 POSIX identity facts，
不能接受交换、过期或不完整证据。

端到端生产接受 MUST 由既有 display 上线 owner 的 **Bringup-C4 production acceptance**
入口承接原 G0/C3 中必要的外层校验：执行前从获批准的交付/C1 记录冻结 reviewed SHA，
不得从待接受的 C4 文件或最终一次观察反推预期值；生成外层接受记录时绑定该 reviewed SHA
和当次已通过本地 C4 binder 的原始文件字节 sha256、文件身份、输入与执行 bracket。
最终接受 MUST 对照原先冻结的 SHA 和外层记录，重新验证 reviewed-SHA、C4 原始字节 sha256
及文件身份，拒绝缺失、不匹配、交换或变化的记录。外层记录沿用既有私有文件与有界读取安全约束，
不得以新读到的事实覆盖原记录后声称比对成功。

R1 MUST 交付并登记该存活入口的实际代码归属、公开调用路径、记录来源及正反向行为证明，
而不是只给接受职责改名。该入口与匹配测试/契约交付前，R3 MUST NOT 删除旧 G0/C3
所承载的 SHA/digest 校验能力。它只迁移上述必要保证，不恢复冷 rollout、N 组 census、
冷 baseline 或 C3 的其他已撤销检查，也不引入更强的签名/信任模型。

C4 CLI 成功 MUST NOT 代替外层生产接受或 C1–C3 的独立证据。SHA/digest 绑定 MUST
留在外层接受记录，MUST NOT 在 C4 闭集 schema 或既有 binder CLI 中虚构、增加相应字段。
R1 切换后，生产接受 MUST NOT 再依赖 `#1895 G0/C3`、其已退役模块或包装入口。

#### Scenario: 完整 receipt 可接受

- **WHEN** 同一真实执行所有事实成立、schema/semantic 一致且私有文件字节和身份未变化
- **THEN** C4 binder 接受对应输入/bracket/文件身份的 PASS receipt
- **AND** 生产接受还须由 Bringup-C4 production acceptance 对获批准交付/C1 冻结的 reviewed SHA 和原外层记录重验 exact-byte sha256 与文件身份；本地 CLI PASS 单独不足

#### Scenario: 证据或路径不可信

- **WHEN** 目标已存在、symlink/nonregular/权限/nlink 不合格，或 receipt 有额外字段、错误 identity/bracket、secret、超界 JSON 或 POSIX identity facts 变化
- **THEN** 发布或接受失败闭合，不覆盖旧目标，不输出 raw URL/query/secret

#### Scenario: 既有 river-click 发布保持兼容

- **WHEN** river-click 使用共享 private publisher 遇到成功或原生文件系统错误
- **THEN** 保持其既有格式、错误分类、no-clobber 与 FD 释放契约

#### Scenario: 外层绑定缺失或已变化

- **WHEN** 本地 C4 binder 已 PASS，但原 reviewed-SHA 冻结/外层记录缺失，SHA 不匹配，或最终接受时 C4 字节/文件身份与原记录不同
- **THEN** Bringup-C4 production acceptance 拒绝生产接受；不得重新采样为新预期、降级成 CLI PASS 或在 C4 闭集协议中增加字段来绕过

#### Scenario: 冷接受链删除前必须先完成存活入口交接

- **WHEN** R3 准备删除原 issue1895 G0/C3 接受路径
- **THEN** R1 必须已交付 Bringup-C4 production acceptance 的真实入口、记录来源和缺失/SHA 不匹配/字节或身份变化拒绝证明，并更新匹配的 canonical C4 契约及调用方
- **AND** 未提供该证明则 cutover 不完整；不能只保留独立 C4 producer/binder 就删除外层保证
