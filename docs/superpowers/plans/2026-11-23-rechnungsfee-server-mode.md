# RechnungsFee Server-Modus — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RechnungsFee als Docker-Server-App (ein Container, same-origin SPA+API, OIDC/PocketID, PWA) lokal auf `feat/server-mode` lauffähig entwickeln und testen — inkl. Dockerfile, CI-Workflow-Datei und internem Security-Audit — ohne Push/Deploy.

**Architecture:** Env-Schalter `RECHNUNGSFEE_SERVER_MODE=1` aktiviert in FastAPI das Ausliefern der gebauten SPA + einen Auth-Layer (OIDC Authorization Code + PKCE gegen PocketID, serverseitige SQLite-Sessions, Session-Cookie HttpOnly/Secure/SameSite=Lax). `RECHNUNGSFEE_BASE_DIR` lenkt alle Datenpfade auf das Docker-Volume `/data`. Desktop-Modus bleibt unverändert.

**Tech Stack:** Backend FastAPI + SQLAlchemy + authlib (OIDC) + slowapi (Rate-Limit), Python 3.12 (Docker-Image `python:3.12-slim`; lokal 3.14 zum Testen OK). Frontend React 19 + Vite + vite-plugin-pwa + Tailwind v4, bestehende Vitest-Suite. CI: GitHub Actions (Datei wird geschrieben, Push macht Paddy wegen workflow-Scope). Docker: multistage (node:22 → python:3.12-slim), non-root.

**Spec:** `docs/superpowers/specs/2026-11-23-rechnungsfee-server-mode-design.md`

## Global Constraints

- **Nie pushen ohne ausdrückliche Nachfrage von Paddy.** Commits auf Deutsch, Conventional Commits, Imperativ, ~50 Zeichen; Autor `Simperdy <5592535+Simperdy@users.noreply.github.com>`, Trailer `Co-authored-by: Ron Weasley <319951031+the-ron-weasley@users.noreply.github.com>`.
- UI-Texte in Du-Ansprache; Design-Treue an `docs/14-design-language.md` (Modals §7 etc.).
- Version kommt aus Git-Tag; `package.json`-Version nie anfassen.
- Desktop-Modus (Tauri) darf durch keine Änderung kaputtgehen: alle Server-Modus-Änderungen hinter `RECHNUNGSFEE_SERVER_MODE`.
- GoBD-Buchungslogik (Unveränderlichkeit, SHA-256, Storno) nicht anfassen — nur Transport/Auth.
- Secrets nie ins Repo; `.env.example` ohne Werte.
- Verifikation pro Task: Backend `pytest src/backend/tests`, Frontend `npm test` + `npx tsc -b --pretty false` + `node ./node_modules/vite/bin/vite.js build` aus `src/frontend`, dann `git diff --check`.
- Lokales Python ist 3.14; Docker-Ziel ist 3.12 — keine 3.13+/3.14-only Features im Backend-Code.

---

### Task 1: Backend — Server-Modus-Grundgerüst (BASE_DIR-Override, Static SPA, Health)

**Files:**
- Modify: `src/backend/database/connection.py` (Env-Override `RECHNUNGSFEE_BASE_DIR` vor `_resolve_app_data_dir`)
- Modify: `src/backend/main.py` (Server-Modus-Block: StaticFiles + SPA-Fallback, `/health` bleibt public, CORS nur im Nicht-Server-Modus)
- Create: `src/backend/server_mode.py` (Hilfsmodul: `SERVER_MODE` Flag, `SPA_DIR`-Ermittlung, `mount_spa(app)` mit SPA-Fallback, Security-Header-Middleware-Hook — Platzhalter für Task 3)
- Test: `src/backend/tests/test_server_mode.py`

**Interfaces:**
- Produces: `server_mode.SERVER_MODE: bool`, `server_mode.mount_spa(app: FastAPI) -> None` — consumed by Task 2 (Auth-Middleware hängt nach SPA-Mount, schützt nur `/api/*`).

