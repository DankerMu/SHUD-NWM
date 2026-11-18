## MODIFIED Requirements

### Requirement: 完整有界只读浏览器证明

C4 MUST 观察 home 可见地图、健康 readonly runtime、current-read，以及双源 ops
status/stages/jobs/logs；合法多 stage jobs 按稳定顺序选取匹配 job。loading
SHALL 有界等待，quiet 后重读 SHALL 使用相同 step
deadline。响应 MUST 完成且读取受 byte/time cap 限制，不能以 2xx
headers 宣称成功。任何 required failure、source
error、permission/runtime 错误、超时、Slurm 请求或非 GET/HEAD 控制请求 MUST 阻止 PASS；无控制计数 MUST 来自实际 observer。

#### Scenario: 响应或 DOM 不完成

- **WHEN** body、evaluate 或 quiet 后 recheck 挂起直到 deadline
- **THEN** lane 有界结束为非 PASS，不绕过 deadline 等待

#### Scenario: 正常取消与必需失败分流

- **WHEN** 导航产生非必需 ERR_ABORTED，而 required identity 请求均完成
- **THEN** 正常取消不单独使 C4 失败
- **AND** 同一执行中任一 required 请求失败或控制请求出现仍阻止 PASS

#### Scenario: 只读 /ops 出现禁用控件

- **WHEN** display_readonly `/ops` 标题已观察到，且 observer 计到 role selector 或 retry/cancel 控件
- **THEN** C4 以 `FORBIDDEN_CONTROL`（stage `ops`）结束为非 PASS
- **AND** role selector 与 retry/cancel 两条分支各有一条负向测试钉住其 code、stage 与 message
