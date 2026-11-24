"""
Tests für den Server-Modus (Task 1): BASE_DIR-Override, SPA-Auslieferung, Health.

Der Env-Override in database/connection.py wirkt nur beim Modul-Import
(Modul-Level-Konstanten). Deshalb wird das Modul hier kontrolliert via
importlib.reload neu geladen - mit anschließendem Restore (die bestehenden
Objekte (engine, SessionLocal, Base, Base.metadata) werden zurückgeschrieben,
damit andere Tests der Suite weiter gegen die ursprüngliche DB laufen).
Alle Tests laufen in einem eigenen tmp-Verzeichnis, nie im echten Datenordner.
"""
import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database.connection as connection
import server_mode


class _ConnectionSnapshot:
    """Alle Modul-Attribute von database.connection vor dem Reload."""

    def __init__(self) -> None:
        self.werte: dict = dict(connection.__dict__)

    def restore(self) -> None:
        modul = connection.__dict__
        for name in list(modul):
            if name not in self.werte:
                del modul[name]
        modul.update(self.werte)


def _reload_connection():
    """Reload mit anschließendem Restore: Rückgabe ist eine Teilmenge der
    Modul-Attribute (BASE_DIR, APP_DATA_DIR, DB_PATH) des Reload-Zustands."""
    snap = _ConnectionSnapshot()
    try:
        modul = importlib.reload(connection)
        return {
            "BASE_DIR": modul.BASE_DIR,
            "APP_DATA_DIR": modul.APP_DATA_DIR,
            "DB_PATH": modul.DB_PATH,
            "DATABASE_URL": modul.DATABASE_URL,
        }
    finally:
        snap.restore()


def test_base_dir_folgt_env_override(monkeypatch, tmp_path):
    ziel = tmp_path / "server-data"
    monkeypatch.setenv("RECHNUNGSFEE_BASE_DIR", str(ziel))
    monkeypatch.setenv("RECHNUNGSFEE_SERVER_MODE", "1")

    m = _reload_connection()
    assert m["BASE_DIR"] == ziel.resolve()
    # Profilmanager legt APP_DATA_DIR eine Ebene tiefer an
    assert m["APP_DATA_DIR"] == ziel.resolve() / "profile" / "Standard"
    assert m["DB_PATH"] == m["APP_DATA_DIR"] / "rechnungsfee.db"
    assert ziel.is_dir()
    # Restore: die Suite laeuft weiter gegen die urspruengliche DB
    assert connection.DB_PATH != m["DB_PATH"]


def test_ohne_env_bleibt_plattform_verhalten(monkeypatch, tmp_path):
    monkeypatch.delenv("RECHNUNGSFEE_BASE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Linux")

    m = _reload_connection()
    assert m["BASE_DIR"] == tmp_path / ".local" / "share" / "RechnungsFee"


def _spa_app(tmp_path: Path, monkeypatch) -> FastAPI:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(server_mode, "SPA_DIR", static)
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)

    app = FastAPI()

    @app.get("/api/konten")
    def konten():
        return {"ok": True}

    server_mode.mount_spa(app)
    return app


def test_mount_spa_fallback_und_api_404(tmp_path, monkeypatch):
    client = TestClient(_spa_app(tmp_path, monkeypatch))

    # Nicht-API-Pfad ohne Datei -> index.html (React-Router uebernimmt)
    r = client.get("/kassenbuch")
    assert r.status_code == 200
    assert "SPA" in r.text
    # Echte API-Route bleibt unberuehrt
    assert client.get("/api/konten").json() == {"ok": True}
    # API-Pfad ohne Route -> JSON-404, niemals index.html
    r = client.get("/api/gibts-nicht")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "SPA" not in r.text
    # Statisches Asset wird direkt ausgeliefert
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_mount_spa_ohne_verzeichnis_crasht_nicht(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mode, "SPA_DIR", tmp_path / "gibt-es-nicht")
    monkeypatch.setattr(server_mode, "SERVER_MODE", True)
    app = FastAPI()

    @app.get("/api/konten")
    def konten():
        return {"ok": True}

    server_mode.mount_spa(app)  # kein Mount, kein Crash
    client = TestClient(app)
    assert client.get("/api/konten").json() == {"ok": True}
    r = client.get("/kassenbuch")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_mount_spa_inaktiv_ohne_server_mode(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    monkeypatch.setattr(server_mode, "SPA_DIR", static)
    monkeypatch.setattr(server_mode, "SERVER_MODE", False)
    app = FastAPI()
    server_mode.mount_spa(app)
    client = TestClient(app)
    assert client.get("/kassenbuch").status_code == 404
    assert not any(getattr(r, "name", "") == "assets" for r in app.routes)


def test_initialisiere_startup_auf_frischem_volumen_ohne_auth_tabelle(tmp_path, monkeypatch):
    """Regression: initialisiere() registriert _purge_bei_startup bei Import und damit
    VOR main.startup()/create_all. Auf einem frischen Volumen (keine auth_sessions-
    Tabelle) darf der Startup trotzdem nicht crashen - die Tabelle wird idempotent
    vor dem Purge angelegt (Bug: frischer Docker-Erststart starb mit
    'no such table: auth_sessions')."""
    import os
    import sqlite3

    daten = tmp_path / "server-data"
    daten.mkdir()
    monkeypatch.setenv("RECHNUNGSFEE_SERVER_MODE", "1")
    monkeypatch.setenv("RECHNUNGSFEE_BASE_DIR", str(daten))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-mindestens-32-zeichen-lang!!")
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("OIDC_CLIENT_ID", "rechnungsfee")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app.example/api/auth/callback")
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "a@example.org")

    # connection wie in test_base_dir_folgt_env_override auf tmp-Volumen umbiegen
    snap = _ConnectionSnapshot()
    try:
        importlib.reload(connection)
        assert str(connection.BASE_DIR) == str(daten)

        # server_mode.initialisiere nutzt database.connection.engine/SessionLocal -
        # nach dem Reload zeigen die Namen in server_mode auf die alten Objekte,
        # deshalb hier die frischen explizit patchen.
        monkeypatch.setattr(server_mode, "SERVER_MODE", True)
        app = FastAPI()

        @app.get("/health")
        def health():
            return {"ok": True}

        server_mode.initialisiere(app)
        # Startup-Events ausloesen (TestClient macht das beim __enter__; hier manuell)
        import anyio
        async def _startup():
            for h in app.router.on_startup:
                r = h()
                if hasattr(r, "__await__"):
                    await r
        anyio.from_thread.run(_startup) if False else None
        with TestClient(app) as client:
            assert client.get("/health").json() == {"ok": True}

        # auth_sessions existiert jetzt im frischen Volumen
        db_path = connection.DB_PATH
        assert db_path.exists()
        con = sqlite3.connect(str(db_path))
        try:
            con.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()
        finally:
            con.close()
    finally:
        snap.restore()
        importlib.reload(connection)
