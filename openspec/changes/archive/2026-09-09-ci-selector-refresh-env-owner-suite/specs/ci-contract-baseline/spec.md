## ADDED Requirements

### Requirement: the scheduler refresh env template MUST select its content-asserting owner suite

`tests/test_scheduler_file_provider_refresh.py` reads `infra/env/compute.scheduler-provider-refresh.env.example` by path and asserts its content: that `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` is present, and that none of `DATABASE_URL=`, `PIPELINE_DATABASE_URL=`, `PGHOST=` or `PGPORT=` appears. Before this change the only rule matching that path was the `infra/env/**` rule, whose single target does not read the file, so a template-only diff reached the targeted lane with no reader of the changed file executed — and because that selection is non-empty, the zero-assertion CI warning did not fire either. `scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`) for that template targeting `tests/test_scheduler_file_provider_refresh.py`. Because rule matches accumulate and the `infra/env/**` rule stays in place, the template's selection SHALL be exactly that owner suite together with `tests/test_two_node_docker_runtime.py`, and `tests/test_select_ci_tests.py` SHALL pin it as an exact set rather than by membership. The rule SHALL NOT be added to the `#1684` rollout-producer group, whose target is the static deployment contract suite and which does not read this template. The selections of the other thirteen `infra/env/*.example` templates SHALL remain unchanged.

#### Scenario: a refresh env template diff selects its owner suite

- **WHEN** the changed paths are exactly `infra/env/compute.scheduler-provider-refresh.env.example`
- **THEN** `select_tests` emits exactly `["tests/test_scheduler_file_provider_refresh.py", "tests/test_two_node_docker_runtime.py"]`

#### Scenario: sibling env templates keep their existing selections

- **WHEN** the changed paths are exactly `infra/env/compute.example`, or exactly `infra/env/compute.scheduler-dbfree.env.example`
- **THEN** each selection is exactly `["tests/test_slurm_gateway_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`
- **WHEN** the changed paths are exactly `infra/env/display.example`
- **THEN** the selection is exactly `["tests/test_two_node_docker_runtime.py"]`
