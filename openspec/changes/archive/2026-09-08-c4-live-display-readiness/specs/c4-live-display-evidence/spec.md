## ADDED Requirements

### Requirement: 独立真实 C4 入口

系统 SHALL 提供 `test:e2e:live-c4-display`，要求显式 frontend/API origin、basin、segment、receipt path。入口 MUST NOT 使用 mock/HAR API、角色 override、历史 receipt 或 river-click receipt 代替真实 C4，普通测试发现 MUST 排除 live-only lane。

#### Scenario: 缺失输入不能变成跳过后成功
- **WHEN** frontend/API origin 或 receipt path 缺失
- **THEN** 入口非零结束为 BLOCKED；有可安全发布的路径时提供 BLOCKED 证据，不报告 PASS

#### Scenario: 产品定位 pin 缺失或非法
- **WHEN** basin 或 segment pin 缺失、空白或不符合标识符约束
- **THEN** 入口非零结束为 FAIL CONFIG_INVALID，并在路径可安全发布时提供 FAIL 证据，不启动浏览器、不报告 PASS

#### Scenario: 真实双源证明
- **WHEN** 显式配置就绪并执行 C4
- **THEN** Chromium 访问真实 `/` 和 GFS/IFS strict `/ops`，以 source、basin/version、network version、run、model、cycle、scenario 与 job/log identity 关联实际观察
- **AND** 普通 Vitest 的 fake-page/VM 测试不要求浏览器二进制，也不构成 live PASS

### Requirement: 完整有界只读浏览器证明

C4 MUST 观察 home 可见地图、健康 readonly runtime、current-read，以及双源 ops status/stages/jobs/logs；合法多 stage jobs 按稳定顺序选取匹配 job。loading SHALL 有界等待，quiet 后重读 SHALL 使用相同 step deadline。响应 MUST 完成且读取受 byte/time cap 限制，不能以 2xx headers 宣称成功。任何 required failure、source error、permission/runtime 错误、超时、Slurm 请求或非 GET/HEAD 控制请求 MUST 阻止 PASS；无控制计数 MUST 来自实际 observer。

#### Scenario: 响应或 DOM 不完成
- **WHEN** body、evaluate 或 quiet 后 recheck 挂起直到 deadline
- **THEN** lane 有界结束为非 PASS，不绕过 deadline 等待

#### Scenario: 正常取消与必需失败分流
- **WHEN** 导航产生非必需 ERR_ABORTED，而 required identity 请求均完成
- **THEN** 正常取消不单独使 C4 失败
- **AND** 同一执行中任一 required 请求失败或控制请求出现仍阻止 PASS

### Requirement: 私有闭集证据接受链

producer、schema、semantic validator、publisher 和 binder SHALL 一起交付，状态为 PASS/BLOCKED/FAIL。字段 MUST 闭集、脱敏、限长/限深/限大小；跨字段 source/job/log/时间关系由 semantic 与 binder 验证。输出 MUST 使用 euid-owned 0700 parent 与 0600 regular single-link 文件，no-follow/exclusive/no-clobber 发布、fsync/readback，拒绝交换/变化；清理只允许本次创建的对象。C4 binder MUST 绑定当次五输入、秒粒度执行 bracket 与 POSIX identity facts，不能接受交换、过期或不完整证据。端到端接受链 MUST 另由交付/G0 门冻结 reviewed SHA，并由 #1895 C3 publisher/binder 绑定及重验 C4 文件字节的 sha256 与 reviewed-SHA；MUST NOT 用 C4 CLI 成功代替外层校验，也不在 C4 闭集协议中虚构 SHA/digest 字段。

#### Scenario: 完整 receipt 可接受
- **WHEN** 同一真实执行所有事实成立、schema/semantic 一致且私有文件字节和身份未变化
- **THEN** C4 binder 接受对应输入/bracket/文件身份的 PASS receipt；生产整体接受仍须通过 #1895 G0/C3 的 SHA/digest 门

#### Scenario: 证据或路径不可信
- **WHEN** 目标已存在、symlink/nonregular/权限/nlink 不合格，或 receipt 有额外字段、错误 identity/bracket、secret、超界 JSON 或 POSIX identity facts 变化
- **THEN** 发布或接受失败闭合，不覆盖旧目标，不输出 raw URL/query/secret

#### Scenario: 既有 river-click 发布保持兼容
- **WHEN** river-click 使用共享 private publisher 遇到成功或原生文件系统错误
- **THEN** 保持其既有格式、错误分类、no-clobber 与 FD 释放契约
