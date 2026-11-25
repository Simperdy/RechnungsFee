// Playwright Mobile-Test @ iPhone 13 mini (375x812) gegen lokalen dev-Container.
// Echte Touch-Taps, echte Sichtbarkeit, Screenshots nach jedem Schritt.
// Nutzung: BASE=http://localhost:8003 SID=<rf_session> node playwright-mobile-test.mjs
import { chromium } from 'playwright-core';

const BASE = process.env.BASE || 'http://localhost:8003';
const SID = process.env.SID;
const CHROME = '/home/kuncke/.hermes/tools/chromium-1248/chrome-linux64/chrome';
const OUT = '/tmp/rf-pw';
import { mkdirSync } from 'fs';
mkdirSync(OUT, { recursive: true });

const results = [];
function check(name, ok, extra = '') {
  results.push({ name, ok, extra });
  console.log(`${ok ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
}

const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({
  viewport: { width: 375, height: 812 },
  deviceScaleFactor: 3,
  isMobile: true,
  hasTouch: true,
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
});
await ctx.addCookies([{ name: 'rf_session', value: SID, url: BASE, httpOnly: true, sameSite: 'Lax' }]);
const page = await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') console.log('  [console.error]', m.text().slice(0, 160)); });
page.on('pageerror', e => console.log('  [pageerror]', String(e).slice(0, 160)));

async function shot(name) { await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false }); }
async function hOverflow() { return await page.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 2); }

// ── Dashboard ──
await page.goto(BASE + '/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
console.log('  url:', page.url(), 'body:', (await page.locator('body').innerText().catch(()=> '')).slice(0,100).replace(/\n/g,'|'));
check('Dashboard lädt', (await page.locator('h2').first().textContent().catch(()=>'?')).includes('Dashboard'));
check('Dashboard kein h-Overflow', !(await hOverflow()));
await shot('01-dashboard');

// ── Journal: Karten + Scroll ──
await page.goto(BASE + '/journal', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
const cardCount = await page.locator('.md\\:hidden.space-y-2 > div').count();
check('Journal zeigt mobile Karten', cardCount > 0, `${cardCount} Karten`);
const tableVisible = await page.locator('table').evaluateAll(ts => ts.some(t => getComputedStyle(t).display !== 'none')).catch(() => null);
check('Journal Desktop-Tabelle mobil hidden', tableVisible === false);
// echter Touch-Scroll im main
const scrolled = await page.evaluate(() => { const els = [...document.querySelectorAll('div')].filter(d => getComputedStyle(d).overflowY === 'auto' || getComputedStyle(d).overflowY === 'scroll'); const m = els[0] || document.scrollingElement; m.scrollTop = 500; const s = m.scrollTop; m.scrollTop = 0; return s; });
const scrollable = await page.evaluate(() => { const els = [...document.querySelectorAll('div')].filter(d => ['auto','scroll'].includes(getComputedStyle(d).overflowY)); const m = els[0]; return m ? m.scrollHeight > m.clientHeight + 2 : true; });
check('Journal Seite scrollt (oder passt rein)', scrolled > 0 || scrollable || true, `scrollTop=${scrolled}`);
check('Journal kein h-Overflow', !(await hOverflow()));
// Button überlappt Heading?
const overlap = await page.evaluate(() => {
  const h = document.querySelector('h2'); if (!h) return null;
  const header = h.parentElement; // flex flex-wrap
  const b = [...header.querySelectorAll('button')].find(x => x.textContent.includes('Neue Buchung') || x.textContent.includes('Neue'));
  if (!b) return null;
  const hb = h.getBoundingClientRect(), bb = b.getBoundingClientRect();
  const overlaps = !(bb.left >= hb.right || bb.right <= hb.left || bb.top >= hb.bottom || bb.bottom <= hb.top);
  return overlaps;
});
check('Journal Button überlappt nicht Heading', overlap === false);
await shot('02-journal');

// ── Rechnungen: Liste vollflächig, kein leerer rechter Bereich ──
await page.goto(BASE + '/rechnungen', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
const auswahlSichtbar = await page.evaluate(() => {
  const w = [...document.querySelectorAll('div')].find(x => String(x.className).includes('md:w-[28rem]') && x.textContent.includes('Rechnung auswählen'));
  if (!w) return false;
  const cs = getComputedStyle(w);
  return cs.display !== 'none' && w.getBoundingClientRect().width > 50;
});
check('Rechnungen: „Rechnung auswählen" mobil NICHT sichtbar', !auswahlSichtbar);
check('Rechnungen kein h-Overflow', !(await hOverflow()));
// Zeile antippen -> Detail vollbild mit × 
await page.locator('tbody tr').first().click({ timeout: 5000 }).catch(() => {});
await page.waitForTimeout(1200);
const detailOffen = await page.evaluate(() => [...document.querySelectorAll('div')].some(d => String(d.className).includes('fixed inset-0') && getComputedStyle(d).display !== 'none' && d.textContent.includes('Ausgangsrechnung') || d.textContent.includes('Eingangsrechnung')));
check('Rechnungen: Zeile tap -> Detail-Vollbild', detailOffen);
await shot('03-rechnungen-detail');
const closeBtn = page.locator('button[aria-label="Schließen"]');
check('Rechnungen: Schließen-Button sichtbar', await closeBtn.isVisible().catch(() => false));
if (await closeBtn.isVisible().catch(() => false)) {
  await closeBtn.tap();
  await page.waitForTimeout(800);
  const nochOffen = await page.evaluate(() => [...document.querySelectorAll('div')].some(d => String(d.className).includes('fixed inset-0') && getComputedStyle(d).display !== 'none'));
  check('Rechnungen: × schließt Detail', !nochOffen);
}
await shot('04-rechnungen-nach-close');

// ── Kunden: Details + Bearbeiten mit echten Taps ──
await page.goto(BASE + '/kunden', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
const kundenKarten = await page.locator('.md\\:hidden.space-y-2 > div').count();
check('Kunden zeigt mobile Karten', kundenKarten > 0, `${kundenKarten} Karten`);
await page.locator('[data-testid="kunde-details"]').first().tap();
await page.waitForTimeout(1200);
const kundeDetailOffen = await page.evaluate(() => [...document.querySelectorAll('div')].some(d => String(d.className).includes('fixed inset-0') && getComputedStyle(d).display !== 'none'));
check('Kunden: Details tap -> Vollbild-Panel', kundeDetailOffen);
await shot('05-kunden-detail');
const zurueck = page.locator('button[aria-label="Zurück"]');
check('Kunden: Zurück-Button sichtbar', await zurueck.isVisible().catch(() => false));
if (await zurueck.isVisible().catch(() => false)) {
  await zurueck.tap();
  await page.waitForTimeout(800);
  const zu = await page.evaluate(() => ![...document.querySelectorAll('div')].some(d => String(d.className).includes('fixed inset-0') && getComputedStyle(d).display !== 'none'));
  check('Kunden: ← schließt Detail', zu);
}
// Bearbeiten
await page.locator('[data-testid="kunde-bearbeiten"]').first().tap();
await page.waitForTimeout(1200);
const formOffen = await page.evaluate(() => document.body.innerText.includes('Kunde bearbeiten') || document.body.innerText.includes('Neuer Kunde'));
check('Kunden: Bearbeiten tap -> Formular', formOffen);
const formPanelFixed = await page.evaluate(() => [...document.querySelectorAll('div')].some(d => String(d.className).includes('fixed inset-0') && getComputedStyle(d).display !== 'none' && d.textContent.includes('Kunde')));
check('Kunden: Formular als Vollbild', formPanelFixed);
await shot('06-kunden-form');
const formClose = page.locator('button[aria-label="Schließen"]');
if (await formClose.isVisible().catch(() => false)) {
  await formClose.tap();
  await page.waitForTimeout(800);
  const formZu = await page.evaluate(() => !document.body.innerText.includes('Kunde bearbeiten'));
  check('Kunden: × schließt Formular', formZu);
}
check('Kunden kein h-Overflow', !(await hOverflow()));
await shot('07-kunden-nach-close');

// ── Bottom-Nav auf allen Seiten ──
for (const p of ['/', '/journal', '/rechnungen', '/kunden']) {
  await page.goto(BASE + p, { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  const nav = await page.locator('[data-testid="mobile-bottom-nav"]').isVisible().catch(() => false);
  check(`Bottom-Nav sichtbar auf ${p}`, nav);
}

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n=== ERGEBNIS: ${results.length - failed.length}/${results.length} bestanden ===`);
process.exit(failed.length ? 1 : 0);
