import type { ReactNode } from 'react'

// Gruppiert eine Liste für die Mobile-Ansicht in Karten. Ab md: unsichtbar,
// dort rendern die Seiten ihre gewohnten Tabellen (hidden md:block).
export function MobileCardList({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="md:hidden space-y-2" data-testid="mobile-card-list">
      {hint && <p className="text-xs text-slate-400 dark:text-slate-500">{hint}</p>}
      {children}
    </div>
  )
}

// Eine Karte in einer MobileCardList – Fläche komplett antippbar (min. 44 px hoch).
export function MobileCard({
  onClick,
  aktiv = false,
  children,
}: {
  onClick?: () => void
  aktiv?: boolean
  children: ReactNode
}) {
  const base = `w-full text-left min-h-11 px-4 py-3 rounded-xl border transition-colors ${
    aktiv
      ? 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800'
      : 'bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700'
  } ${onClick ? 'active:bg-slate-50 dark:active:bg-slate-700' : ''}`

  if (!onClick) return <div className={base}>{children}</div>
  return (
    <button type="button" onClick={onClick} className={base}>
      {children}
    </button>
  )
}