- [ ] **Step 1: Failing test schreiben** — `test_server_mode.py`: mit `RECHNUNGSFEE_SERVER_MODE=1` und gesetztem `RECHNUNGSFEE_BASE_DIR` zeigt `connection.BASE_DIR` auf den Override; ohne Env bleibt das Plattform-Verhalten. Test nutzt `monkeypatch` + `importlib.reload` — Vorsicht: Modul-Level-Konstanten; conftest stellt sicher, dass andere Tests nicht reload-verschmutzt werden (Reload in Fixture mit anschließendem Restore).
- [ ] **Step 2: Test laufen lassen, FAIL sehen** (`AttributeError`/falscher Pfad).
- [ ] **Step 3: Implementierung** — in `connection.py` ganz oben: `BASE_DIR = Path(os.environ["RECHNUNGSFEE_BASE_DIR"]) if os.environ.get("RECHNUNGSFEE_BASE_DIR") else _resolve_app_data_dir(...)`; in `server_mode.py`: `SERVER_MODE = os.environ.get("RECHNUNGSFEE_SERVER_MODE") == "1"`, `SPA_DIR = Path(__file__).parent / "static"` (vom Dockerfile dorthin kopiert), `mount_spa(app)` montiert `StaticFiles(directory=SPA_DIR, html=True)` als Fallback-Route mit niedrigster Priorität (Exception-Handler für 404 → `index.html`, außer Pfad startet mit `/api/`).
- [ ] **Step 4: `main.py` verdrahten** — `if SERVER_MODE: app.middleware... CORS nur else; mount_spa(app) nach allen Routern; `/health` bleibt wie er ist (kein Auth). Desktop-Pfad byte-identisch zum heutigen Verhalten (kein SPA-Mount ohne Env).
- [ ] **Step 5: Tests grün** — `pytest src/backend/tests/test_server_mode.py -v` + volle Backend-Suite grün (kein Regression durch Reload-Fixture).
- [ ] **Step 6: Commit** — `git commit -m "feat: Server-Modus-Grundgeruest mit BASE_DIR-Override und SPA-Auslieferung"` (+ Co-author-Trailer).

### Task 2: Backend — OIDC-Auth (PocketID) + Sessions + Middleware

**Files:**
- Modify: `src/backend/requirements.txt` (neue Pins: `authlib`, `httpx`, `slowapi` — Versionen recherchieren und pinnen)
- Create: `src/backend/api/auth.py` (Router `/api/auth`: login/callback/logout/status; authlib-OIDC-Client aus Env; `state`+PKCE in kurzlebiger signierter Ablage; Session-Create/Delete)
- Create: `src/backend/auth_sessions.py` (SQLAlchemy-Modell `Session` in `database/models.py` ODER eigene Tabelle in `auth_sessions.py` — Entscheidung: eigene Datei, damit `models.py` (GoBD-kritisch) unberührt bleibt; Tabelle `auth_sessions(id, sid_hash, email, created_at, expires_at, revoked_at)`; `create_session(db,email)->(sid,cookie_value)`, `get_session(db,sid)`, `revoke_session`)
- Modify: `src/backend/main.py` (Middleware/Dependency: alle `/api/*` außer `/api/auth/*`, `/health`, `/docs`, `/openapi.json` brauchen gültige Session; 401 JSON `{"detail":"not_authenticated"}`)
- Modify: `src/backend/database/connection.py` (neue Tabelle via `Base.metadata.create_all` — `auth_sessions.py` importiert `Base` aus connection und deklariert Modell, wird in main importiert)
- Env-Spec: `src/backend/.env.server.example` (OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_REDIRECT_URI, OIDC_ALLOWED_EMAILS, SESSION_SECRET, RECHNUNGSFEE_SERVER_MODE=1, RECHNUNGSFEE_BASE_DIR=/data)
- Test: `src/backend/tests/test_auth.py` (Stub-IdP via `httpx.MockTransport` oder lokaler Mini-ASGI IdP; Tests: login→302 zu IdP mit state+PKCE, callback mit gültigem Code legt Session an + Set-Cookie HttpOnly/Secure/SameSite=Lax, status 200, logout revokiert, abgelaufene Session → 401, nicht-allowlist Email → 403 + Log, geschützter Endpoint ohne Cookie → 401, mit Cookie → 200)

**Interfaces:**
- Produces: Cookie-Name `rf_session`, Session-TTL 8 h mit Sliding Renewal; `get_current_session` Dependency für spätere Tasks. Frontend (Task 4) erwartet 401-JSON + Pfad `/api/auth/login`.

- [ ] **Step 1: Failing tests** — `test_auth.py` gegen noch nicht existierendes `api/auth.py` (ImportError → FAIL).
- [ ] **Step 2: Minimal-Implementierung** — Env lesen (alle Pflicht im Server-Modus, sonst Startup-Error mit klarer Meldung); `authlib.integrations.httpx_client.OAuth2Client` mit `server_metadata_url=f"{OIDC_ISSUER}/.well-known/openid-configuration"`; login: `create_authorization_url` mit PKCE, `state` in signiertem Cookie (itsdangerous-free: HMAC via `cryptography`, bereits dep) oder in DB-Tabelle `auth_states`; callback: `state` prüfen, `fetch_access_token` + `parse_id_token` (Nonce prüfen), `email`-Claim gegen `OIDC_ALLOWED_EMAILS`, Session anlegen, Redirect auf `/` mit Set-Cookie.
- [ ] **Step 3: Middleware in main.py** — nur wenn `SERVER_MODE`: custom ASGI-Middleware oder FastAPI-Middleware prüft Pfad-Präfix; Session-Cookie lesen, `sid` SHA-256-hashed gegen DB (nie Klartext-SID speichern), Ablauf checken, Sliding Renewal (expires_at = now+8h bei Aktivität), `request.state.session` setzen; 401 JSON sonst.
- [ ] **Step 4: Tests grün** — Stub-IdP: Discovery-Dokument + Token-Endpoint als `httpx.MockTransport` in den authlib-Client injizieren (authlib erlaubt `fetch_token` override — im Test monkeypatchen); alle 401/200/Cookie-Flag-Assertions.
- [ ] **Step 5: Volle Suite** — `pytest src/backend/tests -v` grün (alte Tests laufen im Nicht-Server-Modus, Middleware inaktiv).
- [ ] **Step 6: Commit** — `feat: OIDC-Auth gegen PocketID mit serverseitigen Sessions im Server-Modus`.

### Task 3: Backend — Security-Härtung (Header, Rate-Limit, Secrets)

**Files:**
- Modify: `src/backend/server_mode.py` (Security-Header-Middleware: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Strict-Transport-Security` nur hinter TLS — im Container HTTP, Header setzt NPM? Entscheidung: HSTS setzt NPM am Proxy, Backend setzt CSP + restliche Header; CSP: `default-src 'self'`, keine unsafe-inline — Frontend inline-Script in index.html (Theme) muss auf externes File oder Nonce — Task 4 verschiebt es), `X-Frame-Options: DENY`)
- Modify: `src/backend/api/auth.py` (slowapi-Limiter auf login/callback, z. B. 10/min/IP)
- Modify: `src/backend/main.py` (CSRF: Origin/Referer-Check auf POST/PUT/DELETE/PATCH wenn `Origin`-Header vorhanden und nicht same-origin → 403)
- Test: `src/backend/tests/test_security_headers.py` (Header bei SPA-Antwort + API-Antwort, Rate-Limit greift nach N Requests → 429, Cross-Origin-POST mit fremdem Origin → 403, same-origin POST ok)

- [ ] **Step 1-4:** analog Task 1/2 (failing test → Implementierung → grün).
- [ ] **Step 5:** `pip-audit` gegen requirements (nur Server-relevante Findings; bestehende Findings dokumentieren, nicht ungefragt fixen).
- [ ] **Step 6:** Commit `feat: Security-Header, Rate-Limit und CSRF-Check im Server-Modus`.

### Task 4: Frontend — 401-Redirect + PWA-Grundgerüst

**Files:**
- Modify: `src/frontend/src/api/client.ts` (`request()`: bei `res.status === 401` und nicht-Tauri → `window.location.assign('/api/auth/login')`; im Tauri-Modus unverändert werfen)
- Modify: `src/frontend/package.json` + `npm install -D vite-plugin-pwa`
- Modify: `src/frontend/vite.config.ts` (`VitePWA`-Plugin: `registerType: 'autoUpdate'`, `manifest: { name: 'RechnungsFee', short_name: 'RechnungsFee', display: 'standalone', theme_color, background_color, icons: [192, 512] }`, `workbox: { globPatterns: ['**/*.{html,js,css,png,svg,woff2}'], maximumFileSizeToCacheInBytes: 5MB, navigateFallback: '/index.html', runtimeCaching: [{ urlPattern: /\/api\//, handler: 'NetworkOnly' }] }` — API nie cachen)
- Create: `src/frontend/public/icon-192.png`, `icon-512.png` (bestehendes Logo als Basis — keine Pillow-Grafik; vorhandene PNG/SVG-Assets konvertieren via vorhandenes Tool oder resvg), `public/manifest.webmanifest` wird vom Plugin generiert
- Modify: `src/frontend/index.html` (Apple-Meta: `apple-mobile-web-app-capable`, `apple-mobile-web-app-status-bar-style`, `apple-touch-icon`; Theme-inline-Script in externes `/theme-init.js` auslagern wegen CSP)
- Create: `src/frontend/public/theme-init.js`
- Test: `src/frontend/src/api/client.test.ts` (401 → location.assign '/api/auth/login' mit jsdom, Tauri-Mock bleibt Error)

