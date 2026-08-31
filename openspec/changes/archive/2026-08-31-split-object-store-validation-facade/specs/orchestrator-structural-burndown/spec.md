## ADDED Requirements

### Requirement: Object-store validation facade split preserves runtime and evidence contracts

The repository SHALL split
`services.production_closure.object_store_validation` into the eight responsibility
owners defined by this change. The historical facade and every new owner MUST remain
below 1,000 lines while `.large-file-guard.json` remains byte-identical. Every
pre-split facade attribute, callable signature and dataclass shape MUST remain
available: eight callable seams use runtime facade forwarding, other helpers use plain
re-export, and module/class attribute patches observe the same authority objects.
Standalone and packaged CLI behavior, synthetic fixture bytes, result/blocker/redaction,
evidence/manifest/checksum identity, path safety, runtime staging and cleanup MUST
remain equivalent.

#### Scenario: existing callers import and validate through the facade

- **WHEN** `slurm_validation`, readiness consumers or an existing test imports the
  historical module and validates a deterministic object-store fixture
- **THEN** all baseline names, signatures, dataclasses and class identities resolve
  without an import cycle
- **AND** fixture/package/manifest/checksum bytes, evidence files, staged receipts,
  blocker ordering, redacted summary and object effects are identical to baseline.

#### Scenario: a historical dynamic dependency is patched

- **WHEN** a test patches one of the eight governed facade callables and invokes the
  existing high-level writer, package, verification, registry, staging or CLI path
- **THEN** the direct/transitive coordinator forwards the facade's current runtime
  binding rather than a stale leaf-local callable
- **AND** shared module/class patches retain identity and the existing biting failure
  or output oracle proves the real call path observed the patch.

#### Scenario: unsafe or stale validation input retains fail-closed behavior

- **WHEN** existing symlink, ancestor-swap, non-regular, oversized, stale-workspace,
  tampered-after-verify, collision or pre-existing-object fixtures are validated
- **THEN** the same typed error or blocker and redacted evidence boundary occurs
- **AND** no external path, prior object or cleanup target not created by this run is
  written, replaced or deleted.

#### Scenario: standalone and packaged CLI contracts remain stable

- **WHEN** operators invoke either
  `python -m services.production_closure.object_store_validation` or
  `nhms-production validate-object-store`
- **THEN** click/argparse option, exit-code, stdout/stderr and redaction behavior remains
  identical, including usage failures without a traceback
- **AND** the existing production importer still resolves the historical public types
  and `validate_object_store` entrypoint.

#### Scenario: structural guard evaluates the finite owner set

- **WHEN** the eight production files and guard configuration are evaluated
- **THEN** each file is strictly below 1,000 lines, no ninth owner or replacement
  exclusion exists, the guard digest is unchanged, and a normal verified commit passes
  the hook.
