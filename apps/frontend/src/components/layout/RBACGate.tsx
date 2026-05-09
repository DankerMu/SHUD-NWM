import type { ReactNode } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuthStore } from '@/stores/auth'

interface RBACGateProps {
  roles: string[]
  children: ReactNode
}

export function RBACGate({ roles, children }: RBACGateProps) {
  const role = useAuthStore((state) => state.role)

  if (!roles.includes(role)) {
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