- [ ] **Step 1:** failing Vitest für 401-Redirect (jsdom `window.location` via `delete window.location`-Trick oder `vi.stubGlobal`).
- [ ] **Step 2:** Implementierung client.ts + PWA-Plugin + Icons + index.html.
- [ ] **Step 3:** `npm test`, `npx tsc -b --pretty false`, `node ./node_modules/vite/bin/vite.js build` grün; Build-Output prüfen: `dist/manifest.webmanifest` + `dist/sw.js` vorhanden.
- [ ] **Step 4:** `git diff --check`; Commit `feat: PWA-Grundgeruest und 401-Login-Redirect im Web-Modus`.

### Task 5: Frontend — Mobile-First Stufe 1 (Kern-Seiten)

**Files:**
- Modify: `src/frontend/src/components/AppLayout.tsx` (Bottom-Navigation unter 768 px mit den Kern-Routen: Dashboard, Kassenbuch, Rechnungen, Kunden; bestehende Sidebar ab `md:` — Struktur lesen und minimal invasiv erweitern)
- Modify: Kern-Seiten für Touch: `src/frontend/src/pages/journal/JournalPage.tsx`, `pages/rechnungen/RechnungenPage.tsx`, `pages/kunden/KundenPage.tsx`, `pages/lieferanten/LieferantenPage.tsx`, `pages/dashboard/Dashboard.tsx` (Touch-Ziele ≥ 44 px, Formularfelder volle Breite, Listen-Cards statt enger Tabellen — Tailwind-Klassen an `docs/14-design-language.md` anlehnen)
- Test: Vitest-Snapshot/Render-Tests für Bottom-Nav (Viewport via jsdom nicht real — stattdessen CSS-Klassen-Assertions + manueller Browser-Check in Task 9)

