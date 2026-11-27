# RechnungsFee Server-Modus — Design-Spec

**Datum:** 2026-11-23
**Branch:** `feat/server-mode` (lokal, kein Push ohne ausdrückliche Freigabe durch Paddy)
**Bezug:** Roadmap „Docker-Version" + Issue #117; Mandantenmodell „1 Instanz = 1 Mandant"
**Genehmigt durch:** Paddy (Design-Review im Chat, 2026-11-23)

## 1. Ziel & Nicht-Ziele

Ziel: RechnungsFee als leichtgewichtige Docker-Server-App auf dem NAS (ratrunas) betreiben,
zugänglich per Browser (MacBook) und iPhone (PWA), geschützt durch OIDC (PocketID).
Stufe 1 ist mobil-fokussiert, Stufe 2 volle Desktop-Parität. Caros Bestandsdaten werden
per bestehendem Backup-Restore übernommen.

Nicht-Ziele (Stufe 1): Multi-Mandant auf einer Instanz, native iOS-App, öffentliche
Erreichbarkeit ohne VPN/WLAN (kommt später), Änderungen an der GoBD-Buchungslogik.

## 2. Architektur (genehmigter Ansatz 1: ein Container, same-origin)

- **Ein Docker-Image**, zweistufiger Build: Stage 1 `node:22` baut das React-Frontend
  (`npm ci && npm run build` in `src/frontend`), Stage 2 `python:3.12-slim` installiert
  das Backend (`pip install -r requirements.txt`) und kopiert `dist/` als statische Assets.
- FastAPI liefert die SPA aus (`StaticFiles` + SPA-Fallback auf `index.html`);
  `/api/*` bleibt die bestehende REST-API. Kein zweiter Container, kein CORS im
  Server-Modus, Session-Cookie same-origin.
- **Daten:** ein Volume auf `/data`. Env `RECHNUNGSFEE_BASE_DIR=/data` überschreibt
  in `database/connection.py` den Plattform-Datenpfad (einziger Eingriff dort);
  die bestehende Profil-Logik bleibt — Mandant = `/data/profile/Standard`.
- Läuft als **non-root** (neuer User im Image), Port 8000 intern, **kein publizierter
  Host-Port** im Homelab: Zugriff nur über NPM-Proxy-Host.

## 3. Deployment im Homelab (Paddy/docker Monorepo)

- Neuer Stack `stacks/rechnungsfee-caro/` (compose.yaml, .env.example, README),
  deployed via Dockhand auf ratrunas, Netz `proxy_sensitive`.
- NPM-Proxy-Host `rechnungsfee.pidpad.org` → `http://rechnungsfee-caro:8000`,
  TLS per Let's Encrypt DNS-01 (Cloudflare), wie bestehende Hosts.
- **Phase 1:** NPM-Zugriffsliste nur LAN + VPN — nicht öffentlich schalten.
- Secrets (SESSION_SECRET, OIDC-Client-Daten) nur als Dockhand-Env-Variablen;
  `.env.example` ohne Werte. Compose-Policy-Tests des Monorepos mitpflegen.

## 4. Auth & OIDC (PocketID)

- **Eigener PocketID-Client pro Instanz**, Authorization Code + PKCE (S256),
  Redirect-URI `https://<host>/api/auth/callback` (exakt, kein Slash-Toleranz).
- Neue Backend-Router `api/auth.py`: `GET /api/auth/login` (302 → IdP),
  `GET /api/auth/callback`, `GET /api/auth/logout`, `GET /api/auth/status`.
  Library: authlib (OIDC-Client), PKCE serverseitig verifiziert.
- **Serverseitige Sessions** in SQLite (neue Tabelle `sessions`): Cookie trägt nur
  die Session-ID, Flags `HttpOnly`, `Secure`, `SameSite=Lax` (OIDC-Redirect ist
  cross-site top-level GET), 8 h Ablauf mit Sliding Renewal, serverseitig revokierbar.
- **Allowlist:** Env `OIDC_ALLOWED_EMAILS` (Komma-Liste) — nur freigeschaltete
  Mails (Caro) dürfen rein; andere Logins werden abgelehnt + geloggt (silent reject).
- **CSRF:** SameSite=Lax + Origin/Referer-Check auf allen mutierenden Requests.
- **Rate-Limit** auf `/api/auth/*` (slowapi), Security-Header (CSP ohne unsafe-inline
  wo möglich, HSTS, X-Content-Type-Options, Referrer-Policy, Permissions-Policy).
