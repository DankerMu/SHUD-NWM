import { create } from 'zustand'

export type AuthRole = 'viewer' | 'operator' | 'model_admin' | 'sys_admin'
export type OperatorRole = Exclude<AuthRole, 'viewer'>

const authRoles: AuthRole[] = ['viewer', 'operator', 'model_admin', 'sys_admin']
const operatorRoles: OperatorRole[] = ['operator', 'model_admin', 'sys_admin']
export const isRoleOverrideEnabled = import.meta.env.DEV && import.meta.env.VITE_ENABLE_ROLE_OVERRIDE === 'true'

function configuredRole(): AuthRole {
  const role = import.meta.env.VITE_AUTH_ROLE
  return authRoles.includes(role as AuthRole) ? (role as AuthRole) : 'viewer'
}

function isOperatorRole(role: AuthRole): role is OperatorRole {
  return operatorRoles.includes(role as OperatorRole)
}

export function canUseDevRoleActions(role: AuthRole): role is OperatorRole {
  return isRoleOverrideEnabled && isOperatorRole(role)
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