- [ ] **Step 1:** AppLayout lesen,最小-Invasive Änderung planen (bestehende Nav-Struktur сохранить).
- [ ] **Step 2:** Bottom-Nav implementieren (sichtbar < 768 px via `md:hidden`), Kern-Seiten responsive machen.
- [ ] **Step 3:** `npm test` + `tsc -b` + `vite build` grün.
- [ ] **Step 4:** Commit `feat: Mobile-first Bottom-Navigation und Touch-Layout fuer Kern-Seiten`.

### Task 6: Dockerfile + .dockerignore + lokaler Build

**Files:**
- Create: `Dockerfile` (Repo-Root; Stage 1 `node:22-alpine` → `WORKDIR /app/src/frontend`, `npm ci`, `npm run build`; Stage 2 `python:3.12-slim` → `apt-get` nur notwendige Runtime-Pakete für OCR/PDF (tesseract? prüfen was pytesseract/ocrmypdf brauchen — Image-Doku von Upstream prüfen, minimal halten; libfuse nicht nötig), `pip install --no-cache-dir -r requirements.txt`, copy backend `src/backend`, copy `dist/` → `/app/src/backend/static`, `useradd -m rechnungsfee`, `USER rechnungsfee`, `ENV RECHNUNGSFEE_BASE_DIR=/data RECHNUNGSFEE_SERVER_MODE=1 PYTHONUNBUFFERED=1`, `VOLUME /data`, `EXPOSE 8000`, `HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"`, `CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]` mit `WORKDIR /app/src/backend`)
- Create: `.dockerignore` (node_modules, .venv, __pycache__, .git, uploads, *.db, dist lokal etc.)
- Modify: `src/backend/requirements.txt` nur ergänzen (authlib, httpx, slowapi — aus Task 2); Runtime-Extra `requirements-docker.txt` falls apt-Pakete nötig dokumentiert im Dockerfile-Kommentar.

