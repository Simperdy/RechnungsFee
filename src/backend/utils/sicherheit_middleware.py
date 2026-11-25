"""
Sicherheits-Middleware für die lokale API (Security-Audit lindesbs, 2026-11-07).

F4 Teil 1 – Host-Prüfung gegen DNS-Rebinding: Eine Angreifer-Domain, die per DNS plötzlich
  auf 127.0.0.1 zeigt, gilt im Browser als „gleicher Ursprung“ und könnte alle Antworten
  lesen – CORS greift dann nicht. Der Host-Header verrät sie aber: nur 127.0.0.1/localhost.
F7 Teil 1 – Origin-Prüfung: Formular-, text/plain- und Multipart-POSTs fremder Webseiten
  laufen ohne CORS-Preflight bis zum Endpunkt durch (CORS verhindert nur das Lesen der
  Antwort). Schreibende Requests mit fremdem Origin werden deshalb abgewiesen. Requests ganz
  ohne Origin (eigene Fenster per GET, Skripte, Tests) bleiben erlaubt.
F2 – Datei-Antworten: „X-Content-Type-Options: nosniff“ überall; Antworten, die weder JSON,
  HTML noch PDF sind (XML, Bilder, Office-Dateien, CSV …), zusätzlich mit
  „Content-Security-Policy: sandbox“ – fremde Inhalte dürfen im API-Ursprung kein Skript
  ausführen. PDF bewusst ohne sandbox (blockiert in manchen Engines den PDF-Viewer),
  HTML setzt seine CSP selbst (Journal-Beleg) bzw. ist eigener Code (PDF-Ansichtshülle).

F4 Teil 2 – Sitzungstoken: jeder Request braucht „Authorization: Bearer <token>“
  (pro App-Start von Tauri erzeugt, siehe utils/sitzungstoken.py), sonst 401. Ausnahmen:
  CORS-Preflight (OPTIONS trägt keinen Header) und lesende Abrufe (GET/HEAD) mit gültigem
  signiertem Kurzzeit-Link (`?exp=…&sig=…`, ausgestellt von POST /api/signierte-url) – für
  Fenster, iframe und <img>, die keinen Header setzen können. Der Link bindet Pfad + Query.

Reine ASGI-Middleware (kein BaseHTTPMiddleware), damit Streaming-Antworten unverändert
durchlaufen.
"""
from urllib.parse import urlsplit

import server_mode
from utils import sitzungstoken

# Desktop-Modus: nur localhost-Hosts. Server-Modus (Docker hinter TLS-Proxy):
# der Proxy setzt den Host auf die öffentliche Domain – die erlauben wir,
# solange der Request über den Proxy kam (X-Forwarded-Proto gesetzt).
ERLAUBTE_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# Eigene Oberflächen: Tauri (macOS/Linux), Tauri unter Windows (WebView2), Vite-Dev-Server
# sowie Seiten, die die API selbst ausliefert (PDF-Ansicht) – jeweils nur auf localhost.
_ERLAUBTE_ORIGINS = {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}

_LESEND = {"GET", "HEAD", "OPTIONS"}

_OHNE_SANDBOX = ("application/json", "text/html", "application/pdf")

# F10: Größenlimit per Content-Length (Uploads werden per await datei.read() komplett in den
# Speicher gelesen). Großzügig, damit echte Belege/Backups nie scheitern.
MAX_UPLOAD = 500 * 1024 ** 2                    # 500 MB
MAX_UPLOAD_BACKUP = 10 * 1024 ** 3              # 10 GB – wie das Entpack-Limit beim Restore
_BACKUP_PFADE = ("/api/backup/wiederherstellen",)


def ohne_token_erlaubt(methode: str) -> bool:
    """CORS-Preflight trägt keinen Authorization-Header."""
    return methode == "OPTIONS"


