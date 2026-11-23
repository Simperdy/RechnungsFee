import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getSetupStatus, isTauri } from './client'

// --- Tests für den 401-Redirect im Web-Modus (Server-Modus) ---------------
// Tauri: Verhalten unverändert (Error werfen). Web: Weiterleitung auf den
// OIDC-Login-Endpunkt, damit die PWA nach abgelaufener Session nicht in einer
// Fehlerwüste landet.

function mockFetch(status: number, body: unknown = { detail: 'nope' }) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    // Wie fetch(): Response.json() konsumiert den Body – beim zweiten Aufruf werfen
    json: async () => JSON.parse(JSON.stringify(body)),
    headers: new Headers(),
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('client request: 401-Redirect', () => {
  const originalLocation = window.location

  beforeEach(() => {
    document.documentElement.innerHTML = '<head></head><body></body>'
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // jsdom: window.location ist nicht-configurable, aber der getter auf dem
    // window-Objekt lässt sich überschreiben – hier sauber zurücksetzen.
    Object.defineProperty(window, 'location', { value: originalLocation, configurable: true, writable: true })
  })

  it('leitet im Web-Modus bei 401 auf /api/auth/login um', async () => {
    const assign = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...originalLocation, assign, href: originalLocation.href },
      configurable: true,
      writable: true,
    })
    mockFetch(401)

    await expect(getSetupStatus()).rejects.toThrow()

    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith('/api/auth/login')
  })

  it('leitet bei 500 nicht um, sondern wirft den Backend-Fehler', async () => {
    const assign = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...originalLocation, assign, href: originalLocation.href },
      configurable: true,
      writable: true,
    })
    mockFetch(500, { detail: 'Interner Fehler' })

    await expect(getSetupStatus()).rejects.toThrow('Interner Fehler')

    expect(assign).not.toHaveBeenCalled()
  })

  it('wirft im Tauri-Modus bei 401 weiterhin einen Error ohne Redirect', async () => {
    const assign = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { ...originalLocation, assign, href: originalLocation.href },
      configurable: true,
      writable: true,
    })
    vi.stubGlobal('__TAURI_INTERNALS__', {})
    expect(isTauri()).toBe(true)
    mockFetch(401, { detail: 'Nicht angemeldet' })

    await expect(getSetupStatus()).rejects.toThrow('Nicht angemeldet')

    expect(assign).not.toHaveBeenCalled()
  })
})
