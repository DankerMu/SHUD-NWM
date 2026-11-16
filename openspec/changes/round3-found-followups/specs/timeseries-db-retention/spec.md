## REMOVED Requirements

### Requirement: Retention enforcement is hard-gated on archive receipts

**Reason**: The archive-completeness and archive-rebuild-drill gates, their receipt loaders and the archive-family wire codes were deleted by #1370 when ADR 0002 (Revision 2026-08-11) retired the archive lane. The runner now accepts only the explicit `disabled` archive-gate mode (`scripts/node27_timeseries_retention.py:8-21`). This requirement contradicts that code and the same capability's requirement "The archive gate MUST support an explicit auditable disabled mode while the default stays fail-closed". It also cites the `db-export-salvage` capability and the archive-rebuild-drill coverage rule, which this change retires.

**Migration**: None. The deletion authority is the explicit `disabled` mode requirement, and every receipt records `archive_gate.mode = "disabled"` together with the ADR citation.
