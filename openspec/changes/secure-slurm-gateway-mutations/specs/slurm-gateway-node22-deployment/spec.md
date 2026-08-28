## ADDED Requirements

### Requirement: Node-22 Slurm mutation defense in depth
The production Slurm gateway SHALL combine application-layer service authentication with an entrypoint-enforced loopback-only bind. The service SHALL reject every non-loopback bind before uvicorn starts; a root-managed host packet-filter or ACL is an optional stronger control, not a claim made without an installed live mechanism.

#### Scenario: Gateway and scheduler share an owner-only credential
- **WHEN** the node-22 gateway and DB-free scheduler are deployed
- **THEN** both consume the same `SLURM_GATEWAY_SERVICE_TOKEN` from one untracked owner-mode-0600 environment source
- **AND** the checked-in unit/examples contain only variable names or placeholders, never the credential value.

#### Scenario: Gateway is not remotely reachable
- **WHEN** the node-22 deployment receipt is captured at the configured live gateway port (currently measured as 8090)
- **THEN** `ss -ltnp` shows the gateway bound only to a loopback address such as `127.0.0.1:8090`
- **AND** starting the checked-in entrypoint with `0.0.0.0`, `::`, a hostname, or any non-loopback IP fails before uvicorn opens a socket
- **AND** a probe from another host cannot connect to node-22 at the configured gateway port
- **AND** any packet-filter/ACL receipt is included only if an operator-managed rule is actually installed.

#### Scenario: Live auth boundary is proven without creating a job
- **WHEN** local node-22 probes send an invalid submission body with no token, a wrong token, and the configured token
- **THEN** missing/wrong credentials return `401` before body validation while the configured token passes auth and reaches the existing validation error
- **AND** the receipt proves no `sbatch`, `scancel`, or reset side effect was created by these probes.

#### Scenario: Disabled reset remains absent
- **WHEN** the standalone production gateway starts with `SLURM_GATEWAY_ALLOW_INTERNAL_RESET` unset or false
- **THEN** `POST /api/v1/slurm/internal/reset` returns 404 because the route is not registered
- **AND** authentication changes do not turn the disabled route into 401, 403, or a registered operation.

#### Scenario: Scheduler remains operational after credential rollout
- **WHEN** the DB-free scheduler resumes after gateway auth deployment
- **THEN** its HTTP Slurm client attaches the service bearer to submit/cancel calls and gateway preflight remains healthy
- **AND** health and read-only gateway requests do not carry the service token.
