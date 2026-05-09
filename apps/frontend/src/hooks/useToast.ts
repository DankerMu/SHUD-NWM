import { create } from 'zustand'

export interface ToastItem {
  id: string
  title?: string
  description?: string
  variant?: 'default' | 'destructive'
}

interface ToastState {
  toasts: ToastItem[]
  toast: (toast: Omit<ToastItem, 'id'>) => string
  dismiss: (id: string) => void
}

function createToastId() {
  return Math.random().toString(36).slice(2, 10)
}

export const useToast = create<ToastState>((set) => ({
  toasts: [],
  toast: (toast) => {
    const id = createToastId()
    set((state) => ({ toasts: [...state.toasts, { id, ...toast }].slice(-5) }))
    return id
  },
  dismiss: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((toast) => toast.id !== id),
    })),
}))
