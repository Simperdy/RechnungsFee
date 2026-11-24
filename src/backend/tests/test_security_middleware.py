"""
Regressionstests Security-Audit F2, F4 (Teil 1) und F7 (Teil 1) – utils/sicherheit_middleware.py.

F4: GET /api/unternehmen mit „Host: attacker.example“ lieferte HTTP 200 samt Passwörtern
    (DNS-Rebinding). Jetzt: nur 127.0.0.1/localhost.
F7: POST /api/shutdown mit „Origin: https://evil.example“ und Content-Type text/plain
    beendete das Backend (kein CORS-Preflight). Jetzt: 403 bei fremdem Origin.
F2: E-Rechnungs-XML und Postfach-Anhänge kamen inline als application/xml ohne nosniff/CSP;
    eingebettete <h:script>-Elemente liefen im API-Ursprung. Jetzt: text/plain + sandbox.
"""
import asyncio
import uuid

import pytest

import main
from utils.sicherheit_middleware import SicherheitMiddleware, origin_erlaubt


def _rufe(method="GET", host="127.0.0.1:18765", origin=None, content_type=b"application/json",
          extra_headers=None):
    """Führt die Middleware mit einer Dummy-App aus; liefert (status, headers, app_aufgerufen)."""
    aufgerufen = []

    async def app(scope, receive, send):
        aufgerufen.append(True)
        headers = [(b"content-type", content_type)] + (extra_headers or [])
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"x"})

    headers = [(b"host", host.encode())] if host is not None else []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": "http", "method": method, "path": "/api/x", "headers": headers}
    nachrichten = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        nachrichten.append(msg)

    asyncio.run(SicherheitMiddleware(app)(scope, receive, send))
    start = nachrichten[0]
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, bool(aufgerufen)


# ── F4: Host-Header ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["127.0.0.1:18765", "localhost:8002", "localhost", "LOCALHOST:5173", "[::1]:18765"])
def test_host_erlaubt(host):
    status, _, aufgerufen = _rufe(host=host)
    assert status == 200 and aufgerufen


@pytest.mark.parametrize("host", ["attacker.example:18765", "127.0.0.1.evil.example", "evil.localhost:1", "", None])
def test_host_fremd_abgewiesen(host):
    status, _, aufgerufen = _rufe(host=host)
    assert status == 421
    assert not aufgerufen


# ── F7: Origin bei schreibenden Requests ───────────────────────────────────────

@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1.evil.example", "file://"])
def test_post_fremder_origin_abgewiesen(origin):
    status, _, aufgerufen = _rufe(method="POST", origin=origin)
    assert status == 403
    assert not aufgerufen


@pytest.mark.parametrize("origin", [
    "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
    "http://localhost:5173", "http://127.0.0.1:18765",
])
def test_post_eigener_origin_erlaubt(origin):
    status, _, aufgerufen = _rufe(method="POST", origin=origin)
    assert status == 200 and aufgerufen


def test_post_ohne_origin_erlaubt():
    """Tauri-Shutdown per curl, eigene Fenster, Skripte: kein Origin-Header."""
    status, _, aufgerufen = _rufe(method="POST")
    assert status == 200 and aufgerufen


def test_get_mit_fremdem_origin_bleibt_lesbar_fuer_cors():
    """GET wird nicht abgewiesen – das Lesen der Antwort verhindert CORS (kein Header)."""
    status, _, aufgerufen = _rufe(method="GET", origin="https://evil.example")
    assert status == 200 and aufgerufen


def test_origin_erlaubt_kaputte_url():
    assert not origin_erlaubt("http://[::1")


# ── F2: nosniff und CSP auf Datei-Antworten ────────────────────────────────────

@pytest.mark.parametrize("ct", [b"application/json", b"text/html; charset=utf-8", b"application/pdf",
                                b"text/plain; charset=utf-8", b"application/xml", b"image/png"])
def test_nosniff_immer(ct):
    _, headers, _ = _rufe(content_type=ct)
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("ct", [b"application/xml", b"text/xml", b"image/svg+xml", b"text/plain; charset=utf-8",
                                b"image/png", b"text/csv"])
def test_sandbox_fuer_fremde_dateitypen(ct):
    _, headers, _ = _rufe(content_type=ct)
    assert headers["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("ct", [b"application/json", b"text/html; charset=utf-8", b"application/pdf"])
def test_keine_sandbox_fuer_json_html_pdf(ct):
    _, headers, _ = _rufe(content_type=ct)
    assert "content-security-policy" not in headers


def test_eigene_csp_bleibt_erhalten():
    _, headers, _ = _rufe(content_type=b"text/plain",
                          extra_headers=[(b"content-security-policy", b"default-src 'none'")])
    assert headers["content-security-policy"] == "default-src 'none'"


# ── F10: Upload-Limit, API-Doku und Debug-Endpunkt ─────────────────────────────

def _mit_laenge(laenge: int, pfad: str = "/api/x"):
    import utils.sicherheit_middleware as sm
    aufgerufen = []

    async def app(scope, receive, send):
        aufgerufen.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    nachrichten = []

    async def send(m):
        nachrichten.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "POST", "path": pfad,
             "headers": [(b"host", b"127.0.0.1:1"), (b"content-length", str(laenge).encode())]}
    asyncio.run(sm.SicherheitMiddleware(app)(scope, receive, send))
    return nachrichten[0]["status"], bool(aufgerufen)


