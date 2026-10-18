import { describe, it, expect, vi } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { Modal } from './Modal'
import { LandCombobox } from './LandCombobox'

// Prio 1 der Roadmap (Barrierefreiheit): Modal-Overlays müssen als Dialog
// erkennbar sein (role="dialog", aria-modal, aria-labelledby).

describe('Modal – ARIA-Grundlage', () => {
  it('rendert den Inhalt mit role=dialog, aria-modal und aria-labelledby auf den Titel', () => {
    render(
      <Modal title="Neuer Kunde" onClose={() => {}}>
        <p>Formularinhalt</p>
      </Modal>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Neuer Kunde' })
    expect(dialog).toBeTruthy()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    const titel = screen.getByText('Neuer Kunde')
    expect(dialog.getAttribute('aria-labelledby')).toBe(titel.id)
  })

  it('schließt zuerst die offene Länderliste und erhält die Formulareingaben', () => {
    const onClose = vi.fn()
    render(
      <Modal title="Neuer Kunde" onClose={onClose}>
        <input aria-label="Firmenname" defaultValue="Beispiel GmbH" />
        <LandCombobox value="DE" onChange={() => {}} />
      </Modal>,
    )

    const landEingabe = screen.getByPlaceholderText('Land suchen…')
    fireEvent.focus(landEingabe)
    expect(screen.getByRole('button', { name: 'Deutschland' })).toBeTruthy()

    fireEvent.keyDown(landEingabe, { key: 'Escape' })

    expect(screen.queryByRole('button', { name: 'Deutschland' })).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect((screen.getByLabelText('Firmenname') as HTMLInputElement).value).toBe('Beispiel GmbH')

    fireEvent.keyDown(landEingabe, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('schließt den Dialog per Esc', () => {
    const onClose = vi.fn()
    render(
      <Modal title="Neuer Kunde" onClose={onClose}>
        <p>Formularinhalt</p>
      </Modal>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('hält den Tab-Fokus im Dialog (Focus-Trap vor und zurück)', () => {
    render(
      <Modal title="Neuer Kunde" onClose={() => {}}>
        <button type="button">Abbrechen</button>
        <button type="button">Anlegen</button>
      </Modal>,
    )
    const buttons = screen.getAllByRole('button')
    buttons[0].focus()

    // Letztes Element + Tab -> wieder auf das erste (nicht aus dem Dialog raus)
    buttons[1].focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(buttons[0])

    // Erstes Element + Shift+Tab -> wieder auf das letzte
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(buttons[1])
  })

  it('setzt beim Öffnen den Fokus auf das erste fokussierbare Element im Dialog', () => {
    render(
      <Modal title="Neuer Kunde" onClose={() => {}}>
        <input type="text" aria-label="Firmenname" />
        <button type="button">Anlegen</button>
      </Modal>,
    )
    expect(document.activeElement).toBe(screen.getByLabelText('Firmenname'))
  })

  it('gibt den Fokus beim Schließen an das vorher aktive Element zurück', () => {
    const ausloeser = document.createElement('button')
    document.body.appendChild(ausloeser)
    ausloeser.focus()

    const { unmount } = render(
      <Modal title="Neuer Kunde" onClose={() => {}}>
        <input type="text" aria-label="Firmenname" />
      </Modal>,
    )
    expect(document.activeElement).toBe(screen.getByLabelText('Firmenname'))

    unmount()
    expect(document.activeElement).toBe(ausloeser)
    ausloeser.remove()
  })

  it('stellt nach autoFocus auch unter StrictMode den Fokus auf den Auslöser zurück', () => {
    const ausloeser = document.createElement('button')
    document.body.appendChild(ausloeser)
    ausloeser.focus()

    const { unmount } = render(
      <StrictMode>
        <Modal title="Neuer Kunde" onClose={() => {}}>
          <input autoFocus type="text" aria-label="Firmenname" />
        </Modal>
      </StrictMode>,
    )
    const eingabe = screen.getByLabelText('Firmenname')
    expect(document.activeElement).toBe(eingabe)

    unmount()
    expect(document.activeElement).toBe(ausloeser)
    ausloeser.remove()
  })

  it('bewahrt Fokus bei neuem onClose und verwendet beim Esc den neuesten Callback', () => {
    const ausloeser = document.createElement('button')
    document.body.appendChild(ausloeser)
    ausloeser.focus()
    const alterCallback = vi.fn()
    const neuerCallback = vi.fn()

    const { rerender } = render(
      <Modal title="Neuer Kunde" onClose={alterCallback}>
        <input type="text" aria-label="Firmenname" />
        <button type="button">Fokusziel</button>
      </Modal>,
    )
    const fokusziel = screen.getByRole('button', { name: 'Fokusziel' })
    fokusziel.focus()
    expect(document.activeElement).toBe(fokusziel)

    rerender(
      <Modal title="Neuer Kunde" onClose={neuerCallback}>
        <input type="text" aria-label="Firmenname" />
        <button type="button">Fokusziel</button>
      </Modal>,
    )
    expect(document.activeElement).toBe(fokusziel)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(alterCallback).not.toHaveBeenCalled()
    expect(neuerCallback).toHaveBeenCalledTimes(1)
    ausloeser.remove()
  })
})
