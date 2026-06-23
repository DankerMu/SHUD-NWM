import { useEffect, type ReactNode } from 'react'

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Toast,
  ToastClose,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from '@/components/ui/toast'
import { cn } from '@/lib/cn'
import { isRoleOverrideEnabled, type AuthRole, useAuthStore } from '@/stores/auth'
import { useMonitoringStore } from '@/stores/monitoring'
import { useToast } from '@/hooks/useToast'

import { SiteHeader } from './SiteHeader'

const roleOptions: Array<{ value: AuthRole; label: string }> = [
  { value: 'viewer', label: 'Viewer' },
  { value: 'analyst', label: 'Analyst' },
  { value: 'operator', label: 'Operator' },
  { value: 'model_admin', label: 'Model Admin' },
  { value: 'sys_admin', label: 'Sys Admin' },
]

const toastDuration = import.meta.env.MODE === 'test' ? Number.POSITIVE_INFINITY : undefined

interface AppShellProps {
  children: ReactNode
}

export function AppShell({ children }: AppShellProps) {
  const role = useAuthStore((state) => state.role)
  const setRole = useAuthStore((state) => state.setRole)
  const { toasts, dismiss } = useToast()

  // runtime config 全局唯一加载点（去 NavBar 后迁到此处）：
  // 加载到既有 store，display_readonly 检测的唯一来源，不新造 fetch。
  const runtimeConfig = useMonitoringStore((state) => state.runtimeConfig)
  const runtimeConfigError = useMonitoringStore((state) => state.runtimeConfigError)
  const fetchRuntimeConfig = useMonitoringStore((state) => state.fetchRuntimeConfig)

  useEffect(() => {
    if (runtimeConfig || runtimeConfigError) return
    void fetchRuntimeConfig()
  }, [fetchRuntimeConfig, runtimeConfig, runtimeConfigError])

  return (
    <ToastProvider duration={toastDuration}>
      <div className="relative flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
        <SiteHeader />
        <main className="relative min-h-0 w-full flex-1 overflow-hidden">
          {isRoleOverrideEnabled ? (
            <div className="absolute right-4 top-4 z-30">
              <Select value={role} onValueChange={(value) => setRole(value as AuthRole)}>
                <SelectTrigger className="w-36" aria-label="Role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent align="end">
                  {roleOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          {children}
        </main>
      </div>
      {toasts.map((toast) => (
        <Toast
          key={toast.id}
          className={cn(
            toast.variant === 'destructive' &&
              'border-danger bg-danger text-white [&_button]:text-white',
          )}
          open
          onOpenChange={(open) => {
            if (!open) dismiss(toast.id)
          }}
        >
          {toast.title ? <ToastTitle>{toast.title}</ToastTitle> : null}
          {toast.description ? <ToastDescription>{toast.description}</ToastDescription> : null}
          <ToastClose />
        </Toast>
      ))}
      <ToastViewport />
    </ToastProvider>
  )
}