def _hostname(host_header: str) -> str:
    """Hostname ohne Port; IPv6-Literal behält die Klammern."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host



# Vertrauensbereich für Forwarded-Header — muss zu RECHNUNGSFEE_PROXY_NETZE
# (Env, Default siehe Dockerfile) passen. Komma-separierte IPs/CIDRs.
import os as _os
_DEFAULT_PROXY_NETZE = "127.0.0.1,172.16.15.0/24,172.16.16.0/24"  # Loopback + NPM proxy_sensitive/proxy_public


def _proxy_netze_aus_env() -> tuple:
    """RECHNUNGSFEE_PROXY_NETZE (komma-separiert) oder Default — muss zu
    --forwarded-allow-ips im Dockerfile passen (dort expandiert die Shell)."""
    roh = _os.environ.get("RECHNUNGSFEE_PROXY_NETZE", _DEFAULT_PROXY_NETZE)
    return tuple(n.strip() for n in roh.split(",") if n.strip())


_PROXY_NETZE = _proxy_netze_aus_env()


def _ist_proxy_client(scope: dict) -> bool:
    """True, wenn der Request nachweislich vom eigenen Proxy kam.

    uvicorn loest mit --proxy-headers/--forwarded-allow-ips die echte Client-IP
    in scope['client'] auf. Wir vertrauen X-Forwarded-* nur, wenn diese IP im
    Proxy-Vertrauensbereich liegt — ein direkter LAN/Docker-Zugriff auf :8000
    (anderer Container in einem anderen privaten Netz) kann die Header sonst spoofen."""
    import ipaddress
    client_ip = (scope.get("client") or ("", 0))[0]
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(n) for n in _PROXY_NETZE)

def origin_erlaubt(origin: str, scope: dict | None = None) -> bool:
    origin = origin.strip().lower()
    if origin in _ERLAUBTE_ORIGINS:
        return True
    try:
        teile = urlsplit(origin)
    except ValueError:
        return False
    if teile.scheme not in ("http", "https"):
        return False
    host = teile.hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    # Server-Modus (Docker, same-origin SPA hinter TLS-Proxy): der Browser schickt
    # die öffentliche Origin (z.B. https://host:8443) – erlaubt, wenn der Request
    # selbst vom Proxy kam (X-Forwarded-Proto), dann ist same-origin garantiert.
    # Server-Modus: fremde Origin nur erlauben, wenn der Request nachweislich
    # vom eigenen Proxy kam — X-Forwarded-Proto allein ist spoofbar, deshalb
    # zusaetzlich die echte Client-IP aus scope['client'] gegen das private
    # Proxy-Netz pruefen (siehe _ist_proxy_client).
    if server_mode.SERVER_MODE and scope is not None:
        headers = scope.get("headers") or {}
        if any(k.lower() == b"x-forwarded-proto" for k, _ in headers) and _ist_proxy_client(scope):
            return True
    return False


async def _abweisen(send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"x-content-type-options", b"nosniff"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class SicherheitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        host_ok = _hostname(headers.get("host", "")) in ERLAUBTE_HOSTS
        # Server-Modus hinter TLS-Proxy: Host ist die oeffentliche Domain. Vertraut
        # wird X-Forwarded-Proto nur, wenn der Aufrufer selbst aus dem Proxy-Netz
        # kommt (uvicorn loest mit --proxy-headers/--forwarded-allow-ips die echte
        # Client-IP in scope['client'] auf) — ein direkter LAN-Zugriff auf :8000
        # kann den Header sonst spoofen und den Host-Check aushebeln.
        if not host_ok:
            _proxy = _ist_proxy_client(scope)
            if not (server_mode.SERVER_MODE and _proxy and headers.get("x-forwarded-proto")):
                await _abweisen(send, 421, "Unzulässiger Host-Header.")
                return

        origin = headers.get("origin")
        if scope.get("method", "GET") not in _LESEND and origin is not None and not origin_erlaubt(origin, scope):
            await _abweisen(send, 403, "Anfrage von fremder Herkunft abgelehnt.")
            return

        if (
            sitzungstoken.pruefung_aktiv()
            and not ohne_token_erlaubt(scope.get("method", "GET"))
            and not sitzungstoken.authorization_gueltig(headers.get("authorization"))
            and not (
                scope.get("method", "GET") in ("GET", "HEAD")
                and sitzungstoken.link_gueltig(scope.get("path", ""), scope.get("query_string", b"").decode("latin-1"))
            )
        ):
            await _abweisen(send, 401, "Sitzungstoken fehlt oder ist ungültig.")
            return

        laenge = headers.get("content-length", "")
        if laenge.isdigit():
            grenze = MAX_UPLOAD_BACKUP if scope.get("path", "").startswith(_BACKUP_PFADE) else MAX_UPLOAD
            if int(laenge) > grenze:
                await _abweisen(send, 413, "Datei zu groß.")
                return

        async def send_mit_headern(message):
            if message["type"] == "http.response.start":
                vorhanden = {k.lower() for k, _ in message.get("headers", [])}
                neu = list(message.get("headers", []))
                if b"x-content-type-options" not in vorhanden:
                    neu.append((b"x-content-type-options", b"nosniff"))
                content_type = next(
                    (v.decode("latin-1").lower() for k, v in neu if k.lower() == b"content-type"), ""
                )
                if (
                    content_type
                    and not content_type.startswith(_OHNE_SANDBOX)
                    and b"content-security-policy" not in vorhanden
                ):
                    neu.append((b"content-security-policy", b"sandbox"))
                message = dict(message, headers=neu)
            await send(message)

        await self.app(scope, receive, send_mit_headern)
