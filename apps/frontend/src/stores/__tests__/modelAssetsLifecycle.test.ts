import { beforeEach, describe, expect, it, vi } from 'vitest'

import { client } from '@/api/client'
import { useModelAssetsStore } from '@/stores/modelAssets'

import { BASINS_MODEL_ID, makeBasinsModel, resetStore, success } from './modelAssetsFixtures'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}))

describe('useModelAssetsStore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetStore()
  })

  it('runs lifecycle preflight and successful operation through typed API paths', async () => {
    const model = makeBasinsModel({ active_flag: false, lifecycle_state: 'inactive' })
    const activeModel = makeBasinsModel({ active_flag: true, lifecycle_state: 'active' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'activate',
      status: 'ready',
      model_id: BASINS_MODEL_ID,
      basin_version_id: model.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'allowed',
      operation: 'activate',
      model: activeModel,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: BASINS_MODEL_ID, log_id: 5 },
    }

    vi.mocked(client.POST).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      if (path === '/api/v1/models/{model_id}/preflight') return success(preflight) as never
      if (path === '/api/v1/models/{model_id}/lifecycle') return success(result) as never
      throw new Error(`Unexpected POST ${path}`)
    })

    const preflightResponse = await useModelAssetsStore.getState().preflightModelOperation(BASINS_MODEL_ID, {
      operation: 'activate',
      override_missing_active: false,
    })
    const resultResponse = await useModelAssetsStore.getState().runModelOperation(BASINS_MODEL_ID, {
      operation: 'activate',
      override_missing_active: false,
    })

    expect(preflightResponse.status).toBe('ready')
    expect(resultResponse.audit_reference).toEqual({ entity_type: 'model_instance', entity_id: BASINS_MODEL_ID, log_id: 5 })
    expect(useModelAssetsStore.getState().operationResult?.status).toBe('allowed')
    expect(useModelAssetsStore.getState().operationPreflight?.status).toBe('ready')
  })

  it('forwards selected rollback previous model id through preflight and lifecycle calls', async () => {
    const outgoing = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const staleSibling = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'superseded' })
    const selectedPrior = makeBasinsModel({ model_id: 'model-c', active_flag: false, lifecycle_state: 'superseded' })
    const restored = makeBasinsModel({ ...selectedPrior, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...outgoing, active_flag: false, lifecycle_state: 'superseded' })
    const postedBodies: unknown[] = []
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'rollback_version',
      status: 'ready',
      model_id: outgoing.model_id,
      basin_version_id: outgoing.basin_version_id,
      current_active_model_id: outgoing.model_id,
      previous_model_id: selectedPrior.model_id,
      restored_model_id: selectedPrior.model_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'rollback',
      operation: 'rollback_version',
      model: restored,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: outgoing.model_id, log_id: 11 },
    }
    useModelAssetsStore.setState({
      models: [outgoing, staleSibling, selectedPrior],
      selectedModel: outgoing,
    })
    vi.mocked(client.POST).mockImplementation(async (...args: unknown[]) => {
      const path = String(args[0])
      const body = (args[1] as { body?: unknown } | undefined)?.body
      postedBodies.push(body)
      if (path === '/api/v1/models/{model_id}/preflight') return success(preflight) as never
      if (path === '/api/v1/models/{model_id}/lifecycle') return success(result) as never
      throw new Error(`Unexpected POST ${path}`)
    })

    await useModelAssetsStore.getState().preflightModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: selectedPrior.model_id,
      override_missing_active: false,
    })
    await useModelAssetsStore.getState().runModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: selectedPrior.model_id,
      override_missing_active: false,
    })

    expect(postedBodies).toEqual([
      { operation: 'rollback_version', override_missing_active: false, previous_model_id: selectedPrior.model_id },
      { operation: 'rollback_version', override_missing_active: false, previous_model_id: selectedPrior.model_id },
    ])
    expect(JSON.stringify(postedBodies)).not.toContain(staleSibling.model_id)
    expect(useModelAssetsStore.getState().operationPreflight?.restored_model_id).toBe(selectedPrior.model_id)
  })

  it('merges lifecycle result and previous model after activation', async () => {
    const previous = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const candidate = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'inactive' })
    const activated = makeBasinsModel({ ...candidate, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...previous, active_flag: false, lifecycle_state: 'superseded' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'activate',
      status: 'ready',
      model_id: candidate.model_id,
      basin_version_id: candidate.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'allowed',
      operation: 'activate',
      model: activated,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: candidate.model_id, log_id: 9 },
    }

    useModelAssetsStore.setState({
      models: [previous, candidate],
      selectedModel: previous,
    })
    vi.mocked(client.POST).mockResolvedValue(success(result) as never)

    await useModelAssetsStore.getState().runModelOperation(candidate.model_id, { operation: 'activate', override_missing_active: false })

    const state = useModelAssetsStore.getState()
    expect(state.models.filter((model) => model.active_flag)).toHaveLength(1)
    expect(state.models.find((model) => model.model_id === candidate.model_id)).toMatchObject({
      active_flag: true,
      lifecycle_state: 'active',
    })
    expect(state.models.find((model) => model.model_id === previous.model_id)).toMatchObject({
      active_flag: false,
      lifecycle_state: 'superseded',
    })
    expect(state.selectedModel).toMatchObject({ model_id: previous.model_id, active_flag: false, lifecycle_state: 'superseded' })
  })

  it('merges rollback result and demotes the selected outgoing model', async () => {
    const outgoing = makeBasinsModel({ model_id: 'model-a', active_flag: true, lifecycle_state: 'active' })
    const restoredBefore = makeBasinsModel({ model_id: 'model-b', active_flag: false, lifecycle_state: 'superseded' })
    const restored = makeBasinsModel({ ...restoredBefore, active_flag: true, lifecycle_state: 'active' })
    const demoted = makeBasinsModel({ ...outgoing, active_flag: false, lifecycle_state: 'superseded' })
    const preflight = {
      schema: 'nhms.model_operation_preflight.v1',
      operation: 'rollback_version',
      status: 'ready',
      model_id: outgoing.model_id,
      basin_version_id: outgoing.basin_version_id,
      blockers: [],
      warnings: [],
      impact: { downstream_surfaces: ['forecast-routing'] },
    }
    const result = {
      status: 'rollback',
      operation: 'rollback_version',
      model: restored,
      previous_model: demoted,
      preflight,
      audit_reference: { entity_type: 'model_instance', entity_id: outgoing.model_id, log_id: 10 },
    }

    useModelAssetsStore.setState({
      models: [outgoing, restoredBefore],
      selectedModel: outgoing,
    })
    vi.mocked(client.POST).mockResolvedValue(success(result) as never)

    await useModelAssetsStore.getState().runModelOperation(outgoing.model_id, {
      operation: 'rollback_version',
      previous_model_id: restoredBefore.model_id,
      override_missing_active: false,
    })

    const state = useModelAssetsStore.getState()
    expect(state.models.filter((model) => model.active_flag)).toHaveLength(1)
    expect(state.models.find((model) => model.model_id === restoredBefore.model_id)).toMatchObject({
      active_flag: true,
      lifecycle_state: 'active',
    })
    expect(state.models.find((model) => model.model_id === outgoing.model_id)).toMatchObject({
      active_flag: false,
      lifecycle_state: 'superseded',
    })
    expect(state.selectedModel).toMatchObject({ model_id: outgoing.model_id, active_flag: false, lifecycle_state: 'superseded' })
  })

  it('keeps backend forbidden lifecycle errors generic and non-successful', async () => {
    vi.mocked(client.POST).mockResolvedValue(
      { data: undefined, error: { error: { code: 'RBAC_FORBIDDEN', message: 'Actor roles are not authorized.' } } } as never,
    )

    await expect(
      useModelAssetsStore.getState().runModelOperation(BASINS_MODEL_ID, { operation: 'activate', override_missing_active: false }),
    ).rejects.toThrow('模型操作执行失败')

    expect(useModelAssetsStore.getState()).toMatchObject({
      operationLoading: false,
      operationError: '模型操作执行失败',
      operationResult: null,
    })
  })
})
