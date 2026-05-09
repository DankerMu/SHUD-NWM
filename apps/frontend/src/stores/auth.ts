import { create } from 'zustand'

export type AuthRole = 'viewer' | 'operator' | 'model_admin' | 'sys_admin'

interface AuthState {
  role: AuthRole
  setRole: (role: AuthRole) => void
}

export const useAuthStore = create<AuthState>((set) => ({
  role: 'viewer',
  setRole: (role) => set({ role }),
}))
