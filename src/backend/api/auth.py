"""OIDC-Auth gegen PocketID (Authorization Code + PKCE) fuer den Server-Modus.

Nur aktiv wenn ``RECHNUNGSFEE_SERVER_MODE=1``; im Desktop-Modus wird dieses Modul
von ``main.py`` gar nicht importiert. Endpunkte:

- ``GET /api/auth/login``    -> 302 zum IdP (PKCE S256 + state + nonce)
- ``GET /api/auth/callback`` -> state/nonce pruefen, id_token verifizieren,
  email gegen ``OIDC_ALLOWED_EMAILS``, Session anlegen + Cookie setzen
- ``GET /api/auth/logout``   -> Session revokieren, Cookie loeschen, 302 auf /
- ``GET /api/auth/status``   -> ``{"authenticated": bool, "email": str | None}``

Env (alle Pflicht im Server-Modus - sonst Startup-Fehler mit klarer Meldung):
``OIDC_ISSUER``, ``OIDC_CLIENT_ID``, ``OIDC_REDIRECT_URI``,
``OIDC_ALLOWED_EMAILS`` (kommagetrennt), ``SESSION_SECRET``.
Optional: ``OIDC_CLIENT_SECRET`` (PocketID ist ein "public client").

``state``/``nonce``/``code_verifier`` liegen in einem serverseitig signierten
Cookie (HMAC-SHA256 mit ``SESSION_SECRET``, Pfad ``/api/auth``) - mehrere parallele
Login-Fenster funktionieren, nichts landet in der DB.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
# OIDC-Erweiterung von authlib (nicht in base_client/__init__ exportiert): liefert
# ``fetch_jwk_set`` und ``parse_id_token`` - beides async und ohne Framework-Bindung.
from authlib.integrations.base_client.async_openid import AsyncOpenIDMixin as _OpenIDMixin
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from auth_sessions import (
    COOKIE_NAME,
    SESSION_TTL,
    create_session,
    revoke_session,
)
from database.connection import get_db

log = logging.getLogger(__name__)

_PFLICHT_ENV = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_REDIRECT_URI", "OIDC_ALLOWED_EMAILS", "SESSION_SECRET")
_SCOPE = "openid email profile"
# slowapi-Limit fuer login/callback (pro Client-IP), angehaengt in server_mode.py
RATE_LIMIT = "10/minute"


class AuthKonfigFehler(RuntimeError):
    """Pflicht-Env fehlt/kaputt - ist ein Startup-Fehler, kein Laufzeitproblem."""


@dataclass(frozen=True)
class ServerAuthConfig:
    issuer: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    allowed_emails: frozenset[str]
    session_secret: str

    @property
    def metadata_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"

    def ist_erlaubt(self, email: str) -> bool:
        return email.strip().lower() in self.allowed_emails


def lade_config(env: dict[str, str] | None = None) -> ServerAuthConfig:
    """Liest die OIDC-Konfiguration und meldet alle fehlenden Variablen auf einmal."""
    quelle = os.environ if env is None else env
    fehlend = [n for n in _PFLICHT_ENV if not (quelle.get(n) or "").strip()]
    if fehlend:
        raise AuthKonfigFehler(
            "Server-Modus: fehlende Pflicht-Env " + ", ".join(fehlend) + ". "
            "Siehe src/backend/.env.server.example (nur Schluessel, keine Werte)."
        )
    issuer = quelle["OIDC_ISSUER"].strip()
    # Lokale Tests erlaubt: http://localhost ODER http://<beliebige-IP> (z. B. LAN-IP
    # mit Stub-IdP im selben Netz). Produktion: https-Pflicht bleibt bestehen, weil
    # nur https:// und explizit http://localhost|127.*|192.168.*|10.*|172.16-31.*
    # als "lokal" gelten.
    import ipaddress
    from urllib.parse import urlsplit as _urlsplit
    _host = _urlsplit(issuer).hostname or ""
    try:
        _ip = ipaddress.ip_address(_host)
        _lokal = _ip.is_private or _ip.is_loopback
    except ValueError:
        _lokal = _host == "localhost" or _host.endswith(".localhost")
    if not (issuer.startswith("https://") or _lokal):
        raise AuthKonfigFehler(
            f"Server-Modus: OIDC_ISSUER muss https sein (oder http://localhost/<private IP> fuer lokale Tests), ist {issuer!r}"
        )
    emails = frozenset(e.strip().lower() for e in quelle["OIDC_ALLOWED_EMAILS"].split(",") if e.strip())
    if not emails:
        raise AuthKonfigFehler("Server-Modus: OIDC_ALLOWED_EMAILS enthaelt keine E-Mail (kommagetrennt erwartet).")
    secret = quelle["SESSION_SECRET"].strip()
    if len(secret) < 32:
        raise AuthKonfigFehler("Server-Modus: SESSION_SECRET muss mindestens 32 Zeichen lang sein.")
    try:
        secret.encode("ascii")
    except UnicodeEncodeError:
        raise AuthKonfigFehler("Server-Modus: SESSION_SECRET darf nur ASCII-Zeichen enthalten (HMAC-Signatur).")
    redirect_uri = quelle["OIDC_REDIRECT_URI"].strip()
    if not urlsplit(redirect_uri).netloc:
        raise AuthKonfigFehler(f"Server-Modus: OIDC_REDIRECT_URI ist keine absolute URL: {redirect_uri!r}")
    return ServerAuthConfig(
        issuer=issuer,
        client_id=quelle["OIDC_CLIENT_ID"].strip(),
        client_secret=(quelle.get("OIDC_CLIENT_SECRET") or "").strip() or None,
        redirect_uri=redirect_uri,
        allowed_emails=emails,
        session_secret=secret,
    )


# ---------------------------------------------------------------------------
# Signierter Flow-State (state, nonce, code_verifier)
# ---------------------------------------------------------------------------

_PENDING_COOKIE_PREFIX = "rf_oidc_pending_"  # + state[:16]: parallele Flows kollidieren nicht
_PENDING_MAX_ALTER = 600  # 10 min ab login; laenger dauert kein Login


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signatur(payload: str, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()


def signiere_pending(daten: dict, secret: str) -> str:
    payload = _b64(json.dumps(daten, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload}.{_b64(_signatur(payload, secret))}"


def lese_pending(token: str | None, secret: str) -> dict | None:
    """Prueft Signatur (constant-time) und Alter; None bei Manipulation/Ablauf."""
    if not token or "." not in token:
        return None
    payload, sig = token.split(".", 1)
    try:
        # Signatur bindet das Secret als BYTES - bei Nicht-ASCII-Secret (Umlaute/EUR aus
        # einem Passwort-Manager) liefert _signatur beim Lesen einen anderen Wert ->
        # lese_pending gab immer None (jeder Login failed nach dem IdP-Redirect).
        if not hmac.compare_digest(_signatur(payload, secret).hex(), _unb64(sig).hex()):
            return None
        daten = json.loads(_unb64(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(daten, dict) or int(daten.get("iat", 0)) + _PENDING_MAX_ALTER < int(time.time()):
        return None
    return daten


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256) gemaess RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# OIDC-Client
# ---------------------------------------------------------------------------

class _SessionOhneSchliessen:
    """Kontext-Wrapper: authlibs Mixin oeffnet die Session mit ``async with`` - ein
    httpx-Client darf aber nur einmal geoeffnet werden. Hier bleibt es ein no-op; der
    Callback schliesst den Client am Ende selbst."""

    def __init__(self, client) -> None:
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *_exc) -> bool:
        return False


class OidcClient(AsyncOAuth2Client):
    """Async-OAuth2-Client mit den OIDC-Methoden von authlib (``parse_id_token``:
    Nonce-, Issuer-, Audience- und JWKS-Signaturpruefung; ``fetch_jwk_set``).

    ``load_server_metadata`` liefert hier nur das Discovery-Dokument (authlibs
    Registry-Variante erwartet ein ``framework``-Argument). Trust-Regel fuer den
    Issuer: https bzw. http://localhost, siehe ``lade_config``."""

    def __init__(self, *args, server_metadata_url: str | None = None, config_issuer: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_metadata_url = server_metadata_url
        self.config_issuer = config_issuer
        self.server_metadata: dict = {}

    async def load_server_metadata(self) -> dict:
        if not self.server_metadata:
            # withhold_token: kein Authorization-Header ans Discovery-Dokument
            resp = await self.request("GET", self.server_metadata_url or "", withhold_token=True)
            resp.raise_for_status()
            self.server_metadata = resp.json()
        return self.server_metadata

    def _get_session(self):
        return _SessionOhneSchliessen(self)

    async def fetch_jwk_set(self, force: bool = False):
        return await _OpenIDMixin.fetch_jwk_set(self, force=force)

    async def parse_id_token(self, token, nonce, claims_options=None, claims_cls=None, leeway=120):
        return await _OpenIDMixin.parse_id_token(
            self, token, nonce,
            claims_options=claims_options, claims_cls=claims_cls, leeway=leeway,
        )


def oauth_client(config: ServerAuthConfig, transport: httpx.BaseTransport | None = None) -> OidcClient:
    """Neuer OIDC-Client pro Flow - authlibs interner State ist nicht threadsicher;
    Discovery-Dokument und JWKS laedt der Client selbst.
    ``transport`` dient nur den Tests (``httpx.MockTransport``)."""
    return OidcClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        scope=_SCOPE,
        code_challenge_method="S256",
        server_metadata_url=config.metadata_url,
        config_issuer=config.issuer,
        trust_env=False,
        timeout=10.0,
        **({"transport": transport} if transport is not None else {}),
    )


