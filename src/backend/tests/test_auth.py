"""
Tests fuer OIDC-Auth (Task 2): Stub-IdP via httpx.MockTransport, echte
authlib-Clienten, tmp-SQLite fuer die Session-Tabelle, eigene Mini-App mit den
Server-Modus-Teilen (kein main.py-Import - das wuerde den GoBD-Startup ausloesen).

Abgedeckt: login 302 mit state+PKCE, callback (Session + Cookie-Flags), status,
logout (revoke), abgelaufene Session 401, nicht-allowlist Email 403, geschuetzter
Endpoint ohne/mit Cookie, Konfig-Fehler bei fehlender Env.
"""
import base64
import hashlib
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import server_mode
from api import auth as auth_modul
from auth_sessions import AuthSession, create_session, get_session, sid_hash
from database.connection import Base, get_db

ISSUER = "https://idp.example.org"
CLIENT_ID = "rechnungsfee"
REDIRECT_URI = "https://rf.example.org/api/auth/callback"
ERLAUBT = "caro@example.org"
GEHEIM = "s" * 48


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class StubIdP:
    """Minimaler OIDC-Anbieter: Discovery, JWKS, Token-Endpoint. Merkt sich das
    letzte Token-Request (code_verifier, redirect_uri) zur Pruefung im Test."""

    def __init__(self) -> None:
        self.key = RSAKey.import_key(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
        )
        self.letztes_token_request: dict = {}
        self.email = ERLAUBT
        self.email_verified = True
        self.nonce = ""

    def claims(self, nonce: str, email: str | None = None) -> dict:
        jetzt = int(time.time())
        return {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "sub-123",
            "email": email or self.email,
            "email_verified": self.email_verified,
            "iat": jetzt,
            "exp": jetzt + 300,
            "nonce": nonce,
        }

    def id_token(self, nonce: str, email: str | None = None) -> str:
        return joserfc_jwt.encode({"alg": "RS256", "kid": "test"}, self.claims(nonce, email), self.key)

    @property
    def jwks(self) -> dict:
        return {"keys": [self.key.as_dict(private=False) | {"kid": "test", "alg": "RS256", "use": "sig"}]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        pfad = request.url.path
        if pfad == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
            })
        if pfad == "/jwks":
            return httpx.Response(200, json=self.jwks)
        if pfad == "/token":
            form = parse_qs(request.content.decode())
            self.letztes_token_request = {k: v[0] for k, v in form.items()}
            # Nonce kennt der Token-Endpoint im echten Flow nicht - der Test setzt sie
            # aus der Authorize-URL von /api/auth/login (stub.nonce = ...).
            return httpx.Response(200, json={
                "access_token": "at-123",
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": self.id_token(self.nonce),
            })
        return httpx.Response(404, json={"error": "not_found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[AuthSession.__table__])
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def stub_idp():
    return StubIdP()


@pytest.fixture
def app(db_factory, stub_idp, monkeypatch):
    """Mini-App mit Auth-Router, ServerModeMiddleware (Auth-Guard) und Test-Endpoint."""
    config = auth_modul.ServerAuthConfig(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret=None,
        redirect_uri=REDIRECT_URI, allowed_emails=frozenset({ERLAUBT}), session_secret=GEHEIM,
    )
    auth_modul.konfiguriere(config)
    # authlib-Client im Callback gegen den Stub-IdP verdrahten
    original = auth_modul.oauth_client
    monkeypatch.setattr(
        auth_modul, "oauth_client",
        lambda cfg, transport=None: original(cfg, transport=stub_idp.transport()),
    )
    monkeypatch.setattr(server_mode, "_session_factory", db_factory)
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)

    app = FastAPI()

    def _db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db

    @app.get("/api/konten")
    def konten():
        return {"items": []}

    app.include_router(auth_modul.router)
    app.add_middleware(server_mode.ServerModeMiddleware)

    yield app
    auth_modul.router._config = None  # type: ignore[attr-defined]


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rf.example.org")


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

def test_lade_config_meldet_fehlende_env_klar():
    with pytest.raises(auth_modul.AuthKonfigFehler) as exc:
        auth_modul.lade_config({})
    text = str(exc.value)
    for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_REDIRECT_URI", "OIDC_ALLOWED_EMAILS", "SESSION_SECRET"):
        assert name in text


def test_lade_config_lehnt_http_issuer_und_kurzes_secret_ab():
    basis = {
        "OIDC_ISSUER": "http://idp.example.org",
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_REDIRECT_URI": REDIRECT_URI,
        "OIDC_ALLOWED_EMAILS": ERLAUBT,
        "SESSION_SECRET": GEHEIM,
    }
    with pytest.raises(auth_modul.AuthKonfigFehler, match="OIDC_ISSUER"):
        auth_modul.lade_config(basis)
    basis["OIDC_ISSUER"] = ISSUER
    basis["SESSION_SECRET"] = "kurz"
    with pytest.raises(auth_modul.AuthKonfigFehler, match="SESSION_SECRET"):
        auth_modul.lade_config(basis)


def test_lade_config_normalisiert_allowlist():
    config = auth_modul.lade_config({
        "OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": CLIENT_ID, "OIDC_REDIRECT_URI": REDIRECT_URI,
        "OIDC_ALLOWED_EMAILS": " Caro@Example.org , zweite@example.org ", "SESSION_SECRET": GEHEIM,
    })
    assert config.allowed_emails == {"caro@example.org", "zweite@example.org"}
    assert config.metadata_url == ISSUER + "/.well-known/openid-configuration"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_login_redirectet_mit_state_und_pkce(app):
    async with _client(app) as c:
        r = await c.get("/api/auth/login")
    assert r.status_code == 302
    ziel = urlsplit(r.headers["location"])
    assert f"{ziel.scheme}://{ziel.netloc}{ziel.path}" == ISSUER + "/authorize"
    q = parse_qs(ziel.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [REDIRECT_URI]
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["state"][0]) >= 32 and len(q["nonce"][0]) >= 32
    assert len(q["code_challenge"][0]) == 43  # base64url(SHA-256) ohne Padding

    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("rf_oidc_pending_")][0]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/api/auth" in cookie


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

