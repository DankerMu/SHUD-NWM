"""Grid registry worker package (Epic #897 canonical-source-grid-registry).

Task 3.1a defines the input-record contract at :mod:`workers.grid_registry.input_record`.
Task 3.1b (SUB-5) exposes the writer at :mod:`workers.grid_registry.registry` and
the CLI at ``python -m workers.grid_registry``.
"""

from workers.grid_registry.registry import (
    GridDriftDetectedError,
    LiveProducerSignatureMismatchError,
    RegistrationError,
    RegistrationInvariantError,
    register_snapshot,
)

__all__ = (
    "GridDriftDetectedError",
    "LiveProducerSignatureMismatchError",
    "RegistrationError",
    "RegistrationInvariantError",
    "register_snapshot",
)
