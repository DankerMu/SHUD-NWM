import { ChevronRight, Database, Filter, GitBranch, MapPinned, PackageSearch, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/cn'
import {
  buildModelAssetDependencyGraph,
  buildModelAssetKpis,
  buildModelAssetMapProjection,
  buildModelAssetProducts,
  buildModelAssetTree,
  displaySanitizedSource,
  hasRestrictedModelAssetSource,
  MODEL_ASSET_RESTRICTED_SOURCE,
  MODEL_ASSET_UNAVAILABLE,
  type ModelAsset,
  type ModelAssetActiveFilter,
  useModelAssetsStore,
} from '@/stores/modelAssets'

function displayValue(value: unknown) {
  return typeof value === 'string' && value.trim() !== ''
    ? value
    : typeof value === 'number' && Number.isFinite(value)
      ? String(value)
      : MODEL_ASSET_UNAVAILABLE
}

function dateValue(value: unknown) {
  if (typeof value !== 'string' || value.trim() === '') return MODEL_ASSET_UNAVAILABLE
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

function resourceProfile(model: ModelAsset | null): Record<string, unknown> {
  return model?.resource_profile && typeof model.resource_profile === 'object' && !Array.isArray(model.resource_profile)
    ? model.resource_profile
    : {}
}

function nestedRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function firstString(value: unknown) {
  if (!Array.isArray(value)) return null
  for (const entry of value) {
    if (typeof entry === 'string' && entry.trim() !== '') return entry
  }
  return null
}

function MetadataRow({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-3 border-b border-border/70 py-2 text-sm last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd className="break-all font-medium text-foreground">{displayValue(value)}</dd>
    </div>
  )
}

function SourceRow({ label, restricted, value }: { label: string; restricted?: boolean; value: unknown }) {
  const display = typeof value === 'string' ? displaySanitizedSource(value) : restricted ? MODEL_ASSET_RESTRICTED_SOURCE : MODEL_ASSET_UNAVAILABLE
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-3 border-b border-border/70 py-2 text-sm last:border-0">
      <dt className="text-muted">{label}</dt>
      <dd
        className={cn('break-all font-medium text-foreground', display === MODEL_ASSET_RESTRICTED_SOURCE && 'text-warning')}
        title={display}
      >
        {display}
      </dd>
    </div>
  )
}

function MiniMapPreview({ mapProjection }: { mapProjection: ReturnType<typeof buildModelAssetMapProjection> }) {
  if (mapProjection.status !== 'available') {
    return (
      <div className="flex h-40 items-center justify-center rounded-md border border-dashed border-border bg-background text-sm text-muted">
        {mapProjection.text}
      </div>
    )
  }

  return (
    <div className="relative h-40 overflow-hidden rounded-md border border-border bg-primary-50" aria-label="模型资产空间预览">
      <div className="absolute inset-4 rounded-[40%] border-2 border-river bg-river/10" />
      <div className="absolute left-8 right-8 top-1/2 h-0.5 -translate-y-1/2 rotate-[-8deg] bg-river-strong" />
      <div className="absolute bottom-3 left-3 rounded bg-white/90 px-2 py-1 text-xs text-muted shadow-sm">
        {mapProjection.text}
      </div>
    </div>
  )
}

export function ModelAssetsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [active, setActive] = useState<ModelAssetActiveFilter>('all')
  const models = useModelAssetsStore((state) => state.models)
  const selectedModel = useModelAssetsStore((state) => state.selectedModel)
  const loading = useModelAssetsStore((state) => state.loading)
  const detailLoading = useModelAssetsStore((state) => state.detailLoading)
  const error = useModelAssetsStore((state) => state.error)
  const fetchModels = useModelAssetsStore((state) => state.fetchModels)
  const fetchModelDetail = useModelAssetsStore((state) => state.fetchModelDetail)
  const clearSelectedModel = useModelAssetsStore((state) => state.clearSelectedModel)
  const selectedModelId = searchParams.get('modelId')

  useEffect(() => {
    void fetchModels({ active: 'all', limit: 50, offset: 0 }).catch(() => undefined)
  }, [fetchModels])

  const tree = useMemo(
    () => buildModelAssetTree(models, { search: query, active, selectedModelId }),
    [active, models, query, selectedModelId],
  )

  useEffect(() => {
    if (!selectedModelId) {
      clearSelectedModel()
      return
    }
    if (models.length === 0) return
    const exists = models.some((model) => model.model_id === selectedModelId)
    if (!exists || !tree.selectedInFilter) {
      clearSelectedModel()
      return
    }
    if (selectedModel?.model_id === selectedModelId) return
    void fetchModelDetail(selectedModelId).catch(() => undefined)
  }, [clearSelectedModel, fetchModelDetail, models, selectedModel?.model_id, selectedModelId, tree.selectedInFilter])

  function selectModel(modelId: string) {
    setSearchParams((params) => {
      params.set('modelId', modelId)
      return params
    })
  }

  const currentSelectedModel = selectedModel?.model_id === selectedModelId ? selectedModel : null
  const kpis = useMemo(() => buildModelAssetKpis(currentSelectedModel), [currentSelectedModel])
  const profile = resourceProfile(currentSelectedModel)
  const sourceLineage = nestedRecord(profile, 'source_lineage')
  const products = useMemo(() => buildModelAssetProducts(currentSelectedModel), [currentSelectedModel])
  const graph = useMemo(() => buildModelAssetDependencyGraph(currentSelectedModel), [currentSelectedModel])
  const mapProjection = useMemo(() => buildModelAssetMapProjection(currentSelectedModel), [currentSelectedModel])

  return (
    <section className="space-y-4" aria-label="模型资产管理">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-normal text-foreground">模型资产管理</h1>
          <p className="text-sm text-muted">Readonly model package inventory for model_admin and sys_admin.</p>
        </div>
        <Badge variant="outline">只读</Badge>
      </div>

      <div className="grid gap-4 xl:grid-cols-[360px_1fr]">
        <Card className="h-fit">
          <CardHeader className="gap-3">
            <div className="flex items-center justify-between gap-3">
              <CardTitle>流域 / 模型树</CardTitle>
              <span className="text-xs text-muted">{models.length} 个模型</span>
            </div>
            <label className="flex h-10 items-center gap-2 rounded-md border border-border bg-white px-3 text-sm">
              <Search className="h-4 w-4 text-muted" aria-hidden="true" />
              <span className="sr-only">搜索模型资产</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索流域、模型、版本"
                className="min-w-0 flex-1 bg-transparent outline-none"
              />
            </label>
            <div className="flex gap-2" aria-label="模型状态筛选">
              {[
                ['all', '全部'],
                ['true', '启用'],
                ['false', '停用'],
              ].map(([value, label]) => (
                <Button
                  key={value}
                  type="button"
                  size="sm"
                  variant={active === value ? 'default' : 'outline'}
                  onClick={() => setActive(value as ModelAssetActiveFilter)}
                >
                  <Filter className="h-3.5 w-3.5" aria-hidden="true" />
                  {label}
                </Button>
              ))}
            </div>
          </CardHeader>
          <CardContent>
            {loading ? <div className="text-sm text-muted">加载中...</div> : null}
            {error && !detailLoading ? <div className="mb-3 rounded-md border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{error}</div> : null}
            {tree.emptyMessage ? (
              <div className="rounded-md border border-dashed border-border bg-background p-6 text-center text-sm text-muted">
                {tree.emptyMessage}
              </div>
            ) : (
              <div className="space-y-3">
                {tree.groups.map((group) => (
                  <div key={`${group.basinName}:${group.basinId ?? ''}`} className="space-y-2">
                    <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
                      <Database className="h-4 w-4 text-primary-600" aria-hidden="true" />
                      {group.basinName}
                    </div>
                    <div className="space-y-1 pl-3">
                      {group.models.map((model) => (
                        <button
                          key={model.model_id}
                          type="button"
                          onClick={() => selectModel(model.model_id)}
                          className={cn(
                            'flex w-full items-start justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm transition-colors',
                            selectedModelId === model.model_id
                              ? 'border-accent bg-primary-50 text-foreground'
                              : 'border-border bg-white hover:bg-background',
                          )}
                        >
                          <span className="min-w-0">
                            <span className="block break-all font-medium">{displayValue(model.model_name ?? model.model_id)}</span>
                            <span className="block break-all text-xs text-muted">{model.model_id}</span>
                          </span>
                          <span className="flex shrink-0 items-center gap-2">
                            {model.active_flag ? <Badge>启用</Badge> : <Badge variant="secondary">停用</Badge>}
                            <ChevronRight className="h-4 w-4 text-muted" aria-hidden="true" />
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          {!selectedModelId ? (
            <Card>
              <CardContent className="p-8 text-center text-sm text-muted">选择一个模型资产查看详情。</CardContent>
            </Card>
          ) : !tree.selectedInFilter && models.length > 0 ? (
            <Card>
              <CardContent className="p-8 text-center text-sm text-muted">当前筛选条件已排除所选模型。</CardContent>
            </Card>
          ) : detailLoading ? (
            <Card>
              <CardContent className="p-8 text-center text-sm text-muted">详情加载中...</CardContent>
            </Card>
          ) : currentSelectedModel ? (
            <>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {kpis.map((kpi) => (
                  <Card key={kpi.label} data-testid="model-asset-kpi-card">
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm text-muted">{kpi.label}</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <div className="break-all text-base font-semibold">{kpi.value}</div>
                    </CardContent>
                  </Card>
                ))}
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <Card>
                  <CardHeader>
                    <CardTitle>模型元数据</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <dl>
                      <MetadataRow label="模型 ID" value={currentSelectedModel.model_id} />
                      <MetadataRow label="模型名称" value={currentSelectedModel.model_name} />
                      <MetadataRow label="流域 ID" value={currentSelectedModel.basin_id} />
                      <MetadataRow label="流域名称" value={currentSelectedModel.basin_name} />
                      <MetadataRow label="SHUD 输入" value={currentSelectedModel.shud_input_name} />
                      <MetadataRow label="创建时间" value={dateValue(currentSelectedModel.created_at)} />
                    </dl>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle>来源 / 包 lineage</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <dl>
                      <SourceRow
                        label="模型包"
                        value={currentSelectedModel.model_package_uri}
                        restricted={hasRestrictedModelAssetSource(currentSelectedModel, 'model_package_uri')}
                      />
                      <SourceRow
                        label="Manifest"
                        value={currentSelectedModel.manifest_uri}
                        restricted={hasRestrictedModelAssetSource(currentSelectedModel, 'manifest_uri')}
                      />
                      <SourceRow
                        label="Mesh URI"
                        value={currentSelectedModel.mesh_uri}
                        restricted={hasRestrictedModelAssetSource(currentSelectedModel, 'mesh_uri')}
                      />
                      <SourceRow
                        label="Source URI"
                        value={currentSelectedModel.source_uri ?? sourceLineage.source_uri ?? firstString(sourceLineage.uris)}
                        restricted={
                          hasRestrictedModelAssetSource(currentSelectedModel, 'source_uri') ||
                          hasRestrictedModelAssetSource(currentSelectedModel, 'resource_profile.source_lineage.source_uri') ||
                          hasRestrictedModelAssetSource(currentSelectedModel, 'resource_profile.source_lineage.uris')
                        }
                      />
                      <SourceRow
                        label="Source Path"
                        value={currentSelectedModel.source_path ?? sourceLineage.source_path ?? sourceLineage.local_path ?? profile.source_path}
                        restricted={
                          hasRestrictedModelAssetSource(currentSelectedModel, 'source_path') ||
                          hasRestrictedModelAssetSource(currentSelectedModel, 'resource_profile.source_lineage.source_path') ||
                          hasRestrictedModelAssetSource(currentSelectedModel, 'resource_profile.source_lineage.local_path') ||
                          hasRestrictedModelAssetSource(currentSelectedModel, 'resource_profile.source_path')
                        }
                      />
                      <MetadataRow label="包校验" value={currentSelectedModel.package_checksum} />
                    </dl>
                  </CardContent>
                </Card>
              </div>

              <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <GitBranch className="h-4 w-4" aria-hidden="true" />
                      版本时间线 / 依赖图
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="grid gap-3 md:grid-cols-4">
                      {[
                        ['流域', currentSelectedModel.basin_version_id],
                        ['河网', currentSelectedModel.river_network_version_id],
                        ['网格', currentSelectedModel.mesh_version_id],
                        ['率定', currentSelectedModel.calibration_version_id],
                      ].map(([label, value]) => (
                        <div key={label} className="rounded-md border border-border bg-background p-3">
                          <div className="text-xs text-muted">{label}</div>
                          <div className="mt-1 break-all text-sm font-semibold">{displayValue(value)}</div>
                        </div>
                      ))}
                    </div>
                    <div className="grid gap-2 md:grid-cols-2">
                      {graph.nodes.map((node) => (
                        <div
                          key={node.id}
                          className={cn(
                            'rounded-md border p-3 text-sm',
                            node.missing ? 'border-dashed border-border bg-background text-muted' : 'border-border bg-white',
                          )}
                        >
                          <div className="font-medium">{node.label}</div>
                          <div className="break-all text-xs">{node.value}</div>
                        </div>
                      ))}
                    </div>
                    <div className="text-xs text-muted">
                      {graph.edges.length > 0
                        ? graph.edges.map((edge) => `${edge.from} -> ${edge.to}`).join(' / ')
                        : '依赖关系暂不可用'}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <MapPinned className="h-4 w-4" aria-hidden="true" />
                      空间预览
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <MiniMapPreview mapProjection={mapProjection} />
                  </CardContent>
                </Card>
              </div>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <PackageSearch className="h-4 w-4" aria-hidden="true" />
                    产品资产
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {products.items.length === 0 ? (
                    <div className="rounded-md border border-dashed border-border bg-background p-6 text-center text-sm text-muted">
                      暂无产品资产
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[720px] text-left text-sm">
                        <thead className="text-xs text-muted">
                          <tr className="border-b border-border">
                            <th className="py-2 pr-3 font-medium">资产 ID</th>
                            <th className="py-2 pr-3 font-medium">名称</th>
                            <th className="py-2 pr-3 font-medium">Checksum</th>
                            <th className="py-2 font-medium">目标</th>
                          </tr>
                        </thead>
                        <tbody>
                          {products.items.map((product) => (
                            <tr key={product.id} className="border-b border-border/70 last:border-0">
                              <td className="break-all py-2 pr-3 font-medium">{product.id}</td>
                              <td className="break-all py-2 pr-3">{product.label}</td>
                              <td className="break-all py-2 pr-3">{product.checksum}</td>
                              <td className="break-all py-2">{product.target}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {products.notice ? <div className="mt-3 text-sm text-muted">{products.notice}</div> : null}
                </CardContent>
              </Card>
            </>
          ) : (
            <Card>
              <CardContent className="p-8 text-center text-sm text-muted">{error ?? '暂无模型详情'}</CardContent>
            </Card>
          )}
        </div>
      </div>
    </section>
  )
}