async def _login_und_callback(app, stub, *, email=None, email_verified=True):
    """login + callback wie der Browser: gleicher Client (Flow-Cookie)."""
    stub.email = email or ERLAUBT
    stub.email_verified = email_verified
    async with _client(app) as c:
        r_login = await c.get("/api/auth/login")
        q = parse_qs(urlsplit(r_login.headers["location"]).query)
        stub.nonce = q["nonce"][0]
        r_cb = await c.get(f"/api/auth/callback?code=abc&state={q['state'][0]}")
        return r_login, r_cb


@pytest.mark.anyio
async def test_callback_legt_session_an_und_setzt_sicheren_cookie(app, stub_idp, db_factory):
    async with _client(app) as c:
        r_login = await c.get("/api/auth/login")
        q = parse_qs(urlsplit(r_login.headers["location"]).query)
        state = q["state"][0]
        challenge = q["code_challenge"][0]
        stub_idp.nonce = q["nonce"][0]
        r = await c.get(f"/api/auth/callback?code=abc&state={state}")

    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # Token-Tausch mit PKCE-Verifier, dessen S256-Challenge zum login passt
    sent = stub_idp.letztes_token_request
    assert sent["code"] == "abc"
    assert sent["redirect_uri"] == REDIRECT_URI
    assert sent["grant_type"] == "authorization_code"
    assert _b64u(hashlib.sha256(sent["code_verifier"].encode()).digest()) == challenge

    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("rf_session=")][0]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/" in cookie
    assert "Max-Age=2592000" in cookie  # 30 d Cookie; absolute Laufzeit serverseitig (expires_at)
    sid = cookie.split("rf_session=")[1].split(";")[0]

    # Klartext-SID nirgends in der DB - nur der SHA-256-Hash
    db = db_factory()
    try:
        zeilen = db.query(AuthSession).all()
        assert len(zeilen) == 1
        assert zeilen[0].email == ERLAUBT
        assert zeilen[0].sid_hash == sid_hash(sid)
        assert sid != zeilen[0].sid_hash
        assert get_session(db, sid) is not None
    finally:
        db.close()

    # Nach dem Callback: geschuetzter Endpoint und status sind erreichbar
    async with _client(app) as c:
        s = await c.get("/api/auth/status", headers={"cookie": f"rf_session={sid}"})  # DBG
        k = await c.get("/api/konten", cookies={"rf_session": sid})
    assert s.json() == {"authenticated": True, "email": ERLAUBT}
    assert k.status_code == 200


@pytest.mark.anyio
async def test_callback_cookie_ist_secure_hinter_https_proxy(app, stub_idp):
    async with _client(app) as c:
        r_login = await c.get("/api/auth/login")
        q = parse_qs(urlsplit(r_login.headers["location"]).query)
        stub_idp.nonce = q["nonce"][0]
        r = await c.get(
            f"/api/auth/callback?code=abc&state={q['state'][0]}",
            headers={"x-forwarded-proto": "https"},
        )
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("rf_session=")][0]
    assert "Secure" in cookie


@pytest.mark.anyio
async def test_callback_lehnt_falschen_state_ab(app):
    async with _client(app) as c:
        await c.get("/api/auth/login")
        r = await c.get("/api/auth/callback?code=abc&state=gefaelscht")
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert "invalid_state" in r.text


