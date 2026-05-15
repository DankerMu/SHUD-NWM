"""Slurm gateway configuration.

`infra/sbatch/` is the canonical real Slurm template set per M6 design decision #1.
`workers/sbatch_templates/` contains legacy single-run templates and is not used by M3+ array orchestration.
`SlurmGatewaySettings.template_dir` defaults to `infra/sbatch`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JOB_TYPE_TEMPLATES = {
    "download_source_cycle": "download_source_cycle.sbatch",
    "convert_canonical": "convert_canonical.sbatch",
    "produce_forcing_array": "produce_forcing_array.sbatch",
    "run_shud_forecast_array": "run_shud_forecast_array.sbatch",
    "parse_output_array": "parse_output_array.sbatch",
    "compute_frequency_array": "compute_frequency_array.sbatch",
    "publish_tiles": "publish_tiles.sbatch",
    "hindcast": "hindcast.sbatch",
}


class SlurmGatewaySettings(BaseSettings):
    """Configuration for Slurm gateway backends."""

    backend: str = "mock"
    version: str = "0.1.0"
    delay_to_running_seconds: float = Field(default=2.0, ge=0)
    delay_to_succeeded_seconds: float = Field(default=5.0, ge=0)
    failure_rate: float = Field(default=0.0, ge=0, le=1)
    failure_seed: int = 42
    force_fail_run_ids: list[str] = Field(default_factory=list)
    workspace_dir: str = "workspace"
    slurm_bin_path: str = ""
    template_dir: str = "infra/sbatch"
    resource_profiles_path: str = "config/resource_profiles.yaml"
    job_type_templates: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_JOB_TYPE_TEMPLATES))
    sacct_poll_interval_seconds: int = Field(default=30, ge=1)
    subprocess_timeout_seconds: int = Field(default=30, ge=1)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])
    allow_internal_reset: bool = False

    model_config = SettingsConfigDict(
        env_prefix="SLURM_GATEWAY_",
        env_nested_delimiter="__",
    )


@lru_cache
def get_settings() -> SlurmGatewaySettings:
    return SlurmGatewaySettings()