- [ ] **Step 1:** `docker build -t rechnungsfee-server:dev .` lokal (Docker 29.1.3 vorhanden) — Build grün, Image-Größe notieren.
- [ ] **Step 2:** Container lokal starten mit Stub-Env (Dummy OIDC-Werte, SESSION_SECRET, ALLOWED_EMAILS=test) + Volume in Scratch: `docker run -d --name rf-test -p 127.0.0.1:8000:8000 -v $TMPDIR/rf-data:/data -e ... rechnungsfee-server:dev`; `curl localhost:8000/health` → ok; `curl localhost:8000/` → index.html; `curl localhost:8000/api/setup/status` → 401 JSON; `curl localhost:8000/api/auth/status` → 200.
- [ ] **Step 3:** Commit `feat: Multistage-Dockerfile fuer Server-Modus (non-root, /data Volume)`.

### Task 7: CI-Workflow (Datei schreiben; Push macht Paddy)

**Files:**
- Create: `.github/workflows/docker-release.yml` — Trigger `push: tags: ['v*']`; Jobs: (1) `test-backend`: python 3.12, `pip install -r requirements.txt -r requirements-dev.txt`, `pytest src/backend/tests`; (2) `test-frontend`: node 22, `npm ci` in src/frontend, `npm test`, `tsc -b`, `vite build`; (3) `audit`: `pip-audit`, `npm audit --audit-level=high` (fail on high); (4) `docker`: needs tests+audit, buildx multistage, push `ghcr.io/nicolettas-muggelbude/rechnungsfee-server:<tag>` + `:latest`, mit `permissions: packages: write`, login via `GITHUB_TOKEN`. Kein PR/Commit durch Agent — Datei nur im Branch.
- Doku: Kommentar im Workflow + Spec-Verweis.

- [ ] **Step 1:** Workflow-Datei schreiben, YAML validieren (`python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/docker-release.yml'))"`).
- [ ] **Step 2:** Commit `ci: Docker-Build + Tests bei Release-Tag nach GHCR` — **kein Push** (workflow-Scope des gh-Tokens, Paddy pusht).

### Task 8: Internes Security-Audit + Fixes

**Files:** Review-Report `docs/superpowers/specs/2026-11-23-server-mode-audit.md` + Fixes inline.