- Im Desktop-Modus (kein SERVER_MODE) bleibt alles wie heute — kein Auth-Pflicht.

## 5. Server-Modus im Backend

- Env `RECHNUNGSFEE_SERVER_MODE=1` in `main.py`:
  - Static SPA ausliefern + SPA-Fallback (alle nicht-`/api`-GETs → index.html).
  - Auth-Middleware schützt **alle** `/api/*` außer `/api/auth/*` und `/health`.
    Ungültige/fehlende Session → 401 JSON; das Frontend leitet auf `/api/auth/login` um.
  - CORS-Middleware im Server-Modus deaktiviert (same-origin).
  - Setup-Flow bleibt erreichbar **nach** Login (Allowlist-User legt Firma an).
- **Bestandsdaten:** Caros Desktop-Backup-ZIP wird nach Login über den bestehenden
  Backup-Restore-Endpunkt hochgeladen; `restore_pending.zip`-Mechanismus greift
  unverändert, da alle Pfade über `APP_DATA_DIR` (= `/data/...`) laufen.
- `GET /health` bleibt ohne Auth (Docker/NPM-Healthcheck).

## 6. Frontend & PWA

- API-Client nutzt im Browser bereits relativ `/api` — im Server-Modus keine Änderung
  nötig. Ausnahme: 401-Antwort in `request()` → `window.location = '/api/auth/login'`.
- **PWA:** `vite-plugin-pwa`, `manifest.webmanifest` (name, Icons 192/512,
  `display: standalone`, `theme_color`), `apple-touch-icon`, Apple-Meta-Tags in
  `index.html`. Service Worker cached **nur die App-Shell** (HTML/JS/CSS/Icons),
  niemals API-Responses oder hochgeladene Belege (Datenschutz/GoBD).
- **Mobil-first Stufe 1** auf: Kassenbuch (Journal: Erfassen/Suchen), Rechnungen
  (Liste/Detail/PDF/neu), Kunden/Lieferanten (Liste/Detail), Dashboard/Cockpit.
  Bottom-Navigation < 768 px, Touch-Ziele ≥ 44 px, große Formularfelder,
  bestehende Design-Sprache (`docs/14-design-language.md`) beibehalten.
  Rest der SPA bleibt funktional, mobil nicht optimiert (Stufe 2).

## 7. Tests, CI & Audit

- **Backend:** pytest (bestehende Test-Infrastruktur in `src/backend/tests`, conftest
  mit eigener DB) — neue Tests für: Auth-Flow gegen Stub-IdP (login/callback/logout/
  status, Allowlist-Ablehnung, Session-Ablauf/Revocation, 401 ohne Session,
  SPA-Fallback, /health ohne Auth), TDD wo praktikabel.
- **Frontend:** bestehende Vitest-Suite (`npm test`) + `tsc -b` + `vite build` müssen
  grün bleiben; neuer Test: 401 → Redirect auf Login.
- **CI (Ziel, Pushen durch Paddy wegen workflow-Scope):** `.github/workflows/`
  baut bei Release-Tag das Docker-Image (multistage), pusht nach GHCR, führt
  Backend-pytest + Frontend-Tests + `pip-audit`/`npm audit` aus. Image-Tag = Release-Tag.
- **Internes Security-Audit vor Go-Live** (Subagent-Review gegen OWASP Top 10 +
  GoBD-Checkliste): AuthZ auf jedem Endpoint, IDOR, Injection, offener Redirect
  (`state` + Redirect-URI-Whitelist), SSRF (OIDC-Discovery nur aus Env), Secrets nie
  in Logs/Repo, Cookie-Flags, Rate-Limits, CSP.
- **GoBD-Garantien unangetastet:** Unveränderlichkeit, SHA-256-Signaturen,
  Storno statt Löschung — Server-Modus ändert nur Transport/Auth, nicht Buchungslogik.

## 8. Offene Punkte / Entscheidungen (bereits geklärt)

- 1 Instanz = 1 Mandant ✔
- Stufe 1 mobil-fokussiert, Stufe 2 volle Parität ✔
- Bestandsübernahme per Backup-Restore ✔
- Eigener PocketID-Client pro Instanz ✔
- Öffentliche NPM-Route im Design, Phase 1 nur LAN/VPN-ACL ✔
- Internes Audit vor Go-Live ✔
- PWA statt nativer App in Stufe 1; Capacitor-Wrapper später ✔
- Code auf `feat/server-mode`, Push erst nach privatem Test, Upstream-PR später ✔
