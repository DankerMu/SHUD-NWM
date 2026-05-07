from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SlurmGatewaySettings(BaseSettings):
    """Configuration for the Slurm gateway mock backend."""

    backend: str = "mock"
    version: str = "0.1.0"
    delay_to_running_seconds: float = Field(default=2.0, ge=0)
    delay_to_succeeded_seconds: float = Field(default=5.0, ge=0)
    failure_rate: float = Field(default=0.0, ge=0, le=1)
    failure_seed: int = 42
    force_fail_run_ids: list[str] = Field(default_factory=list)

    model_config = SettingsConfigDict(
        env_prefix="SLURM_GATEWAY_",
        env_nested_delimiter="__",
    )


@lru_cache
def get_settings() -> SlurmGatewaySettings:
    return SlurmGatewaySettings()

