import { Activity, Map } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export function ForecastPage() {
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="min-h-[28rem] rounded-lg border border-border bg-panel p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-muted">
          <Map className="size-4" />
          全国河网
        </div>
      </section>
      <aside className="space-y-4">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <CardTitle>预报工作台</CardTitle>
              <Badge variant="secondary">Group 3</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4 text-sm text-muted">
            <p>React 迁移占位。</p>
            <Button size="sm" variant="outline">
              <Activity className="size-4" />
              刷新
            </Button>
          </CardContent>
        </Card>
      </aside>
    </div>
  )
}
