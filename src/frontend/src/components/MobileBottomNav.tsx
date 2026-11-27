import { NavLink } from 'react-router-dom'

// Bottom-Navigation für Smartphones (< 768 px). Ab md: übernimmt wieder die Sidebar.
// Abstände berücksichtigen die iOS-Home-Indicator-Zone (env(safe-area-inset-bottom)).
const BOTTOM_NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: 'home' },
  { to: '/journal', label: 'Kassenbuch', icon: 'menu_book' },
  { to: '/rechnungen', label: 'Rechnungen', icon: 'receipt_long' },
  { to: '/kunden', label: 'Kunden', icon: 'person' },
]

export function MobileBottomNav() {
  return (
    <nav
      data-testid="mobile-bottom-nav"
      aria-label="Hauptnavigation"
      className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-700 flex pb-[env(safe-area-inset-bottom)]"
    >
      {BOTTOM_NAV_ITEMS.map(({ to, label, icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === '/'}
          className={({ isActive }) =>
            `flex-1 flex flex-col items-center justify-center gap-0.5 min-h-14 py-1.5 text-[11px] font-medium transition-colors ${
              isActive
                ? 'text-blue-600 dark:text-blue-400'
                : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-100'
            }`
          }
        >
          {({ isActive }) => (
            <>
              <span
                className={`material-symbols-outlined leading-none ${isActive ? 'text-blue-600 dark:text-blue-400' : ''}`}
                style={{ fontSize: '22px', fontVariationSettings: isActive ? "'FILL' 1" : "'FILL' 0" }}
                aria-hidden="true"
              >
                {icon}
              </span>
              <span className="leading-tight">{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
