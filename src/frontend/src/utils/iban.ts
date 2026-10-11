// IBAN-Validierung + OpenIBAN-Web-Lookup, gemeinsam genutzt von StepKonto.tsx (Setup),
// KontenPage.tsx (Stammdaten) und UnternehmenPage.tsx (Einstellungen) - vorher dreifach
// identisch in allen drei Dateien kopiert.

/** Mod-97-Prüfziffer nach ISO 13616 - gilt für IBANs aller Länder, nicht nur DE. */
export function pruefeIbanChecksum(iban: string): boolean {
  const moved = iban.slice(4) + iban.slice(0, 4)
  let digits = ''

  for (const ch of moved) {
    digits += /[A-Z]/.test(ch) ? String(ch.charCodeAt(0) - 55) : ch
  }

  let rest = 0
  for (const ch of digits) {
    rest = (rest * 10 + Number(ch)) % 97
  }

  return rest === 1
}

/** Gibt eine Fehlermeldung zurück, oder null wenn die IBAN gültig ist. Erwartet die IBAN
 * bereits ohne Leerzeichen und in Großbuchstaben. */
export function validiereIban(iban: string): string | null {
  if (iban.startsWith('DE')) {
    if (iban.length < 22) return 'Zu wenig Zeichen'
    if (iban.length > 22) return 'Zu viele Zeichen'
    if (!pruefeIbanChecksum(iban)) return 'Bitte gib eine gültige IBAN ein.'
    return null
  }
  if (iban.length < 15) return 'Zu wenig Zeichen'
  if (iban.length > 34) return 'Zu viele Zeichen'
  if (!/^[A-Z]{2}[0-9A-Z]+$/.test(iban)) return 'Bitte gib eine gültige IBAN ein.'
  if (!pruefeIbanChecksum(iban)) return 'Bitte gib eine gültige IBAN ein.'
  return null
}

export async function ladeOpenIbanDaten(iban: string) {
  const res = await fetch(`https://openiban.com/validate/${encodeURIComponent(iban)}?getBIC=true`)
  if (!res.ok) throw new Error('OpenIBAN-Request fehlgeschlagen')
  return res.json() as Promise<{ valid: boolean; bankData?: { bic?: string; name?: string } }>
}

export const OPEN_IBAN_BANK_FALLBACK = 'Bank konnte nicht automatisch ermittelt werden. Bitte gib sie manuell ein.'
export const OPEN_IBAN_BIC_FALLBACK = 'BIC / SWIFT konnte nicht automatisch ermittelt werden. Bitte gib sie manuell ein.'

const OPEN_IBAN_LAND_NAMEN: Record<string, string> = {
  BE: 'Belgien', DE: 'Deutschland', NL: 'Niederlande', LU: 'Luxemburg',
  CH: 'Schweiz', AT: 'Österreich', LI: 'Liechtenstein',
}

export function openIbanBankdatenFehlgeschlagen(iban: string): string {
  const cc = iban.slice(0, 2)
  const supported = Object.values(OPEN_IBAN_LAND_NAMEN).join(', ')
  return `Für IBANs mit ${cc} bietet openiban.com keine Bankdaten an. Bitte Bank und BIC manuell eingeben. (Offiziell unterstützt: ${supported}.)`
}