# ---------------------------------------------------------------------------
# Cookie-Helfer
# ---------------------------------------------------------------------------

def ist_https(request: Request) -> bool:
    """Secure-Cookie nur ueber TLS.

    Im Server-Modus sitzt immer ein TLS-terminierender Proxy (NPM) davor, der
    ``X-Forwarded-Proto: https`` setzt. Uvicorn vertraut dem Header aber nur von
    127.0.0.1 (Default ``--forwarded-allow-ips``) - im Docker-Netz ist die
    Proxy-IP nicht Loopback, deshalb wird das Flag hier hart auf True gesetzt,
    sobald wir im Server-Modus laufen (Deployment-Regel: Port 8000 nie direkt
    erreichbar, immer hinter NPM/TLS)."""
    import server_mode
    if server_mode.SERVER_MODE:
        return True
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


_LAX = "lax"


def session_cookie_params(sid: str, request: Request) -> dict:
    """``set_cookie``-Parameter an einer Stelle, damit Test und Code dieselben Flags
    pruefen: HttpOnly, Secure (ueber https), SameSite=Lax, Pfad /."""
    return {
        "key": COOKIE_NAME,
        "value": sid,
        # Cookie lebt laenger als die serverseitige Session (30 d); die absolute
        # Laufzeit erzwingt allein expires_at in auth_sessions (Sliding Renewal).
        "max_age": int(timedelta(days=30).total_seconds()),
        "httponly": True,
        "secure": ist_https(request),
        "samesite": _LAX,
        "path": "/",
    }


