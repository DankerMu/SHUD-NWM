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
import { useMobileForm } from '@/hooks/useMobileForm'
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
  const viewportForm = useMobileForm()

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
      {/* data-* 来自 useMobileForm（JS 侧），--nhms-viewport-form 经 `mobile:` 变体设置（CSS 侧）；二者不可见，供浏览器测试核对两侧结论一致。 */}
      <div
        className="relative flex h-dvh w-full flex-col overflow-hidden bg-background text-foreground [--nhms-viewport-form:desktop] mobile:[--nhms-viewport-form:mobile] pt-[env(safe-area-inset-top)] pr-[env(safe-area-inset-right)] pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)]"
        data-viewport-form={viewportForm.mobile ? 'mobile' : 'desktop'}
        data-viewport-short-landscape={viewportForm.landscape ? 'true' : 'false'}
      >
        <SiteHeader />
        <main className="relative min-h-0 w-full flex-1 overflow-hidden">
          {isRoleOverrideEnabled ? (
            // 移动形态（design.md D5 末段）：收成贴 main 左缘、垂直居中的紧凑触发器，层级在地图浮层
            // （最高 z-[130]）之上、Radix 弹层（--z-popover: 300）之下。角色显示名留在 DOM 里只做视觉截断，
            // 测试靠 getByLabel('Role') 的文本判定当前角色。桌面形态的类名逐字保留。
            <div
              className={
                viewportForm.mobile
                  ? 'absolute left-0 top-1/2 z-[200] -translate-y-1/2'
                  : 'absolute right-4 top-4 z-30'
              }
            >
              <Select value={role} onValueChange={(value) => setRole(value as AuthRole)}>
                <SelectTrigger
                  className={
                    viewportForm.mobile
                      ? 'w-14 gap-0 px-[var(--space-1)] [&>span]:min-w-0 [&>span]:truncate [&>svg]:shrink-0'
                      : 'w-36'
                  }
                  aria-label="Role"
                >
                  <SelectValue />
                </SelectTrigger>
                {/* 移动形态弹层开在触发器右侧：矮视口横屏下触发器下方放不下五个选项。 */}
                <SelectContent
                  align={viewportForm.mobile ? 'center' : 'end'}
                  side={viewportForm.mobile ? 'right' : 'bottom'}
                >
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
