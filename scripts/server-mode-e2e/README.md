# Server-Modus: E2E-Test lokal (Stub-IdP + nginx TLS + Docker)

Lokales Test-Setup für den Docker-Server-Modus mit OIDC-Login, **bevor** NPM/Let's Encrypt dranhängt.

## 1. Stub-IdP starten (Port 9999)

```bash
src/backend/.venv/bin/python scripts/server-mode-e2e/stub_idp.py &
# issuer: http://<host-ip>:9999  (Discovery: /.well-known/openid-configuration)
# PKCE S256 Pflicht; auto-approve; erlaubte E-Mail via OIDC_ALLOWED_EMAILS
```

## 2. Selbstsigniertes Zert + nginx auf 8443

```bash
mkdir -p ~/.hermes/cache/scratch/rf-lan-test
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout ~/.hermes/cache/scratch/rf-lan-test/key.pem \
  -out    ~/.hermes/cache/scratch/rf-lan-test/cert.pem \
  -subj "/CN=192.168.66.203" \
  -addext "subjectAltName=IP:192.168.66.203,DNS:rf-lan.local"
```

nginx-Site `rf-lan-test.conf` (sites-available, symlink nach sites-enabled):

```nginx
server {
    listen 8443 ssl;
    server_name 192.168.66.203 rf-lan.local;

    ssl_certificate     /home/kuncke/.hermes/cache/scratch/rf-lan-test/cert.pem;
    ssl_certificate_key /home/kuncke/.hermes/cache/scratch/rf-lan-test/key.pem;

    # WICHTIG für Backup-Upload (.zip.enc kann mehrere MB haben):
    # nginx-Default ist 1m -> 413 Request Entity Too Large
    client_max_body_size 512m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        # $host (ohne Port) ist OK: der CSRF-Check im Backend vergleicht den
        # Port nur, wenn BEIDE Seiten einen expliziten Port mitsenden.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

`nginx -t && systemctl reload nginx`

## 3. Container starten

```bash
docker build -t rechnungsfee-server:dev .
mkdir -p /tmp/rf-data && chmod 777 /tmp/rf-data
docker run -d --name rf-test --network host \
  -e RECHNUNGSFEE_SERVER_MODE=1 \
  -e RECHNUNGSFEE_BASE_DIR=/data \
  -e SESSION_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  -e OIDC_ISSUER="http://192.168.66.203:9999" \
  -e OIDC_CLIENT_ID="rechnungsfee-test" \
  -e OIDC_REDIRECT_URI="https://192.168.66.203:8443/api/auth/callback" \
  -e OIDC_ALLOWED_EMAILS="caro@example.org" \
  -v /tmp/rf-data:/data \
  rechnungsfee-server:dev
```

## 4. Test-Flow

1. Browser: `https://192.168.66.203:8443` → Zert-Warnung akzeptieren → OIDC-Login (Stub auto-approve)
2. Backup-Seite `/backup` → `.zip` oder `.zip.enc` + Passwort → „Backup hochladen und vorbereiten"
3. `docker restart rf-test` → Restore läuft beim Start (`docker logs rf-test | grep Wiederherstellung`)
4. Prüfen: `/api/journal` bzw. UI zeigt die wiederhergestellten Daten

## Bekannte Stolperfallen (alles gefixt in feat/server-mode)

| Symptom | Ursache | Fix |
|---|---|---|
| „Wiederherstellung fehlgeschlagen" bei .zip.enc über :8443 | CSRF-Check: Origin mit Port vs. nginx `Host $host` ohne Port | Port-Abgleich nur wenn beide Seiten Port mitsenden |
| „Wiederherstellung fehlgeschlagen" bei großem Backup | nginx default `client_max_body_size 1m` → 413 HTML (kein JSON) | `client_max_body_size 512m` in der Site + Frontend zeigt HTTP-Status |
| `database disk image is malformed` nach Restore | stale `-wal`/`-shm` der alten DB | Restore checkpoints + löscht Sidecar vor Austausch |
| Crash beim frischen Erststart | `no such table: auth_sessions` vor `create_all` | idempotentes Anlegen vor Startup-Purge |

## Produktion (NPM)

NPM-Proxy-Host `reynomial…` → `http://rechnungsfee-caro:8000`, TLS via Let's Encrypt.
Im NPM-Advanced-Tab ebenfalls `client_max_body_size 512m;` setzen (sonst 413 bei großen Backups).