def loesche_session_cookie(response: Response) -> None:
    # Beide Secure-Varianten loeschen, damit ein Wechsel http<->https kein Cookie stehen laesst
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite=_LAX, secure=False)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite=_LAX, secure=True)


def _db_neu() -> Session:
    """Eigene DB-Session fuer logout/status (laufen vor bzw. unabhaengig von get_db)."""
    import server_mode
    return server_mode._session_factory()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["Auth"])


def get_auth_config() -> ServerAuthConfig:
    """FastAPI-Dependency: Konfiguration aus ``konfiguriere`` (main.py-Startup) oder
    direkt aus der Env (direkte Nutzung/Tests)."""
    config = getattr(router, "_config", None)
    if isinstance(config, ServerAuthConfig):
        return config
    return lade_config()


def limitiere(func):
    """Aussenliegender Dekorator: setzt den slowapi-Limit erst beim ersten Request.

    SlowAPIMiddleware liest Limits ausschliesslich aus ``Limiter._route_limits``, das nur
    ``Limiter.limit(...)`` befuellt - und das braucht den Limiter aus
    ``main.initialisiere()`` (Import-Reihenfolge: auth-Router vor initialisiere).
    Hier wird der echte Handler (gleicher __module__/__name__ wie die Route) einmalig
    dekoriert und ab dann direkt aufgerufen; im Desktop-Modus limitiert slowapi nicht.
    """
    zustand: dict = {}

    @functools.wraps(func)
    def _mit_limit(*args, **kwargs):
        from starlette.requests import Request as _Request

        request = args[0] if args and isinstance(args[0], _Request) else kwargs.get("request")
        limiter = getattr(request.app.state, "limiter", None) if request is not None else None
        if id(limiter) not in zustand:
            zustand[id(limiter)] = limiter.limit(RATE_LIMIT)(func) if limiter is not None else func
        return zustand[id(limiter)](*args, **kwargs)

    return _mit_limit


