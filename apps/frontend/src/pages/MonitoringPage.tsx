import { RefreshCw } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useMonitoringStore } from '@/stores/monitoring'

export function MonitoringPage() {
  const source = useMonitoringStore((state) => state.source)
  const cycleTime = useMonitoringStore((state) => state.cycleTime)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-foreground">监控工作台</h1>
          <p className="mt-1 text-sm text-muted">
            {source} · {cycleTime ?? '未选择周期'}
          </p>
        </div>
        <Button size="sm" variant="outline">
          <RefreshCw className="size-4" />
          刷新
        </Button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <CardTitle>阶段</CardTitle>
              <Badge variant="secondary">Group 2</Badge>
            </div>
          </CardHeader>
          <CardContent className="text-sm text-muted">React 迁移占位。</CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>作业</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Job ID</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Submitted</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell colSpan={3} className="text-center text-muted">
                    暂无数据
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
