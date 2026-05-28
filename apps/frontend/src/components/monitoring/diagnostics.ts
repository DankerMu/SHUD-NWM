import type { components } from '@/api/types'
import type { PipelineJob, PipelineStage } from '@/stores/monitoring'

type BasinResult = components['schemas']['BasinResult']

export interface DiagnosticContext {
  sourceId: string
  cycleTime: string
  runId: string | null
  modelId: string | null
}

const diagnosticFields = [
  'source_id',
  'cycle_time',
  'run_id',
  'model_id',
  'stage',
  'job_id',
  'slurm_job_id',
  'status',
  'error_code',
  'error_message',
  'log_uri',
] as const

type DiagnosticField = (typeof diagnosticFields)[number]

type DiagnosticInput = Partial<Record<DiagnosticField, string | null | undefined>>

function addDiagnosticField(payload: Partial<Record<DiagnosticField, string>>, key: DiagnosticField, value: string | null | undefined) {
  const trimmed = typeof value === 'string' ? value.trim() : ''
  if (trimmed) payload[key] = trimmed
}

export function buildSafeDiagnosticPayload(input: DiagnosticInput) {
  const payload: Partial<Record<DiagnosticField, string>> = {}
  diagnosticFields.forEach((field) => addDiagnosticField(payload, field, input[field]))
  return payload
}

export function buildJobDiagnosticPayload(job: PipelineJob, context: DiagnosticContext) {
  return buildSafeDiagnosticPayload({
    source_id: context.sourceId,
    cycle_time: context.cycleTime,
    run_id: context.runId,
    model_id: context.modelId,
    stage: job.stage ?? job.job_type,
    job_id: job.job_id,
    slurm_job_id: job.slurm_job_id,
    status: job.status,
    error_code: job.error_code,
    error_message: job.error_message,
    log_uri: job.log_uri,
  })
}

export function buildStageDiagnosticPayload(stage: PipelineStage, context: DiagnosticContext) {
  return buildSafeDiagnosticPayload({
    source_id: context.sourceId,
    cycle_time: context.cycleTime,
    run_id: context.runId,
    model_id: context.modelId,
    stage: stage.stage,
    status: stage.display_status ?? stage.status,
  })
}

export function buildBasinDiagnosticPayload(stage: PipelineStage, result: BasinResult, context: DiagnosticContext) {
  return buildSafeDiagnosticPayload({
    source_id: context.sourceId,
    cycle_time: context.cycleTime,
    run_id: context.runId ?? result.run_id,
    model_id: context.modelId ?? result.model_id,
    stage: result.stage ?? stage.stage,
    job_id: result.job_id,
    slurm_job_id: result.slurm_job_id,
    status: result.status,
    error_code: result.error_code,
    error_message: result.error_message,
    log_uri: result.log_uri,
  })
}
