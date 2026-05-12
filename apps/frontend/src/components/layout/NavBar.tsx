import { NavLink } from 'react-router-dom'

import { cn } from '@/lib/cn'

const links = [
  { to: '/', label: '水文预报' },
  { to: '/flood-alerts', label: '洪水预警' },
  { to: '/monitoring', label: '产品监控' },
]

export function NavBar() {
  return (
    <nav className="flex items-center gap-1" aria-label="Main navigation">
      {links.map((link) => (
        <NavLink
          key={link.to}
          to={link.to}
          className={({ isActive }) =>
            cn(
              'rounded-md px-3 py-2 text-sm font-medium text-muted transition-colors hover:bg-background hover:text-foreground',
              isActive && 'bg-background text-accent shadow-sm',
            )
          }
          end={link.to === '/'}
        >
          {link.label}
        </NavLink>
      ))}
    </nav>
  )
}