@pytest.mark.anyio
async def test_callback_ohne_flow_cookie_ist_400(app):
    async with _client(app) as c:
        r = await c.get("/api/auth/callback?code=abc&state=irgendwas")
    assert r.status_code == 400
    assert "invalid_state" in r.text
    assert "text/html" in r.headers["content-type"]
    assert "invalid_state" in r.text


@pytest.mark.anyio
async def test_callback_nicht_allowlist_email_403_ohne_session(app, stub_idp, db_factory):
    _, r = await _login_und_callback(app, stub_idp, email="fremd@example.org")
    assert r.status_code == 403
    assert "not_allowed" in r.text
    assert "nicht freigeschaltet" in r.text
    db = db_factory()
    try:
        assert db.query(AuthSession).count() == 0
    finally:
        db.close()


@pytest.mark.anyio
async def test_callback_email_nicht_verifiziert_403(app, stub_idp):
    _, r = await _login_und_callback(app, stub_idp, email_verified=False)
    assert r.status_code == 403
    assert "not_allowed" in r.text


@pytest.mark.anyio
async def test_callback_falsche_nonce_wird_abgelehnt(app, stub_idp):
    async with _client(app) as c:
        r_login = await c.get("/api/auth/login")
        q = parse_qs(urlsplit(r_login.headers["location"]).query)
        stub_idp.nonce = "voellig-andere-nonce"
        r = await c.get(f"/api/auth/callback?code=abc&state={q['state'][0]}")
    assert r.status_code == 400
    assert "login_failed" in r.text


# ---------------------------------------------------------------------------
# Status / Logout / Guard
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_status_ohne_session_200_false(app):
    async with _client(app) as c:
        r = await c.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}


@pytest.mark.anyio
async def test_geschuetzter_endpoint_ohne_cookie_401(app):
    async with _client(app) as c:
        r = await c.get("/api/konten")
    assert r.status_code == 401
    assert r.json() == {"detail": "not_authenticated"}


@pytest.mark.anyio
async def test_abgelaufene_session_401(app, db_factory):
    db = db_factory()
    try:
        sid, zeile = create_session(db, ERLAUBT)
        zeile.expires_at = zeile.expires_at - timedelta(hours=9)
        db.commit()
    finally:
        db.close()
    async with _client(app) as c:
        r = await c.get("/api/konten", cookies={"rf_session": sid})
        s = await c.get("/api/auth/status", headers={"cookie": f"rf_session={sid}"})  # DBG
    assert r.status_code == 401
    assert s.json() == {"authenticated": False}


@pytest.mark.anyio
async def test_unbekannte_session_id_401(app):
    async with _client(app) as c:
        assert (await c.get("/api/konten", cookies={"rf_session": "nicht-existent"})).status_code == 401


@pytest.mark.anyio
async def test_sliding_renewal_verlaengert_expiry(app, db_factory):
    db = db_factory()
    try:
        sid, zeile = create_session(db, ERLAUBT)
        alt = zeile.expires_at - timedelta(hours=4)  # 4 h "verstrichen"
        zeile.expires_at = alt
        db.commit()
    finally:
        db.close()
    async with _client(app) as c:
        assert (await c.get("/api/konten", cookies={"rf_session": sid})).status_code == 200
    db = db_factory()
    try:
        assert db.query(AuthSession).one().expires_at > alt
    finally:
        db.close()


@pytest.mark.anyio
async def test_logout_revokiert_session_und_loescht_cookie(app, db_factory):
    db = db_factory()
    try:
        sid, _ = create_session(db, ERLAUBT)
    finally:
        db.close()
    async with _client(app) as c:
        assert (await c.get("/api/konten", cookies={"rf_session": sid})).status_code == 200
        r = await c.post("/api/auth/logout", cookies={"rf_session": sid}, headers={"Origin": "https://rf.example.org"})
        assert r.status_code == 302
        assert any(h.startswith("rf_session=") and "Max-Age=0" in h for h in r.headers.get_list("set-cookie"))
    db = db_factory()
    try:
        assert db.query(AuthSession).one().revoked_at is not None
        assert get_session(db, sid) is None
    finally:
        db.close()
    async with _client(app) as c:
        assert (await c.get("/api/konten", cookies={"rf_session": sid})).status_code == 401


@pytest.mark.anyio
async def test_public_endpoints_bleiben_ohne_session_erreichbar(app):
    @app.get("/health")
    def health():
        return {"status": "ok"}

    async with _client(app) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/api/auth/status")).status_code == 200


# ---------------------------------------------------------------------------
# Pending-Cookie (state/nonce/verifier) - Integritaet
# ---------------------------------------------------------------------------

