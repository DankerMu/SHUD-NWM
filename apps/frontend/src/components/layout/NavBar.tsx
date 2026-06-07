import { useEffect } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Bell, CloudRain, Database, Gauge, Map, Settings2, Waves } from 'lucide-react'

import { cn } from '@/lib/cn'
import { hasMinimumMeteorologyContracts } from '@/lib/meteorology/contracts'
import { type AuthRole, useAuthStore } from '@/stores/auth'
import { isDisplayReadonlyRuntimeConfig, useMonitoringStore } from '@/stores/monitoring'

// display_readonly 模式下从主导航降级的运维入口（路由仍保留、仅隐藏入口）。
const displayReadonlyDowngradedRoutes = new Set(['/ops', '/monitoring'])

const links = [
  { to: '/overview', label: '全国总览', icon: Map },
  { to: '/hydro-met', label: '水文气象', icon: CloudRain },
  { to: '/meteorology', label: '气象数据', icon: CloudRain },
  { to: '/forecast', label: '水文预报', icon: Waves },
  { to: '/flood-alerts', label: '洪水预警', icon: Bell },
  { to: '/ops', label: '内部诊断', icon: Settings2, roles: ['operator', 'model_admin', 'sys_admin'] satisfies AuthRole[] },
  { to: '/monitoring', label: '产品监控', icon: Gauge },
  { to: '/system/model-assets', label: '模型资产', icon: Database, roles: ['model_admin', 'sys_admin'] satisfies AuthRole[] },
]

export function NavBar() {
  const location = useLocation()
  const role = useAuthStore((state) => state.role)
  const runtimeConfig = useMonitoringStore((state) => state.runtimeConfig)
  const runtimeConfigError = useMonitoringStore((state) => state.runtimeConfigError)
  const fetchRuntimeConfig = useMonitoringStore((state) => state.fetchRuntimeConfig)

  // NavBar 全局挂载，借此把 runtime config 加载到既有 store（不新造 fetch）。
  useEffect(() => {
    if (runtimeConfig || runtimeConfigError) return
    void fetchRuntimeConfig()
  }, [fetchRuntimeConfig, runtimeConfig, runtimeConfigError])

  // Fail-safe：config 未就绪时按非 display_readonly 处理（默认显示），就绪确认 display_readonly 后收起。
  // /ops、/monitoring 仅是 role-gated 诊断面，只读边界由后端强制，导航降级属业务化展示而非安全边界。
  const displayReadonly = isDisplayReadonlyRuntimeConfig(runtimeConfig)

  const visibleLinks = links.filter((link) => {
    if (link.to === '/meteorology' && !hasMinimumMeteorologyContracts()) return false
    if (displayReadonly && displayReadonlyDowngradedRoutes.has(link.to)) return false
    if ('roles' in link && !link.roles.includes(role)) return false
    return true
  })

  return (
    <nav className="flex items-center gap-1" aria-label="Main navigation">
      {visibleLinks.map((link) => {
        const Icon = link.icon
        return (
          <NavLink
            key={link.to}
            to={link.to}
            className={({ isActive }) =>
              cn('flex h-14 items-center gap-2 border-b-2 px-4 text-sm font-medium transition-colors hover:border-white/80 hover:bg-primary-800 hover:text-white/90', {
                'border-accent text-white': isActive || (link.to === '/overview' && location.pathname === '/'),
                'border-transparent text-white/70': !(isActive || (link.to === '/overview' && location.pathname === '/')),
              })
            }
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            {link.label}
          </NavLink>
        )
      })}
    </nav>
  )
}
