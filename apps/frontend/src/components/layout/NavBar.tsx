import { NavLink, useLocation } from 'react-router-dom'
import { Bell, CloudRain, Database, Gauge, Map, Waves } from 'lucide-react'

import { cn } from '@/lib/cn'
import { hasMinimumMeteorologyContracts } from '@/lib/meteorology/contracts'
import { type AuthRole, useAuthStore } from '@/stores/auth'

const links = [
  { to: '/overview', label: '全国总览', icon: Map },
  { to: '/hydro-met', label: '水文气象', icon: CloudRain },
  { to: '/meteorology', label: '气象数据', icon: CloudRain },
  { to: '/forecast', label: '水文预报', icon: Waves },
  { to: '/flood-alerts', label: '洪水预警', icon: Bell },
  { to: '/monitoring', label: '产品监控', icon: Gauge },
  { to: '/system/model-assets', label: '模型资产', icon: Database, roles: ['model_admin', 'sys_admin'] satisfies AuthRole[] },
]

export function NavBar() {
  const location = useLocation()
  const role = useAuthStore((state) => state.role)
  const visibleLinks = links.filter((link) => {
    if (link.to === '/meteorology' && !hasMinimumMeteorologyContracts()) return false
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