def test_upload_zu_gross_abgewiesen():
    from utils.sicherheit_middleware import MAX_UPLOAD
    assert _mit_laenge(MAX_UPLOAD + 1) == (413, False)
    assert _mit_laenge(MAX_UPLOAD) == (200, True)


def test_backup_restore_darf_groesser_sein():
    from utils.sicherheit_middleware import MAX_UPLOAD, MAX_UPLOAD_BACKUP
    assert _mit_laenge(MAX_UPLOAD * 4, "/api/backup/wiederherstellen") == (200, True)
    assert _mit_laenge(MAX_UPLOAD_BACKUP + 1, "/api/backup/wiederherstellen") == (413, False)


def test_ocr_debug_im_release_aus(monkeypatch):
    import sys

    from fastapi import HTTPException as HE

    from api.system import ocr_debug
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(HE) as exc:
        asyncio.run(ocr_debug(datei=None))
    assert exc.value.status_code == 404


def test_api_doku_nur_ohne_release(monkeypatch):
    # Testlauf ist kein PyInstaller-Build und kein Server-Modus → Doku an;
    # im Release (sys.frozen) oder Server-Modus (Docker) aus.
    monkeypatch.delenv("RECHNUNGSFEE_SERVER_MODE", raising=False)
    import importlib
    importlib.reload(main)
    assert main._IST_RELEASE is False
    assert main.app.docs_url == "/docs"


def test_api_doku_im_server_modus_aus(monkeypatch):
    """Im Server-Modus (Docker) duerfen /docs, /redoc, /openapi.json nicht
    oeffentlich erreichbar sein — sonst API-Recon ohne Login."""
    import importlib
    import server_mode
    # Server-Modus ohne OIDC-Env darf nicht crashen, nur Doku-Flag aendern:
    monkeypatch.setenv("RECHNUNGSFEE_SERVER_MODE", "1")
    _vals = {"OIDC_ISSUER": "https://idp.example", "OIDC_CLIENT_ID": "x",
             "OIDC_REDIRECT_URI": "https://app.example/api/auth/callback",
             "OIDC_ALLOWED_EMAILS": "a@b.de", "SESSION_SECRET": "x" * 40}
    for k, v in _vals.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    importlib.reload(main)
    try:
        assert main.app.docs_url is None
        assert main.app.redoc_url is None
        assert main.app.openapi_url is None
    finally:
        monkeypatch.delenv("RECHNUNGSFEE_SERVER_MODE", raising=False)
        for k in _vals:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(server_mode, "SERVER_MODE", False)
        importlib.reload(main)


def test_middleware_ist_in_der_app_registriert():
    assert any(m.cls is SicherheitMiddleware for m in main.app.user_middleware)


# ── F2: XML-Auslieferung der Endpunkte ─────────────────────────────────────────

def test_temp_xml_als_text(tmp_path, monkeypatch):
    from api import rechnungen
    monkeypatch.setattr(rechnungen, "TEMP_DIR", tmp_path)
    token = str(uuid.uuid4())
    (tmp_path / f"{token}.xml").write_text('<x xmlns:h="http://www.w3.org/1999/xhtml"><h:script>alert(1)</h:script></x>')
    antwort = rechnungen.get_temp_pdf(token)
    assert antwort.media_type.startswith("text/plain")


def test_temp_pdf_bleibt_pdf(tmp_path, monkeypatch):
    from api import rechnungen
    monkeypatch.setattr(rechnungen, "TEMP_DIR", tmp_path)
    token = str(uuid.uuid4())
    (tmp_path / f"{token}.pdf").write_bytes(b"%PDF-1.4")
    assert rechnungen.get_temp_pdf(token).media_type == "application/pdf"


@pytest.mark.parametrize("mime,erwartet", [
    ("application/xml", "text/plain; charset=utf-8"),
    ("text/xml", "text/plain; charset=utf-8"),
    ("application/pdf", "application/pdf"),
])
def test_postfach_datei_xml_als_text(tmp_path, monkeypatch, mime, erwartet):
    from types import SimpleNamespace

    from api import postfach
    (tmp_path / "a.bin").write_bytes(b"inhalt")
    monkeypatch.setattr(postfach, "UPLOADS", tmp_path)
    monkeypatch.setattr(postfach, "_eintrag", lambda _id, _db: SimpleNamespace(datei_pfad="a.bin", mime_type=mime))
    assert postfach.postfach_datei(1, db=None).media_type == erwartet
