"""Server-Modus: Env-Flags, SPA-Auslieferung, Auth-Guard, Security-Header, CSRF.

Aktiviert nur ueber ``RECHNUNGSFEE_SERVER_MODE=1`` (Docker-Image setzt das). Im
Desktop-Modus (Tauri) aendert dieses Modul nichts: ``mount_spa``/``initialisiere``
sind No-ops und CORS bleibt unveraendert.

Reihenfolge in ``main.py`` - ``add_middleware`` wickelt von aussen nach innen, die
zuletzt hinzugefuegte Schicht laeuft zuerst:

    ServerModeMiddleware   (aeusserste: Security-Header, CSRF, Auth-Guard)
      -> SlowAPIMiddleware (nur Limit auf /api/auth/login + /callback)
        -> Routing + SPA-Fallback
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Mapping, Receive, Scope, Send

log = logging.getLogger(__name__)

SERVER_MODE = os.environ.get("RECHNUNGSFEE_SERVER_MODE") == "1"

# Vom Dockerfile dorthin kopiert: <backend>/static = vite build dist/
SPA_DIR = Path(__file__).resolve().parent / "static"

_API_PREFIX = "/api/"

# Im Server-Modus ohne Login erreichbar (Health fuer Docker/NPM, API-Doku hinter NPM-ACL).
# /api/setup/status liefert nur Booleans (kein Personenbezug) und muss vor dem ersten
# Login erreichbar sein - sonst bootet ein frisches Volume nie bis zur Anmeldung.
PUBLIC_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})
_PUBLIC_PREFIXES = ("/api/auth/", "/api/setup/status") + tuple(PUBLIC_PATHS)

# Statische Top-Level-Dateien der SPA (index.html-Fallback deckt den Rest)
_STATIC_DATEIEN = (
    "favicon.ico", "favicon.png", "manifest.webmanifest", "sw.js", "theme-init.js",
    "registerSW.js", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "robots.txt",
    "vite.svg", "logo.svg",
)

# Security-Header fuer jede Antwort. HSTS setzt der TLS-Proxy (NPM), das Backend
# laeuft dahinter ueber HTTP. CSP ohne unsafe-inline fuer Skripte: das Inline-
# Theme-Script in index.html muss das Frontend nach /theme-init.js auslagern.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), geolocation=(), "
    "gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=(), interest-cohort=()"
)
SECURITY_HEADER = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Content-Security-Policy": CSP,
}

_CSRF_METHODEN = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Eigener Scope-Schluessel fuer die Session (FASTAPI ist ein eigener String-Namespace,
# aber besser eindeutig) - Routen lesen ueber ``session_aus_scope(scope)``.
SESSION_SCOPE_KEY = "rechnungsfee.session"


# ---------------------------------------------------------------------------
# SPA-Auslieferung
# ---------------------------------------------------------------------------

def mount_spa(app: FastAPI) -> None:
    """Montiert die gebaute SPA als Fallback.

    Muss NACH allen API-Routern aufgerufen werden. Nicht-API-Requests ohne
    Datei-Match liefern index.html (React-Router uebernimmt), /api/* immer JSON-404.
    Ohne SPA-Verzeichnis (Desktop, lokale Tests) passiert nichts.
    """
    if not SERVER_MODE or not SPA_DIR.is_dir():
        return

    @app.exception_handler(404)
    async def spa_fallback(request: Request, _exc: Exception):
        path = request.url.path
        if path.startswith(_API_PREFIX) or path in PUBLIC_PATHS:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = SPA_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # Echte statische Assets (JS/CSS/Icons) direkt ausliefern
    assets = SPA_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    for name in _STATIC_DATEIEN:
        if (SPA_DIR / name).is_file():
            _statische_datei(app, name)


def _statische_datei(app: FastAPI, name: str) -> None:
    """Eine SPA-Datei als GET-Route. Pfad wird gegen SPA_DIR geprueft (kein Traversal)."""
    pfad = (SPA_DIR / name).resolve()
    if not pfad.is_relative_to(SPA_DIR.resolve()):
        return

    def liefern() -> FileResponse:
        return FileResponse(pfad)

    app.add_api_route(f"/{name}", liefern, methods=["GET"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Session aus dem Cookie (gemeinsam fuer Auth-Guard und /api/auth/status)
# ---------------------------------------------------------------------------

def _session_factory():
    """Einziger Ort, an dem die DB-Session fuer Auth-Lookups herkommt (Tests biegen
    hier eine tmp-DB ein, statt den globalen engine zu benutzen)."""
    from database.connection import SessionLocal
    return SessionLocal()


def session_zur_anfrage(headers: Headers):
    """Liest das Session-Cookie (``rf_session``) und liefert die gueltige Session-
    Zeile oder None. Pruefung und Sliding Renewal macht ``auth_sessions.get_session``."""
    from auth_sessions import COOKIE_NAME, get_session

    sid = None
    for teil in headers.get("cookie", "").split(";"):
        name, _, wert = teil.strip().partition("=")
        if name == COOKIE_NAME and wert:
            sid = wert.strip('"')
            break
    if not sid:
        return None
    db = _session_factory()
    try:
        return get_session(db, sid)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CORS nur Desktop - Server-Modus ist same-origin
# ---------------------------------------------------------------------------

def cors_middleware_kwargs() -> dict | None:
    """None im Server-Modus (same-origin, keine CORS-Middleware), sonst die
    unveraenderte Desktop-Konfiguration aus main.py."""
    if SERVER_MODE:
        return None
    return {
        "allow_origins": ["http://localhost:5173", "tauri://localhost", "http://tauri.localhost"],
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": [
            "Content-Disposition",
            "X-Datev-Eintraege",
            "X-Datev-Uebersprungen",
            # fehlten bis Issue #502: in der installierten App (anderer Origin als das Backend)
            # waren sie für das Frontend unlesbar - „ohne Sachkonto" zeigte immer 0
            "X-Datev-LeerKonto",
            "X-Datev-Belege",
            "X-Datev-Verrechnung",
            "X-Datev-Verrechnung-Belege",
            "X-Buchhalter-Eintraege",
            "X-GoBD-Buchungen",
            "X-GoBD-Belege",
        ],
    }


# ---------------------------------------------------------------------------
# Auth-Guard / CSRF
# ---------------------------------------------------------------------------

def ist_geschuetzt(path: str) -> bool:
    """Alle /api/* ausser /api/auth/*; /health, /docs, /redoc, /openapi.json frei.
    Alles ausserhalb von /api/ (SPA, Assets) ist ebenfalls frei."""
    if not path.startswith(_API_PREFIX):
        return False
    return not path.startswith(_PUBLIC_PREFIXES)


def _same_origin(origin: str, host_header: str, scheme: str, forwarded_proto: str) -> bool:
    """Origin gegen Host vergleichen. Das Schema zaehlt nur, wenn es eindeutig ist
    (echtes https oder ``X-Forwarded-Proto: https`` vom TLS-Proxy); hinter einem
    reinen HTTP-LAN-Zugriff waere ein erzwungener https-Vergleich ein Fehlalarm.

    Der Port wird nur verglichen, wenn BEIDE Seiten einen expliziten Port haben:
    Proxies wie nginx ``proxy_set_header Host $host`` oder NPM strippen den Port
    aus dem Host-Header, der Browser schickt ihn im Origin aber mit (z.B.
    Origin ``https://host:8443`` vs. Host ``host``). Ohne Port-Abgleich waere jeder
    Request hinter solchem Proxy ein Fehlalarm (403 cross_origin_forbidden)."""
    try:
        teile = urlsplit(origin)
    except ValueError:
        return False
    if not teile.scheme or not teile.netloc:
        return False
    if not _host_netloc_gleich(teile.netloc, host_header.strip()):
        return False
    if (forwarded_proto or "").strip().lower().split(",")[0] == "https" and teile.scheme.lower() != "https":
        return False
    if scheme.lower() == "https" and teile.scheme.lower() != "https":
        return False
    return True


def _host_netloc_gleich(origin_netloc: str, host_header: str) -> bool:
    """Vergleicht host[:port] – Port nur wenn beide einen haben (Default-Ports gelten als gleich)."""
    def _split(netloc: str):
        # IPv6 in Klammern: [::1]:8443
        if netloc.startswith("["):
            ende = netloc.find("]")
            host = netloc[1:ende]
            rest = netloc[ende + 1:]
            port = rest[1:] if rest.startswith(":") else ""
            return host.lower(), port
        host, _, port = netloc.partition(":")
        return host.lower(), port

    o_host, o_port = _split(origin_netloc)
    h_host, h_port = _split(host_header)
    if o_host != h_host:
        return False
    # Ein Port auf einer Seite fehlt (Proxy hat ihn gestrippt) -> Hosts gleich,
    # Port nicht vergleichbar -> akzeptieren. Default-Ports gelten als leer.
    if o_port in ("", "443", "80") or h_port in ("", "443", "80"):
        return True
    return o_port == h_port


def origin_ist_fremd(headers: Headers, scheme: str) -> bool:
    """True, wenn ein Origin-Header vorhanden ist und nicht zum Host passt.

    Mutierende Requests ohne Origin UND ohne Referer gelten ebenfalls als fremd:
    SameSite=Lax allein ist keine zweite Deckung (alte Webviews, Same-Site-Bypass)."""
    origin = headers.get("origin")
    if origin:
        if origin.strip() == "null":  # sandboxed iframe / file://
            return True
        return not _same_origin(origin, headers.get("host", ""), scheme, headers.get("x-forwarded-proto", ""))
    referer = headers.get("referer")
    if referer:
        try:
            teile = urlsplit(referer)
        except ValueError:
            return True
        return not _host_netloc_gleich(teile.netloc, headers.get("host", "").strip())
    return True


def _setze_session(scope: Scope, sitzung) -> None:
    """Session fuer die Routen bereitstellen.

    ``request.state`` (Starlette-``State``) ist kein verlaesslicher Transport aus einer
    ASGI-Middleware: je nach Routing-Schicht (inkl. eigener Middleware-Stacks der ASGI-
    App) entstehen neue ``Scope``-dicts. Deshalb steht die Session in
    ``scope[SESSION_SCOPE_KEY]`` (Scope-Referenzen bleiben erhalten) UND im Request-State,
    wo dieser schon existiert."""
    scope[SESSION_SCOPE_KEY] = sitzung
    current = scope.get("state")
    if current is not None:
        try:
            current["session"] = sitzung
        except (TypeError, AttributeError):
            pass


def session_aus_scope(scope: Mapping) -> object | None:
    """Session des aktuellen Requests (von :class:`ServerModeMiddleware` gesetzt)."""
    sitzung = scope.get(SESSION_SCOPE_KEY)
    if sitzung is not None:
        return sitzung
    state = scope.get("state")
    return getattr(state, "session", None) if state is not None else None


class ServerModeMiddleware:
    """Security-Header (auch auf 401/403/404), CSRF-Check, Auth-Guard.

    Setzt ``request.state.session`` fuer geschuetzte API-Requests. Die Header werden
    in ``send`` ergaenzt, damit auch Exception-Handler-Antworten sie tragen."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        pfad = scope.get("path", "")
        methode = scope.get("method", "GET").upper()

        async def send_mit_headern(message) -> None:
            if message["type"] == "http.response.start":
                b = MutableHeaders(raw=message.get("headers", []))
                for k, v in SECURITY_HEADER.items():
                    if k not in b:  # case-insensitive
                        b.append(k, v)
            await send(message)

        if methode in _CSRF_METHODEN and pfad.startswith(_API_PREFIX):
            if origin_ist_fremd(headers, scope.get("scheme", "http")):
                log.warning("CSRF-Check: Cross-Origin %s %s abgelehnt.", methode, pfad)
                await _json_antwort(send_mit_headern, 403, {"detail": "cross_origin_forbidden"})
                return

        if ist_geschuetzt(pfad):
            sitzung = session_zur_anfrage(headers)
            if sitzung is None:
                await _json_antwort(send_mit_headern, 401, {"detail": "not_authenticated"})
                return
            _setze_session(scope, sitzung)

        await self.app(scope, receive, send_mit_headern)


async def _json_antwort(send: Send, status: int, inhalt: dict) -> None:
    body = json.dumps(inhalt).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Rate-Limit (slowapi)
# ---------------------------------------------------------------------------

def limiter_erzeugen():
    """Limiter ohne Default-Limits (nur explizit begrenzte Pfade).

    Schluessel ist die Client-IP. Hinter dem NPM-Proxy steht in
    ``request.client.host`` nur die Proxy-IP - im Server-Modus wird deshalb die
    erste IP aus ``X-Forwarded-For`` genommen (NPM setzt den Header immer und
    ueberschreibt clientseitig hereinkommende Werte). Der Container-Port 8000
    darf nie direkt erreichbar sein, sonst liesse sich der Header spoofen."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    import server_mode

    if server_mode.SERVER_MODE:
        def key_func(request) -> str:
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                return xff.split(",")[0].strip()
            return request.client.host if request.client else "unknown"

        return Limiter(key_func=key_func, default_limits=[])

    return Limiter(key_func=get_remote_address, default_limits=[])


def _ratelimit_limits_setzen(app: FastAPI) -> None:
    """Setzt ``ratelimit``-Markierungen auf den auth-Routen (slowapi wertet sie ueber
    die SlowAPIMiddleware aus - login/callback bleiben normale Handler).

    FastAPI 0.139 loest include_router-Routen erst beim ersten Request auf; damit die
    Markierung nicht verloren geht, laeuft sie vor jedem Request erneut (idempotent)."""
    from api.auth import RATE_LIMIT

    limitierte = {"/api/auth/login", "/api/auth/callback"}
    # Router-Routen koennen (FastAPI 0.139) noch im Low-Priority-Store liegen
    weitere = []
    sammeln = getattr(app.router, "_iter_low_priority_routes", None)
    if sammeln is not None:
        try:
            weitere = list(sammeln())
        except TypeError:
            weitere = []
    for route in list(app.routes) + weitere:
        if getattr(route, "path", None) in limitierte:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None and not getattr(endpoint, "ratelimit", None):
                endpoint.ratelimit = [RATE_LIMIT]


# ---------------------------------------------------------------------------
# Startup / Verdrahtung (nur Server-Modus)
# ---------------------------------------------------------------------------

def pruefe_konfiguration() -> None:
    """OIDC-Pflicht-Env pruefen und die Config fuer den auth-Router setzen.
    Wirft ``AuthKonfigFehler`` mit klarer Meldung (Startup-Fehler)."""
    from api import auth as auth_modul

    auth_modul.konfiguriere(auth_modul.lade_config())


def initialisiere(app: FastAPI) -> None:
    """Verdrahtet den Server-Modus an einer Stelle: Config pruefen (Startup-Fehler
    bei fehlenden Env-Variablen), Rate-Limit, Middleware. No-op im Desktop-Modus."""
    if not SERVER_MODE:
        return

    from slowapi import _rate_limit_exceeded_handler
    from slowapi.middleware import SlowAPIMiddleware

    # 1) Pflicht-Env
    pruefe_konfiguration()

    # 1b) Abgelaufene Auth-Sessions regelmaessig aufraeumen (sonst waechst die
    # Tabelle unbegrenzt; GoBD-relevant ist nur die Pruefung bei jedem Zugriff).
    # WICHTIG: Dieses on_event wird bei Import (main.py: initialisiere()) registriert
    # und laeuft damit VOR main.startup() -> Base.metadata.create_all(). Auf einem
    # frischen Volumen existiert auth_sessions da noch nicht und purge crasht den
    # Start. Deshalb Tabelle hier idempotent anlegen (gleiches Muster wie
    # main._startup_auth_tabelle), bevor purge zugreift.
    from auth_sessions import AuthSession, purge_abgelaufen
    from database.connection import Base, SessionLocal, engine

    def _auth_tabelle_sicher_anlegen() -> None:
        try:
            Base.metadata.create_all(bind=engine, tables=[AuthSession.__table__])
        except Exception:
            log.exception("Server-Modus: auth_sessions konnte nicht angelegt werden")

    @app.on_event("startup")
    def _purge_bei_startup() -> None:
        _auth_tabelle_sicher_anlegen()
        db = SessionLocal()
        try:
            geloescht = purge_abgelaufen(db)
            if geloescht:
                log.info("Server-Modus: %d abgelaufene Auth-Sessions entfernt.", geloescht)
        finally:
            db.close()

    # 2) Rate-Limit auf /api/auth/login + /callback (10/min/IP)
    app.state.limiter = limiter_erzeugen()
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    _ratelimit_limits_setzen(app)
    app.add_middleware(SlowAPIMiddleware)

    # 3) Markierungen aktualisieren, sobald Routen aufgeloest werden (FastAPI 0.139)
    schon_aufgeloest = len(app.routes)

    @app.middleware("http")
    async def ratelimit_markierungen(request, call_next):
        nonlocal schon_aufgeloest
        if len(app.routes) != schon_aufgeloest:
            schon_aufgeloest = len(app.routes)
            _ratelimit_limits_setzen(app)
        return await call_next(request)

    # 4) Zuletzt = aeusserste Schicht: Security-Header, CSRF, Auth-Guard
    app.add_middleware(ServerModeMiddleware)

    log.info("Server-Modus aktiv (SPA-Verzeichnis: %s)", SPA_DIR)


__all__ = [
    "SERVER_MODE", "SPA_DIR", "PUBLIC_PATHS", "SECURITY_HEADER", "CSP",
    "ServerModeMiddleware", "mount_spa", "ist_geschuetzt", "origin_ist_fremd",
    "initialisiere", "pruefe_konfiguration", "cors_middleware_kwargs",
    "session_zur_anfrage", "session_aus_scope", "SESSION_SCOPE_KEY", "limiter_erzeugen",
    "setze_ratelimit_limits",
]


def setze_ratelimit_limits(app: FastAPI) -> None:
    """Oeffentliche Huelle fuer die Rate-Limit-Markierung (vor jedem Request)."""
    _ratelimit_limits_setzen(app)
