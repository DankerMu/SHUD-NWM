from __future__ import annotations

import os as os
import re as re
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from datetime import UTC as UTC
from datetime import datetime as datetime
from errno import ENOENT as ENOENT
from pathlib import Path as Path
from pathlib import PurePosixPath as PurePosixPath
from typing import Any as Any
from typing import Mapping as Mapping
from urllib.parse import unquote as unquote
from urllib.parse import urlparse as urlparse

from packages.common.redaction import redact_payload as redact_payload
from services.orchestrator import scheduler as _scheduler  # noqa: F401
from services.orchestrator import source_cycle_raw_manifest  # noqa: F401
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES as DEFAULT_JOB_TYPE_TEMPLATES

from . import config as config
from . import db_free as db_free
from . import path_modes as path_modes
from .config import (
    _DB_FREE_CANONICAL_RAW_AUTHORITY_ENV as _DB_FREE_CANONICAL_RAW_AUTHORITY_ENV,
)
from .config import (
    _DB_FREE_PATH_SPECS as _DB_FREE_PATH_SPECS,
)
from .config import (
    _DB_FREE_RAW_MANIFEST_ROOT_ENV as _DB_FREE_RAW_MANIFEST_ROOT_ENV,
)
from .config import (
    _DB_FREE_REQUIRED_ENV as _DB_FREE_REQUIRED_ENV,
)
from .config import (
    _DB_FREE_SELECTOR_SPECS as _DB_FREE_SELECTOR_SPECS,
)
from .config import (  # noqa: F401
    ProductionSchedulerConfig as ProductionSchedulerConfig,
)
from .config import (
    _evidence_scalar as _evidence_scalar,
)
from .config import (
    _normalized_optional_identity as _normalized_optional_identity,
)
from .config import (
    _repair_missing_forcing_cycle_time as _repair_missing_forcing_cycle_time,
)
from .db_free import (  # noqa: F401
    _DB_FREE_CREDENTIAL_WORDS as _DB_FREE_CREDENTIAL_WORDS,
)
from .db_free import (
    _DB_FREE_DB_BACKEND_VALUES as _DB_FREE_DB_BACKEND_VALUES,
)
from .db_free import (
    _DB_FREE_ENCODED_FORBIDDEN_RE as _DB_FREE_ENCODED_FORBIDDEN_RE,
)
from .db_free import (
    _DB_FREE_OBJECT_STORE_PREFIX_ENV as _DB_FREE_OBJECT_STORE_PREFIX_ENV,
)
from .db_free import (
    _DB_FREE_PUBLIC_OBJECT_PREFIXES as _DB_FREE_PUBLIC_OBJECT_PREFIXES,
)
from .db_free import (
    _DB_FREE_RAW_MANIFEST_PREFIX_ENV as _DB_FREE_RAW_MANIFEST_PREFIX_ENV,
)
from .db_free import (
    _DB_FREE_SAFE_OBJECT_SEGMENT_RE as _DB_FREE_SAFE_OBJECT_SEGMENT_RE,
)
from .db_free import (
    _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES as _DB_FREE_SUPPORTED_OBJECT_URI_SCHEMES,
)
from .db_free import (
    _db_free_allowed_roots as _db_free_allowed_roots,
)
from .db_free import (
    _db_free_allowed_roots_and_blockers as _db_free_allowed_roots_and_blockers,
)
from .db_free import (
    _db_free_blocker as _db_free_blocker,
)
from .db_free import (
    _db_free_blocker_path_evidence as _db_free_blocker_path_evidence,
)
from .db_free import (
    _db_free_common_object_uri_unsafe_reason as _db_free_common_object_uri_unsafe_reason,
)
from .db_free import (
    _db_free_file_is_readable as _db_free_file_is_readable,
)
from .db_free import (
    _db_free_local_path_component_reason as _db_free_local_path_component_reason,
)
from .db_free import (
    _db_free_loop_filtered_realpath as _db_free_loop_filtered_realpath,
)
from .db_free import (
    _db_free_object_uri_check as _db_free_object_uri_check,
)
from .db_free import (
    _db_free_path_check as _db_free_path_check,
)
from .db_free import (
    _db_free_path_evidence_scalar as _db_free_path_evidence_scalar,
)
from .db_free import (
    _db_free_path_identity as _db_free_path_identity,
)
from .db_free import (
    _db_free_published_uri_boundary as _db_free_published_uri_boundary,
)
from .db_free import (
    _db_free_raw_manifest_prefix_check as _db_free_raw_manifest_prefix_check,
)
from .db_free import (
    _db_free_raw_manifest_prefix_evidence as _db_free_raw_manifest_prefix_evidence,
)
from .db_free import (
    _db_free_resolution_failure_reason as _db_free_resolution_failure_reason,
)
from .db_free import (
    _db_free_s3_uri_boundary as _db_free_s3_uri_boundary,
)
from .db_free import (
    _db_free_safe_object_key as _db_free_safe_object_key,
)
from .db_free import (
    _db_free_scheme_for_evidence as _db_free_scheme_for_evidence,
)
from .db_free import (
    _db_free_selector_check as _db_free_selector_check,
)
from .db_free import (
    _db_free_selector_evidence_scalar as _db_free_selector_evidence_scalar,
)
from .db_free import (
    _db_free_selector_text_is_db_like as _db_free_selector_text_is_db_like,
)
from .db_free import (
    _db_free_uri_evidence as _db_free_uri_evidence,
)
from .db_free import (
    _db_free_urlparse as _db_free_urlparse,
)
from .db_free import (
    _path_is_relative_to as _path_is_relative_to,
)
from .path_modes import (  # noqa: F401
    _config_path_preserve_final_component_for_mode as _config_path_preserve_final_component_for_mode,
)
from .path_modes import (
    _config_path_relative_to_preserve_final_for_mode as _config_path_relative_to_preserve_final_for_mode,
)
from .path_modes import (
    _confined_path_for_mode as _confined_path_for_mode,
)
from .path_modes import (
    _expanduser_for_mode as _expanduser_for_mode,
)
from .path_modes import (
    _optional_config_path_for_mode as _optional_config_path_for_mode,
)
from .path_modes import (
    _optional_config_path_relative_to_preserve_final_for_mode as _optional_config_path_relative_to_preserve_final_for_mode,  # noqa: E501
)
from .path_modes import (
    _optional_raw_config_path_relative_to_preserve_components as _optional_raw_config_path_relative_to_preserve_components,  # noqa: E501
)
from .path_modes import (
    _raw_config_path_preserve_components as _raw_config_path_preserve_components,
)
from .path_modes import (
    _raw_config_path_relative_to_preserve_components as _raw_config_path_relative_to_preserve_components,
)
from .path_modes import (
    _require_safe_directory_final_component_for_mode as _require_safe_directory_final_component_for_mode,
)
from .path_modes import (
    _require_under_workspace_for_mode as _require_under_workspace_for_mode,
)
from .path_modes import (
    _resolve_config_path_for_mode as _resolve_config_path_for_mode,
)
from .path_modes import (
    _resolve_optional_config_path_for_mode as _resolve_optional_config_path_for_mode,
)
from .path_modes import (
    _safe_preserve_final_component as _safe_preserve_final_component,
)

# Restore the monolith's shared-namespace annotation contract: the historical
# owner defined ``ProductionSchedulerConfig`` in the same module globals as the
# db_free helpers, so ``typing.get_type_hints`` on
# ``_db_free_allowed_roots`` / ``_db_free_allowed_roots_and_blockers`` resolved
# their ``config`` annotations to the live class. ``db_free`` only declares the
# class under TYPE_CHECKING (a runtime import there would cycle back through
# ``config``), so the barrel binds the class into ``db_free``'s globals after
# both modules are loaded.
db_free.ProductionSchedulerConfig = ProductionSchedulerConfig

__all__ = ("ProductionSchedulerConfig",)
