## REMOVED Requirements

### Requirement: PostgreSQL-valid cancel status transitions

**Reason**: The control-plane cancel route is retired (#3394); both scenarios drive that route and its response.
**Migration**: None. The scheduler's own cancellation writes statuses through the vocabulary pinned by `status-vocabulary` and the enum rule of "Status schema and OpenAPI alignment".
