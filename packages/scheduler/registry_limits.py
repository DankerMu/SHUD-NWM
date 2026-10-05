"""Size bounds of the scheduler registry manifest, shared by every reader and writer.

A leaf module with no imports, so standalone scripts and the light orchestrator
modules can bound a registry read without loading the provider stack.
``services.orchestrator.scheduler_file_providers`` re-exports both names.
"""

from __future__ import annotations

# The reader holds the whole manifest in memory, so both bounds are hard
# refusals on publish and on read. Measured on the production manifest of
# 2026-10-05: 132 rows, 13,353,454 bytes, 329,431 JSON value nodes (a row
# inlines its direct-grid station bindings: about 101 KB and 2,500 nodes).
# The previous 16 MiB / 400,000 bounds left room for about 14 more basins, the
# node bound binding first. At 32 MiB / 800,000 the node bound still binds
# first, at about 320 rows; ``MAX_REGISTRY_MODELS`` stays the row-count bound.
#
# History: 4 MiB until 2026-08-25, when 62 rows / 4,250,534 bytes overran it
# and the atomic writer rolled the publish back (``provider_restored_previous``).
MAX_REGISTRY_MANIFEST_BYTES = 32 * 1024 * 1024
# Keys are not nodes; mappings, arrays and every value in them are. This budget
# is specific to registry manifests: readiness, catalog and state-index readers
# keep their own 300,000-node bound.
MAX_REGISTRY_MANIFEST_JSON_NODES = 800_000
