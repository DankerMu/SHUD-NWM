# Tasks — env-template-slurm-private-lanes (#3171)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Config / project setup | yes | env template + README required-keys block: E2, E3 |
| Documentation / migration notes | yes | the deliberate absence of the readiness private key is documented: task 1.3 |
| Others | no | no code, schema, IO or runtime change |

- [ ] 1.1 Add `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` to `infra/env/compute.scheduler-dbfree.env.example` with the
  node-22 private path and a comment naming the shared-key fallback and the generation check.
- [ ] 1.2 Add it to the README `nhms-required-keys: compute.scheduler-dbfree` block with a comment.
- [ ] 1.3 In the template and the README, state that `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is deliberately
  unset: no producer maintains a private readiness index, no Slurm entry point reads it, and the fallback is the shared
  index that refresh keeps current.
- [ ] 1.4 Add a case to `tests/test_env_templates.py` asserting, via `parse_env_template`, that
  `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is absent from the template. Document the absence only in whole-line
  `#` comments.
- [ ] 1.5 Show the required-keys check reds when the registry key is removed, and the absence case reds when the
  readiness key is added (scratch copies), then run the E2 tests.
