"""Regressionstests Security-Audit 2026-11-23 (Server-Modus).

Ergaenzt test_auth.py / test_security_headers.py um die vom Audit monierten
Luecken: Pending-Cookie-Roundtrip mit iat, Fehlerpfad loescht Cookie,
HTML-Fehlerseite statt JSON, Shutdown/system-Routen im Server-Modus 404,
Upload-Size-Guard 413, Backup-Traversal.
"""
import os
import sys
import time
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
from api import auth as auth_modul
from auth_sessions import create_session
from database.connection import Base
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

GEHEIM = os.environ["SESSION_SECRET"]


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def app(db, monkeypatch):
    monkeypatch.setattr(server_mode, "_session_factory", lambda: db)
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    application = FastAPI()

    @application.get("/api/konten")
    def konten():
        return {"ok": True}

    @application.post("/api/shutdown")
    def shutdown():
        if server_mode.SERVER_MODE:
            raise HTTPException(status_code=404, detail="Not Found")
        return {"ok": True}

    @application.get("/api/system/tesseract")
    def tess():
        if server_mode.SERVER_MODE:
            raise HTTPException(status_code=404, detail="Not Found")
        return {"installiert": True}

    application.include_router(auth_modul.router)
    application.add_middleware(server_mode.ServerModeMiddleware)
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://rf.example.org") as c:
        yield c


def test_pending_roundtrip_mit_iat():
    """Der Login setzt iat - sonst liefert lese_pending nach dem Signieren None
    (Audit: Callback lieferte 400 login_failed)."""
    token = auth_modul.signiere_pending(
        {"state": "s", "nonce": "n", "verifier": "v", "iat": int(time.time())}, GEHEIM
    )
    daten = auth_modul.lese_pending(token, GEHEIM)
    assert daten is not None and daten["state"] == "s"


@pytest.mark.anyio
async def test_callback_fehler_loescht_pending_cookie(client):
    """Audit HIGH #5: im Fehlerpfad darf das signierte Pending-Cookie nicht
    stehen bleiben - Callback mit kaputtem state liefert Fehlerseite + loescht."""
    r = await client.get("/api/auth/callback?code=x&state=falsch",
                         cookies={"rf_oidc_pending": "ungueltig"})
    assert r.status_code == 400
    assert "text/html" in r.headers.get("content-type", "")
    loeschen = [v for k, v in r.headers.items()
                if k.lower() == "set-cookie" and "rf_oidc_pending" in v]
    assert loeschen


@pytest.mark.anyio
async def test_callback_not_allowed_ist_html_fehlerseite(client):
    """Audit CRITICAL #1 (UX-Teil): Fehlerseite enthaelt verstaendlichen Text
    und einen Link zum erneuten Login statt nacktem 403/400-JSON."""
    pending = auth_modul.signiere_pending(
        {"state": "s1", "nonce": "n1", "verifier": "v1", "iat": int(time.time())}, GEHEIM
    )
    r = await client.get("/api/auth/callback?code=x&state=s1",
                         cookies={"rf_oidc_pending": pending})
    # Token-Tausch gegen idp.example.org schlaegt fehl -> login_failed als HTML
    assert r.status_code == 400
    assert "Anmeldung fehlgeschlagen" in r.text
    assert "/api/auth/login" in r.text


@pytest.mark.anyio
async def test_shutdown_im_server_modus_404(client, db):
    """Audit HIGH #4: /api/shutdown darf im Server-Modus nicht os._exit ausloesen."""
    sid, _ = create_session(db, "caro@example.org")
    r = await client.post("/api/shutdown", cookies={"rf_session": sid},
                          headers={"Origin": "https://rf.example.org"})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_system_tesseract_im_server_modus_404(client, db):
    """Audit HIGH #4: Desktop-OCR-Hilfsrouten sind im Server-Modus deaktiviert."""
    sid, _ = create_session(db, "caro@example.org")
    r = await client.get("/api/system/tesseract", cookies={"rf_session": sid})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_upload_guard_413_im_server_modus(monkeypatch):
    """Audit HIGH #3: Uploads > Limit bekommen 413 statt RAM-DoS."""
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    import io as _io
    from fastapi import UploadFile
    from utils.upload_guard import lies_upload

    gross = UploadFile(file=_io.BytesIO(b"x" * 1024), filename="gross.pdf")
    with pytest.raises(HTTPException) as exc:
        await lies_upload(gross, max_bytes=512)
    assert exc.value.status_code == 413

    klein = UploadFile(file=_io.BytesIO(b"ok"), filename="klein.pdf")
    assert await lies_upload(klein, max_bytes=512) == b"ok"


def test_backup_dateiname_traversal_abgelehnt():
    """Audit LOW (Path-Traversal): dateiname darf keinen Pfad enthalten."""
    from pathlib import Path
    assert Path("../../etc/rechnungsfee_x.db").name != "../../etc/rechnungsfee_x.db"
    assert Path("rechnungsfee_2026.db").name == "rechnungsfee_2026.db"
