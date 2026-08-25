## ADDED Requirements

### Requirement: Registry import accepts every supported package manifest schema version

Basins registry import SHALL validate a package manifest's `schema_version` against the declared set of supported package schema versions rather than against a single hardcoded literal, and that set SHALL retain versions published before a packaging migration for as long as manifests carrying them can still be presented. Import SHALL reject only a version outside that set, with `BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID`.

#### Scenario: Freshly published manifest imports after a packaging schema bump

- **WHEN** the packager publishes a model at the current package schema version and that manifest is imported
- **THEN** import proceeds, because the current version is a supported version

#### Scenario: Pre-migration manifest still imports

- **WHEN** a manifest published before the packaging migration is presented, including through the relocation path that re-imports an already-verified package
- **THEN** import proceeds, because the previous version remains supported

#### Scenario: Unknown version is refused

- **WHEN** a manifest carries a `schema_version` outside the supported set
- **THEN** import fails with `BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID`
