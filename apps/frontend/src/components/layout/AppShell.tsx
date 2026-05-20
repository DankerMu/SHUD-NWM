import type { ReactNode } from 'react'

import { NavBar } from '@/components/layout/NavBar'
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
import { useToast } from '@/hooks/useToast'

const roleOptions: Array<{ value: AuthRole; label: string }> = [
  { value: 'viewer', label: 'Viewer' },
  { value: 'analyst', label: 'Analyst' },
  { value: 'operator', label: 'Operator' },
  { value: 'model_admin', label: 'Model Admin' },
  { value: 'sys_admin', label: 'Sys Admin' },
]

interface AppShellProps {
  children: ReactNode
}

export function AppShell({ children }: AppShellProps) {
  const role = useAuthStore((state) => state.role)
  const setRole = useAuthStore((state) => state.setRole)
  const { toasts, dismiss } = useToast()

  return (
    <ToastProvider>
      <div className="min-h-screen bg-background text-foreground">
        <header className="sticky top-0 z-20 border-b border-primary-800 bg-primary-900 text-white shadow-sm">
          <div className="flex h-14 items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
            <div className="flex items-center gap-6">
              <div className="text-base font-semibold text-white">NHMS</div>
              <NavBar />
            </div>
            {isRoleOverrideEnabled ? (
              <Select value={role} onValueChange={(value) => setRole(value as AuthRole)}>
                <SelectTrigger className="w-36 border-white/30 bg-white/10 text-white" aria-label="Role">
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
            ) : null}
          </div>
        </header>
        <main className="w-full max-w-[100vw] overflow-x-hidden px-4 py-4 sm:px-6 lg:px-8">{children}</main>
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