def konfiguriere(config: ServerAuthConfig) -> None:
    router._config = config  # type: ignore[attr-defined]


@router.get("/login")
@limitiere
def login(request: Request):
    """Startet den Authorization-Code-Flow: 302 zum IdP."""
    config = get_auth_config()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()

    url = config.issuer.rstrip("/") + "/authorize?" + urlencode({
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": _SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # PocketID/IdP-Login erzwingen: ohne prompt wuerde /api/auth/login die bestehende
        # IdP-Session wiederverwenden. Nach einem Logout/abgelehnten Login kann das zu einer
        # Redirect-Schleife (App -> login -> callback -> App) fuehren.
        "prompt": "select_account",
    })
    response = RedirectResponse(url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        _PENDING_COOKIE_PREFIX + state[:16],
        signiere_pending(
            {"state": state, "nonce": nonce, "verifier": verifier, "iat": int(time.time())},
            config.session_secret,
        ),
        max_age=_PENDING_MAX_ALTER,
        httponly=True,
        secure=ist_https(request),
        samesite=_LAX,
        path="/api/auth",
    )
    return response


@router.get("/callback")
@limitiere
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, error_description: str | None = None,
                   db: Session = Depends(get_db)):
    """Callback des IdP: state/nonce pruefen, id_token verifizieren, Allowlist pruefen,
    Session anlegen. Das Pending-Cookie wird in jedem Fall am Ende geloescht."""
    config = get_auth_config()

    cookie_name = _PENDING_COOKIE_PREFIX + (state or "")[:16] if state else _PENDING_COOKIE_PREFIX

    def _fehler_response(status: int, detail: str, log_msg: str | None = None) -> Response:
        if log_msg:
            log.warning("%s", log_msg)
        resp = _auth_fehlerseite(status, detail)
        resp.delete_cookie(cookie_name, path="/api/auth", httponly=True, samesite=_LAX)
        return resp

    if error:
        # error_description kommt aus der Query und darf nicht ungefiltert ins Log
        log.warning("OIDC-Login vom IdP abgebrochen: %r", (error_description or error)[:200])
        return _fehler_response(400, "login_failed")

    # Pro Flow eindeutig benanntes Cookie — parallele Logins (zweiter Tab/Geraet)
    # ueberschreiben sich nicht gegenseitig.
    pending = lese_pending(request.cookies.get(cookie_name), config.session_secret)
    if not pending or not code or not state or not hmac.compare_digest(str(pending.get("state")), state):
        # state passt nicht zum signierten Flow-Cookie -> CSRF-Versuch oder abgelaufen
        resp = _fehler_response(400, "invalid_state", "OIDC-Callback: state/Flow-Cookie ungueltig.")
        resp.delete_cookie(cookie_name, path="/api/auth", httponly=True, samesite=_LAX)
        return resp

    try:
        client = oauth_client(config)
        try:
            metadata = await client.load_server_metadata()
            # Discovery-Dokument muss vom konfigurierten Issuer stammen (sonst
            # Token-/JWKS-Endpunkte eines fremden IdP nach DNS-/Proxy-Hijack).
            if str(metadata.get("issuer") or "").rstrip("/") != config.issuer.rstrip("/"):
                raise AuthKonfigFehler("OIDC-Discovery: issuer im Dokument != OIDC_ISSUER")
            token = await client.fetch_token(
                metadata["token_endpoint"],
                code=code,
                redirect_uri=config.redirect_uri,
                code_verifier=pending["verifier"],
                grant_type="authorization_code",
            )
            # Strikte iss/aud-Pruefung (fail-closed) — sonst wuerde ein id_token
            # einer anderen Client-App am selben IdP akzeptiert.
            claims = await client.parse_id_token(
                token, nonce=pending["nonce"], leeway=120,
                claims_options={
                    "iss": {"essential": True, "value": config.issuer},
                    "aud": {"essential": True, "values": [config.client_id]},
                },
            )
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - authlib wirft diverse OAuthError-Varianten
        log.warning("OIDC: Token-Tausch/ID-Token-Pruefung fehlgeschlagen: %r", str(exc)[:200])
        return _fehler_response(400, "login_failed")

    email = str(claims.get("email") or "").strip().lower()
    if claims.get("email_verified") is not True:  # strikt boolesch, fail-closed (fehlend/"true"/1 ablehnen)
        return _fehler_response(403, "not_allowed", "OIDC-Login abgelehnt: email_verified=false.")
    if not config.ist_erlaubt(email):
        # Bewusst ohne die Adresse im Log (personenbezogen), nur der Hinweis.
        return _fehler_response(403, "not_allowed", "OIDC-Login abgelehnt: E-Mail nicht in OIDC_ALLOWED_EMAILS.")

    sid, sitzung = create_session(
        db, email,
        idp_sub=str(claims.get("sub") or "") or None,
        idp_session=str(claims.get("sid") or "") or None,
    )
    response = RedirectResponse("/", status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(**session_cookie_params(sid, request))
    response.delete_cookie(cookie_name, path="/api/auth", httponly=True, samesite=_LAX)
    log.info("Server-Modus: Session angelegt (id=%s)", sitzung.id)
    return response


_AUTH_FEHLER_SEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anmeldung fehlgeschlagen – RechnungsFee</title>
<style>body{font-family:system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;color:#1c1917}
h1{font-size:1.25rem}code{background:#f5f5f4;padding:.1rem .3rem;border-radius:.25rem}
a{color:#b45309}.hinweis{color:#57534e;font-size:.9rem}</style></head>
<body><h1>Anmeldung fehlgeschlagen</h1>
<p class="hinweis">{detail_text}</p>
<p><code>{detail}</code></p>
<p><a href="/api/auth/login">Erneut anmelden</a></p></body></html>"""

_DETAIL_TEXTE = {
    "login_failed": "Der Anmelde-Vorgang beim Identitaetsanbieter ist fehlgeschlagen. Bitte versuche es erneut.",
    "invalid_state": "Die Anmeldungssitzung ist abgelaufen oder ungueltig. Bitte versuche es erneut.",
    "not_allowed": "Dieses Konto ist fuer diese RechnungsFee-Instanz nicht freigeschaltet. Bitte wende dich an den Administrator.",
}


def _auth_fehlerseite(status: int, detail: str) -> Response:
    """Minimale Fehlerseite statt nacktem JSON - der OIDC-Callback ist ein
    Top-Level-Navigation-Request, JSON sieht der Nutzer nie."""
    from fastapi.responses import HTMLResponse
    text = _DETAIL_TEXTE.get(detail, "Unbekannter Fehler.")
    body = _AUTH_FEHLER_SEITE.replace("{detail_text}", text).replace("{detail}", detail)
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/logout")
def logout(request: Request):
    """Revokiert die Session serverseitig und loescht das Cookie."""
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        db = _db_neu()
        try:
            revoke_session(db, sid)
        finally:
            db.close()
    response = RedirectResponse("/", status_code=302)
    response.headers["Cache-Control"] = "no-store"
    loesche_session_cookie(response)
    return response


def get_current_session(request: Request):
    """Session des Requests oder None.

    Primär: die Middleware liest das Cookie selbst (damit /api/auth/status ohne
    Middleware-Lücke funktioniert). Fallback: von der Middleware in den Scope gelegter Wert.
    """
    import server_mode

    sitzung = server_mode.session_zur_anfrage(Headers(scope=request.scope))
    if sitzung is None:
        sitzung = server_mode.session_aus_scope(request.scope)
    return sitzung


@router.get("/status")
def status(request: Request):
    """Vom Frontend beim Start abgefragt - ohne gueltige Session 200 mit false."""
    sitzung = get_current_session(request)
    if sitzung is None:
        return JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"authenticated": True, "email": sitzung.email},
        headers={"Cache-Control": "no-store"},
    )


__all__ = [
    "AuthKonfigFehler", "RATE_LIMIT", "ServerAuthConfig", "lade_config", "konfiguriere",
    "get_auth_config", "router", "signiere_pending", "lese_pending", "session_cookie_params",
    "ist_https", "oauth_client", "pkce_pair", "OidcClient",
]