- [ ] **Step 1:** Subagent (Qwen3.8 Flash Next, sequenziell — KI-PC trägt nur eine Session) mit komplettem Diff `git diff main...HEAD` + Checkliste: OWASP Top 10 (AuthZ pro Endpoint, IDOR, Injection, offener Redirect, SSRF bei OIDC-Discovery, Secrets in Logs), Cookie-Flags, Session-Fixation (SID-Rotation nach Login), Rate-Limits, CSP-Verträglichkeit mit Frontend, GoBD: keine Buchungslogik geändert (`git diff` auf journal/tagesabschluss/rechnungen Buchungs-Pfade leer), Backup-Restore-Pfad unverändert.
- [ ] **Step 2:** Findings als Blocker/Optional klassifizieren, Blocker fixen, Tests nachziehen.
- [ ] **Step 3:** `pip-audit` + `npm audit` Befunde dokumentieren.
- [ ] **Step 4:** Commit `fix: Audit-Findings Server-Modus` + Report committen.

### Task 9: Lokaler End-to-End-Test (das „Vorzeigbare")

- [ ] **Step 1:** Stub-IdP lokal (kleiner FastAPI/uvicorn auf Port 9999 mit Discovery + authorize + token + jwks; RSA-Key via cryptography; ein Test-User `caro@example.org`).
- [ ] **Step 2:** Container mit echten Env-Werten gegen Stub-IdP starten; Browser (browser_exec) durch: `/` → 302 `/api/auth/login` → Stub-IdP → callback → Session-Cookie → Dashboard lädt; Setup-Wizard durchklicken (Firma anlegen); Kassenbuch-Buchung anlegen; Rechnung anlegen + PDF; Backup-ZIP (Dummy) hochladen + Restore-Flow; Logout → wieder 401.
- [ ] **Step 3:** Mobile-Viewport (iPhone-Größe) durchklicken: Bottom-Nav sichtbar, Kern-Seiten bedienbar — Screenshots für Paddy.
- [ ] **Step 4:** PWA-Check: manifest + sw.js ausgeliefert, `display: standalone` im Manifest.
- [ ] **Step 5:** Report an Paddy mit Screenshots + Befehlen; Commit `test: End-to-End Server-Modus mit Stub-IdP (Doku)` (Testskripte unter `scripts/server-mode-e2e/` commiten).

### Task 10: Homelab-Stack vorbereiten (Dateien lokal im Monorepo, Push später)

**Files:** (im Clone von Paddy/docker, eigener Branch — Push-Regel gilt)
- Create: `stacks/rechnungsfee-caro/compose.yaml` (Service `rechnungsfee-caro`, Image `ghcr.io/.../rechnungsfee-server:latest` — bis GHCR steht: lokaler Build-Tag, dokumentieren; Env aus Dockhand-Variablen: OIDC_*, SESSION_SECRET, OIDC_ALLOWED_EMAILS; Volume `rechnungsfee_caro_data:/data`; Netz `proxy_sensitive` extern, **keine Ports**; healthcheck), `.env.example` (ohne Werte), `README.md` (Deploy via Dockhand, NPM-Host anlegen, Phase-1-ACL LAN/VPN only)
- Modify: `tests/verify-monorepo-layout.rb` STACKS-Liste + Policy-Test-Tupel ergänzen (analog bestehender Stacks)
- [ ] **Step 1:** Stack anlegen, `ruby tests/verify-*.rb` lokal grün (Ruby auf Mini-PC), Mutationsprobe für neue Assertions.
- [ ] **Step 2:** Commit auf neuem Branch im docker-Repo — **kein Push** ohne Paddy.

---

## Self-Review (gegen Spec)

1. **Spec-Coverage:** §2 Architektur → Tasks 1, 6; §3 Homelab → Task 10; §4 OIDC → Tasks 2, 3; §5 Server-Modus/Bestand → Tasks 1, 2, 9; §6 PWA/Mobile → Tasks 4, 5; §7 Tests/CI/Audit → Tasks 2-3 (pytest), 7 (CI), 8 (Audit); §8 Entscheidungen → überall verankert. Lücke: keine.
2. **Placeholder-Scan:** alle Steps haben konkrete Befehle/Dateien; keine TBDs.
3. **Type-Konsistenz:** `server_mode.SERVER_MODE`/`mount_spa` (T1) werden von T2/T3 konsumiert; Cookie `rf_session` (T2) von T4 erwartet; `SPA_DIR = src/backend/static` (T6 Dockerfile) konsistent mit T1.
