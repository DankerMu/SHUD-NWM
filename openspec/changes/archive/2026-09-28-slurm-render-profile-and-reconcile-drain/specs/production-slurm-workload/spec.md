## ADDED Requirements

### Requirement: Production-closure render uses the submission's own resource profile

The production-closure Slurm validation SHALL render the sbatch script from the resource profile of the current submission held in memory, with the deployment-level partition and exclude-node overrides applied by the same resolution function the gateway uses. The `lane_dir/resource_profiles.yaml` file SHALL remain byte-identical evidence and SHALL NOT be read back as a render input.

#### Scenario: Concurrent forced submission overwrites the lane profile before render

- **WHEN** two `validate-slurm --submit --force` runs share a `run_id` with different resource env, and B overwrites `lane_dir/resource_profiles.yaml` after A wrote it but before A renders
- **THEN** A's rendered script MUST carry A's partition, memory, walltime, cpus-per-task and SHUD thread values

#### Scenario: Deployment partition override still applies

- **WHEN** `SLURM_GATEWAY_PARTITION_OVERRIDE` and `SLURM_GATEWAY_EXCLUDE_NODES` are set in the process environment
- **THEN** the rendered script MUST use the override partition and exclude-node values
