import { useEffect, useState, type ReactNode } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { type AuthRole, useAuthStore } from '@/stores/auth'
import { isDisplayReadonlyRuntimeConfig, useMonitoringStore } from '@/stores/monitoring'

interface RBACGateProps {
  roles: AuthRole[]
  allowDisplayReadonly?: boolean
  children: ReactNode
}

export const DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS = 10_000

export function RBACGate({ roles, allowDisplayReadonly = false, children }: RBACGateProps) {
  const role = useAuthStore((state) => state.role)
  const runtimeConfig = useMonitoringStore((state) => state.runtimeConfig)
  const runtimeConfigError = useMonitoringStore((state) => state.runtimeConfigError)
  const fetchRuntimeConfig = useMonitoringStore((state) => state.fetchRuntimeConfig)
  const [runtimeConfigTimedOut, setRuntimeConfigTimedOut] = useState(false)
  const displayReadonlyAllowed = allowDisplayReadonly && isDisplayReadonlyRuntimeConfig(runtimeConfig)
  const roleAllowed = roles.includes(role)
  const waitingForRuntimeConfig = allowDisplayReadonly && !roleAllowed && runtimeConfig === null && runtimeConfigError === null

  useEffect(() => {
    if (!waitingForRuntimeConfig) {
      setRuntimeConfigTimedOut(false)
      return
    }
    void fetchRuntimeConfig()
    const timer = window.setTimeout(() => setRuntimeConfigTimedOut(true), DISPLAY_READONLY_RUNTIME_CONFIG_DEADLINE_MS)
    return () => window.clearTimeout(timer)
  }, [fetchRuntimeConfig, waitingForRuntimeConfig])

  if (waitingForRuntimeConfig && !runtimeConfigTimedOut) {
    return <div role="status">正在确认只读诊断访问策略…</div>
  }

  if (!displayReadonlyAllowed && !roleAllowed) {
    return (
      <Card role="alert" className="max-w-lg">
        <CardHeader>
          <CardTitle>权限不足</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted">当前角色无法访问该页面。</CardContent>
      </Card>
    )
  }

  return <>{children}</>
}
