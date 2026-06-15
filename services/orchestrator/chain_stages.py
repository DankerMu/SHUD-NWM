from __future__ import annotations

from services.orchestrator.chain_types import StageDefinition

LEGACY_FORECAST_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("download_gfs", "download", "download_source_cycle.sbatch", "raw_complete", "failed_download"),
    StageDefinition("convert_canonical", "canonical", "convert_canonical.sbatch", "canonical_ready", "failed_convert"),
    StageDefinition("produce_forcing", "forcing", "produce_forcing.sbatch", "forcing_ready", "failed_forcing"),
    StageDefinition("run_shud_forecast", "forecast", "run_shud_forecast.sbatch", "forecast_running", "failed_run"),
    StageDefinition("parse_output", "parse", "parse_output.sbatch", "complete", "failed_parse"),
)

M3_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(
        "download",
        "download_source_cycle",
        "download_source_cycle.sbatch",
        "raw_complete",
        "failed_download",
        is_array=False,
    ),
    StageDefinition(
        "convert",
        "convert_canonical",
        "convert_canonical.sbatch",
        "canonical_ready",
        "failed_convert",
        is_array=False,
    ),
    StageDefinition(
        "forcing",
        "produce_forcing_array",
        "produce_forcing_array.sbatch",
        "forcing_ready",
        "failed_forcing",
        is_array=True,
    ),
    StageDefinition(
        "forecast",
        "run_shud_forecast_array",
        "run_shud_forecast_array.sbatch",
        "forecast_running",
        "failed_run",
        is_array=True,
    ),
    StageDefinition(
        "parse",
        "parse_output_array",
        "parse_output_array.sbatch",
        "complete",
        "failed_parse",
        is_array=True,
    ),
    StageDefinition(
        "state_save_qc",
        "save_state_snapshot_array",
        "save_state_snapshot_array.sbatch",
        "complete",
        "failed_publish",
        is_array=True,
    ),
    StageDefinition(
        "frequency",
        "compute_frequency_array",
        "compute_frequency_array.sbatch",
        "complete",
        "failed_parse",
        is_array=True,
    ),
    StageDefinition("publish", "publish_tiles", "publish_tiles.sbatch", "complete", "failed_publish", is_array=False),
)

STAGES: tuple[StageDefinition, ...] = M3_STAGES

ANALYSIS_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(
        "era5_download",
        "analysis_download_source_cycle",
        "analysis_download_source_cycle.sbatch",
        "raw_complete",
        "failed_download",
    ),
    StageDefinition(
        "canonical_convert",
        "analysis_convert_canonical",
        "analysis_convert_canonical.sbatch",
        "canonical_ready",
        "failed_convert",
    ),
    StageDefinition(
        "forcing_produce",
        "analysis_produce_forcing",
        "analysis_produce_forcing.sbatch",
        "forcing_ready",
        "failed_forcing",
    ),
    StageDefinition("analysis_run", "run_shud_analysis", "run_shud_analysis.sbatch", "forecast_running", "failed_run"),
    StageDefinition(
        "parse_output",
        "parse_analysis_output",
        "parse_analysis_output.sbatch",
        "complete",
        "failed_parse",
    ),
    StageDefinition("state_save_qc", "save_state_snapshot", "save_state_snapshot.sbatch", "complete", "failed_publish"),
)

__all__ = [
    "ANALYSIS_STAGES",
    "LEGACY_FORECAST_STAGES",
    "M3_STAGES",
    "STAGES",
]
