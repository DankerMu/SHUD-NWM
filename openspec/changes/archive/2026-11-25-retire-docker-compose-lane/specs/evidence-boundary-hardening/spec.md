## MODIFIED Requirements

### Requirement: Artifact ownership wording matches audit expectations

Governed artifact ownership documentation SHALL include the literal ownership
terms required by the entropy audit.

#### Scenario: governance audit checks DOC_STATUS

- **WHEN** the audit checks `docs/governance/DOC_STATUS.md`
- **THEN** the document explicitly mentions every ownership term the audit
  requires (the agent, review, evidence and artifact paths) and no
  gate-eligible ownership finding is reported

## REMOVED Requirements

### Requirement: Display runtime rejects compute-only copyback authority

**Reason**: The scenario "compute role includes copyback root" describes the static check of the compose files, which is deleted with the docker/compose lane (#3231); the other scenario names the Docker entrypoint.

**Migration**: Replaced by "The display role rejects compute-only copyback authority". How a compute-side copyback root may relate to the object-store root stays with the publisher's copyback validation.

## ADDED Requirements

### Requirement: The display role rejects compute-only copyback authority

The `display_readonly` runtime role SHALL reject compute-control path
configuration, including `NHMS_OBJECT_STORE_COPYBACK_ROOT`, so display nodes can
read published artifacts without gaining run-product copyback authority.

#### Scenario: display role includes copyback root

- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly` and
  `NHMS_OBJECT_STORE_COPYBACK_ROOT` configured
- **THEN** startup is refused and the key is reported as a
  display-forbidden compute-control path env
