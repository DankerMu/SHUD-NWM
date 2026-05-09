import { Waves } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { ForecastSegmentInfo } from '@/stores/forecast'

interface SegmentInfoProps {
  segment: ForecastSegmentInfo
}

export function SegmentInfo({ segment }: SegmentInfoProps) {
  return (
    <Card>
      <CardHeader className="p-4 pb-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Waves className="size-4 flex-none text-river" />
              <span className="truncate">{segment.name ?? segment.segmentId}</span>
            </CardTitle>
            <p className="mt-1 break-all text-xs text-muted">{segment.segmentId}</p>
          </div>
          {segment.streamOrder ? (
            <Badge variant="secondary" className="flex-none">
              Order {segment.streamOrder}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="grid gap-2 p-4 pt-1 text-xs">
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted">流域版本</span>
          <span className="min-w-0 truncate font-medium text-foreground">{segment.basinVersionId ?? '-'}</span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted">河网版本</span>
          <span className="min-w-0 truncate font-medium text-foreground">
            {segment.riverNetworkVersionId ?? '-'}
          </span>
        </div>
      </CardContent>
    </Card>
  )
}
