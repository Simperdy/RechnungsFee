import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { MobileBottomNav } from './MobileBottomNav'

// jsdom kann Viewport-Breiten nicht real auswerten – daher prüfen wir die
// Sichtbarkeitsklassen der Bottom-Nav und die zugehörigen Kern-Routen.
describe('MobileBottomNav', () => {
  it('ist nur unterhalb von 768 px sichtbar (md:hidden)', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <MobileBottomNav />
      </MemoryRouter>,
    )
    const nav = screen.getByTestId('mobile-bottom-nav')
    expect(nav.className).toContain('md:hidden')
    expect(nav.className).toContain('fixed')
    expect(nav.className).toContain('bottom-0')
  })

  it('verlinkt die vier Kern-Routen mit Touch-Ziel-Höhe', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <MobileBottomNav />
      </MemoryRouter>,
    )
    const links = screen.getAllByRole('link')
    expect(links.map((l) => l.getAttribute('href'))).toEqual(['/', '/journal', '/rechnungen', '/kunden'])
    expect(links.map((l) => within(l).getByText(/^(Dashboard|Kassenbuch|Rechnungen|Kunden)$/).textContent))
      .toEqual(['Dashboard', 'Kassenbuch', 'Rechnungen', 'Kunden'])
    for (const link of links) {
      expect(link.className).toContain('flex-1')
      expect(link.className).toContain('min-h-14')
    }
  })
})
