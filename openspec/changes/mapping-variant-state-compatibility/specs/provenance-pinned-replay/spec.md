## ADDED Requirements

### Requirement: Replay lineage is pinned by cutover interval

Replay and calibration SHALL be pinned to model lineage by interval, with the cutover time `t*` resolved from a recorded authority: the successful direct-grid activation audit record for the basin scope in `ops.audit_log` (the Change 4 activation-class audit record), which by construction equals the `valid_time` of the cutover's cloned `(M1, source, t*)` rows. The pre-cutover interval SHALL be replayed offline on the legacy model `M0` and the post-cutover interval SHALL use the direct-grid variant `M1`. An interval that straddles `t*` SHALL be split at `t*` into an `M0` sub-interval (offline) and an `M1` sub-interval, each executed and lineage-recorded separately; producing a single cross-variant spliced series is forbidden (docs §10: 跨 cutover 的同站连续时间序列不存在，消费方必须按 variant 分段). Replay SHALL be a non-activation operation and SHALL NOT invoke lifecycle activation.

#### Scenario: Pre-cutover interval replays on M0

- **WHEN** a replay or calibration run targets a time interval entirely before the cutover time `t*`
- **THEN** the run uses model `M0`
- **THEN** the run's recorded lineage attributes the outputs to `M0`.

#### Scenario: Post-cutover interval uses M1

- **WHEN** a replay or forecast run targets a time interval at or after the cutover time `t*`
- **THEN** the run uses model `M1`
- **THEN** the run's recorded lineage attributes the outputs to `M1`.

#### Scenario: An interval straddling the cutover time is split at t*

- **WHEN** a replay or calibration request targets an interval that starts before `t*` and ends at or after `t*` (the common long historical calibration window)
- **THEN** the request is resolved as two lineage-pinned sub-intervals split at `t*`: the `[start, t*)` portion on `M0` offline and the `[t*, end]` portion on `M1`
- **THEN** each sub-interval records its own model lineage and no single output series splices across the variant boundary.

#### Scenario: The cutover time is resolved from the recorded activation authority

- **WHEN** replay routing needs `t*` for a basin scope
- **THEN** `t*` is read from the recorded direct-grid activation authority for the scope (the successful activation-class audit record), never guessed from data availability or wall-clock time
- **THEN** the resolved `t*` equals the `valid_time` of the cutover's cloned `(M1, source, t*)` rows whenever a clone was performed.

### Requirement: The activation hard guard does not mis-fire on offline replay

Because offline replay is a non-activation operation, the Change 4 lifecycle hard guard that refuses re-activating a legacy-mapping model after direct-grid activation history SHALL NOT intercept offline `M0` replay. The guard SHALL remain scoped to activation requests only.

#### Scenario: Offline M0 replay after cutover is not blocked by the guard

- **WHEN** a basin has direct-grid (`M1`) activation history and an operator starts an offline `M0` replay/calibration run for a pre-cutover interval
- **THEN** the run does not call lifecycle activation and is not intercepted by the legacy-reactivation hard guard
- **THEN** the offline `M0` run proceeds and records its `M0` lineage.

#### Scenario: Re-activating legacy mapping is still refused

- **WHEN** a request attempts to lifecycle-activate the legacy-mapping model `M0` for a basin with direct-grid activation history
- **THEN** the Change 4 hard guard refuses the activation
- **THEN** the refusal applies only to activation, leaving offline replay unaffected.