def test_pending_cookie_ist_signiert_und_laeuft_ab():
    token = auth_modul.signiere_pending(
        {"state": "a", "nonce": "b", "verifier": "c", "iat": int(time.time())}, GEHEIM
    )
    assert auth_modul.lese_pending(token, GEHEIM)["state"] == "a"
    assert auth_modul.lese_pending(token, "anderes-secret") is None
    fremd = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert auth_modul.lese_pending(fremd, GEHEIM) is None
    alt = auth_modul.signiere_pending({"state": "a", "iat": int(time.time()) - 3600}, GEHEIM)
    assert auth_modul.lese_pending(alt, GEHEIM) is None


def test_pkce_pair_passt_zusammen():
    verifier, challenge = auth_modul.pkce_pair()
    assert _b64u(hashlib.sha256(verifier.encode()).digest()) == challenge
    assert 43 <= len(verifier) <= 128


# ---------------------------------------------------------------------------
# Session-Tabelle
# ---------------------------------------------------------------------------

def test_sid_wird_nur_als_hash_gespeichert(db_factory):
    db = db_factory()
    try:
        sid, zeile = create_session(db, "  Caro@Example.org ")
        assert zeile.email == "caro@example.org"
        assert zeile.sid_hash == hashlib.sha256(sid.encode()).hexdigest()
        assert len(zeile.sid_hash) == 64
        assert zeile.revoked_at is None
        assert zeile.expires_at - zeile.created_at == timedelta(hours=8)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Rate-Limit (slowapi) - Runde 3
# ---------------------------------------------------------------------------

@pytest.fixture
def rl_app(db_factory, monkeypatch):
    """Aeussere App mit echtem initialisiere()-Pfad (Limiter + SlowAPIMiddleware + Guard)."""
    from database.connection import get_db

    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    monkeypatch.setattr(server_mode, "_session_factory", db_factory)
    app = FastAPI()

    def _db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    # initialisiere() liest die Config aus der Env (pruefe_konfiguration) -> hier setzen
    for k, v in {
        "OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": CLIENT_ID, "OIDC_REDIRECT_URI": REDIRECT_URI,
        "OIDC_ALLOWED_EMAILS": ERLAUBT, "SESSION_SECRET": GEHEIM,
    }.items():
        monkeypatch.setenv(k, v)
    app.include_router(auth_modul.router)
    server_mode.initialisiere(app)
    return app


@pytest.mark.anyio
async def test_rate_limit_login_und_callback_429(rl_app):
    """Regression: Runde 1+2 meldeten 'Rate-Limit aktiv'. 14 Logins pro IP muessen
    ab dem 11. Request 429 liefern - die slowapi-Middleware liest nur
    Limiter._route_limits (Dekorator), nicht ein manuell gesetztes
    endpoint.ratelimit-Attribut."""
    transport = httpx.ASGITransport(app=rl_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://rf.example.org",
                                 headers={"x-forwarded-for": "203.0.113.7"}) as c:
        codes = [(await c.get("/api/auth/login")).status_code for _ in range(14)]
    assert codes[:10] == [302] * 10, codes
    assert codes[10:] == [429] * 4, codes


@pytest.mark.anyio
async def test_rate_limit_ohne_initialisiere_schlaegt_nicht_fehl(app):
    """Ohne Limiter (Desktop-Modus / Mini-App) darf der Login weiterlaufen."""
    async with _client(app) as c:
        assert (await c.get("/api/auth/login")).status_code == 302


def test_lade_config_http_private_ip_nur_mit_dev_option(monkeypatch):
    """http://<private LAN-IP> ist nur mit RECHNUNGSFEE_OIDC_HTTP_LAN=1 erlaubt
    (sonst Klartext-OIDC im Produktivbetrieb moeglich)."""
    basis = {
        "OIDC_ISSUER": "http://192.168.66.203:9999",
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_REDIRECT_URI": REDIRECT_URI,
        "OIDC_ALLOWED_EMAILS": ERLAUBT,
        "SESSION_SECRET": GEHEIM,
    }
    monkeypatch.delenv("RECHNUNGSFEE_OIDC_HTTP_LAN", raising=False)
    with pytest.raises(auth_modul.AuthKonfigFehler, match="OIDC_ISSUER"):
        auth_modul.lade_config(basis)
    monkeypatch.setenv("RECHNUNGSFEE_OIDC_HTTP_LAN", "1")
    cfg = auth_modul.lade_config(basis)  # jetzt erlaubt (Stub-IdP im LAN)
    assert cfg.issuer == "http://192.168.66.203:9999"
    # localhost bleibt ohne Option erlaubt
    basis["OIDC_ISSUER"] = "http://localhost:9999"
    monkeypatch.delenv("RECHNUNGSFEE_OIDC_HTTP_LAN", raising=False)
    assert auth_modul.lade_config(basis).issuer == "http://localhost:9999"
