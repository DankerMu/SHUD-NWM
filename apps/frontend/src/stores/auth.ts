import { create } from 'zustand'

export type AuthRole = 'viewer' | 'operator' | 'model_admin' | 'sys_admin'

const authRoles: AuthRole[] = ['viewer', 'operator', 'model_admin', 'sys_admin']
export const isRoleOverrideEnabled = import.meta.env.DEV && import.meta.env.VITE_ENABLE_ROLE_OVERRIDE === 'true'

function configuredRole(): AuthRole {
  const role = import.meta.env.VITE_AUTH_ROLE
  return authRoles.includes(role as AuthRole) ? (role as AuthRole) : 'viewer'
}

interface AuthState {
  role: AuthRole
  setRole: (role: AuthRole) => void
}

export const useAuthStore = create<AuthState>((set) => ({
  role: configuredRole(),
  setRole: (role) => {
    if (isRoleOverrideEnabled) set({ role })
  },
}))
