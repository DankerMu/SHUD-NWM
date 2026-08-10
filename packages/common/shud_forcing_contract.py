"""Single source of truth for the SHUD forcing package station-index identity.

The direct-grid SHUD forcing package publishes one *main station-index member*
(the ``.tsd.forc`` table listing station rows plus their per-station CSV
filenames). Historically that member was hard-coded as ``shud/qhh.tsd.forc``
for **every** basin, which made a single basin's slug masquerade as a generic
transport name and left 10+ hand-copied string literals across the producer,
the SHUD runtime, the mapping builder, and the QHH diagnostic tooling with no
shared authority to keep them aligned.

This module is that authority (issue #1176, change
``forcing-package-neutral-identity``):

* producers emit **only** :data:`CANONICAL_SHUD_FORCING_INDEX_MEMBER`;
* direct-grid consumers require **exactly one** member of
  :data:`SHUD_FORCING_INDEX_MEMBERS` in both the package manifest and the staged
  filesystem — both members present, or none declared, is fail-closed;
* non-direct-grid staging copies the whole package prefix and may legitimately
  hold a residual second member, so it resolves by the manifest-declared member
  with canonical-first as the fallback, and keeps the existing internal-forcing
  fallback when zero members are present;
* either way, historical packages carrying
  :data:`LEGACY_SHUD_FORCING_INDEX_MEMBER` stay readable without ever being
  rewritten;
* the member filename is a pure *transport* identity: it is never evidence of
  which basin the forcing data belongs to, and the runtime still stages it out
  as ``{project}.tsd.forc`` in the model input directory.

Deliberately dependency-free (no imports beyond ``__future__``) so every worker
can consume it without creating an import cycle.
"""

from __future__ import annotations

#: Canonical, basin-neutral station-index member of a SHUD forcing package.
#: The only member name new packages are allowed to carry.
CANONICAL_SHUD_FORCING_INDEX_MEMBER = "shud/stations.tsd.forc"

#: Legacy station-index member name. Accepted read-only for historical
#: packages already in the object store; never produced again. Removable once
#: no direct-grid package in the object store lacks the canonical member.
LEGACY_SHUD_FORCING_INDEX_MEMBER = "shud/qhh.tsd.forc"

#: Every accepted station-index member path, canonical first — that order is the
#: non-direct-grid fallback preference. Direct-grid consumers MUST require
#: exactly one of these.
SHUD_FORCING_INDEX_MEMBERS = (
    CANONICAL_SHUD_FORCING_INDEX_MEMBER,
    LEGACY_SHUD_FORCING_INDEX_MEMBER,
)

#: Basename of :data:`CANONICAL_SHUD_FORCING_INDEX_MEMBER`.
CANONICAL_SHUD_FORCING_INDEX_BASENAME = "stations.tsd.forc"

#: Basename of :data:`LEGACY_SHUD_FORCING_INDEX_MEMBER`.
LEGACY_SHUD_FORCING_INDEX_BASENAME = "qhh.tsd.forc"

#: Both station-index basenames. Station forcing CSVs must never collide with
#: either one, so reserved-filename sets are built from this tuple.
SHUD_FORCING_INDEX_BASENAMES = (
    CANONICAL_SHUD_FORCING_INDEX_BASENAME,
    LEGACY_SHUD_FORCING_INDEX_BASENAME,
)

#: Package manifest ``role`` marking the station-index member. Unchanged by
#: the identity migration so existing manifest consumers keep working.
SHUD_FORCING_ROLE = "shud_forcing"
