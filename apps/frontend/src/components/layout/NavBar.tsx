import { NavLink, useLocation } from 'react-router-dom'
import { Bell, Gauge, Map, Waves } from 'lucide-react'

import { cn } from '@/lib/cn'

const links = [
  { to: '/overview', label: '全国总览', icon: Map },
  { to: '/forecast', label: '水文预报', icon: Waves },
  { to: '/flood-alerts', label: '洪水预警', icon: Bell },
  { to: '/monitoring', label: '产品监控', icon: Gauge },
]

export function NavBar() {
  const location = useLocation()

  return (
    <nav className="flex items-center gap-1" aria-label="Main navigation">
      {links.map((link) => {
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
