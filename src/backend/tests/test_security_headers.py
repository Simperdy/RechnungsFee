"""Security-Tests Server-Modus: Header, Rate-Limit, CSRF.

Läuft gegen eine minimale FastAPI-App mit ``server_mode.ServerModeMiddleware``
und gestubbter Session-Factory — ohne echtes OIDC.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("RECHNUNGSFEE_SERVER_MODE", "1")
os.environ.setdefault("SESSION_SECRET", "test-secret-" + "x" * 32)
os.environ.setdefault("OIDC_ISSUER", "https://idp.example.org")
os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
os.environ.setdefault("OIDC_REDIRECT_URI", "https://rf.example.org/api/auth/callback")
os.environ.setdefault("OIDC_ALLOWED_EMAILS", "caro@example.org")

import httpx
import server_mode
from auth_sessions import create_session
from database.connection import Base
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def sid(db):
    res = create_session(db, "caro@example.org")
    return res[0] if isinstance(res, tuple) else res


@pytest.fixture
def app(db, monkeypatch):
    monkeypatch.setattr(server_mode, "_session_factory", lambda: db)
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    application = FastAPI()

    @application.get("/api/konten")
    def konten():
        return {"ok": True}

    @application.post("/api/konten")
    def konten_post():
        return {"ok": True}

    @application.get("/api/auth/status")
    def status():
        return {"authenticated": False}

    @application.get("/health")
    def health():
        return {"status": "ok"}

    application.add_middleware(server_mode.ServerModeMiddleware)
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://rf.example.org") as c:
        yield c


@pytest.mark.anyio
async def test_security_header_auf_api_und_401(client):
    r = await client.get("/api/konten")
    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in r.headers["content-security-policy"]


@pytest.mark.anyio
async def test_health_ohne_session_und_mit_headern(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.anyio
async def test_cross_origin_post_403_same_origin_ok(client, sid):
    r = await client.post(
        "/api/konten",
        cookies={"rf_session": sid},
        headers={"Origin": "https://evil.example.org"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "cross_origin_forbidden"

    r = await client.post(
        "/api/konten",
        cookies={"rf_session": sid},
        headers={"Origin": "https://rf.example.org"},
    )
    assert r.status_code == 200

    # GET ist CSRF-sicher — fremd-ist ok
    r = await client.get("/api/konten", cookies={"rf_session": sid},
                         headers={"Origin": "https://evil.example.org"})
    assert r.status_code == 200


@pytest.mark.anyio
async def test_geschuetzter_endpoint_ohne_session_401(client):
    r = await client.get("/api/konten")
    assert r.status_code == 401
    assert r.json() == {"detail": "not_authenticated"}


@pytest.mark.anyio
async def test_auth_status_ohne_session_200(client):
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}
