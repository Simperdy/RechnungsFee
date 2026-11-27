"""
Regressionstest für Issue #347.

Bug: backup_wiederherstellen() (main.py) referenzierte die nie definierte Variable
RESTORE_MARKER statt RESTORE_MARKER_ZIP - jeder Wiederherstellungsversuch crashte
mit NameError: name 'RESTORE_MARKER' is not defined ("Failed to fetch" im Frontend,
500 Internal Server Error im Backend). Kritisch, da Backup-Wiederherstellung komplett
unbenutzbar war.
"""
import asyncio
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest

import main


def _db_bytes() -> bytes:
    """Kleine, gültige SQLite-Datenbank (der Upload prüft PRAGMA integrity_check)."""
    with tempfile.TemporaryDirectory() as tmp:
        pfad = Path(tmp) / "x.db"
        con = sqlite3.connect(str(pfad))
        con.execute("CREATE TABLE t (id INTEGER)")
        con.commit()
        con.close()
        return pfad.read_bytes()


def _zip_bytes(mit_db: bool = True, eintraege: dict[str, bytes] | None = None, db: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if mit_db:
            zf.writestr("rechnungsfee.db", db if db is not None else _db_bytes())
        for name, inhalt in (eintraege or {"uploads/dummy.txt": b"x"}).items():
            zf.writestr(name, inhalt)
    return buf.getvalue()


def _upload_file(filename: str, content: bytes):
    from fastapi import UploadFile
    return UploadFile(filename=filename, file=io.BytesIO(content))


def test_backup_wiederherstellen_schreibt_restore_marker_zip(tmp_path, monkeypatch):
    marker_zip = tmp_path / "restore_pending.zip"
    monkeypatch.setattr(main, "RESTORE_MARKER_ZIP", marker_zip)

    inhalt = _zip_bytes()
    datei = _upload_file("backup.zip", inhalt)

    ergebnis = asyncio.run(main.backup_wiederherstellen(datei=datei, passwort=""))

    assert ergebnis == {"ok": True, "neustart_erforderlich": True}
    assert marker_zip.exists()
    assert marker_zip.read_bytes() == inhalt


def test_backup_wiederherstellen_ohne_db_in_zip_wird_abgelehnt(tmp_path, monkeypatch):
    marker_zip = tmp_path / "restore_pending.zip"
    monkeypatch.setattr(main, "RESTORE_MARKER_ZIP", marker_zip)

    datei = _upload_file("backup.zip", _zip_bytes(mit_db=False))

    with pytest.raises(Exception) as exc_info:
        asyncio.run(main.backup_wiederherstellen(datei=datei, passwort=""))
    assert "400" in str(exc_info.value) or "rechnungsfee.db" in str(exc_info.value)
    assert not marker_zip.exists()


def test_pruefe_wiederherstellung_entfernt_stale_wal_shm(tmp_path, monkeypatch):
    """Regression: Restore auf einem Volumen mit laufender alter DB liess stale
    rechnungsfee.db-wal/-shm stehen. Nach dem Ersetzen der DB las SQLite einen WAL,
    der nicht zur neuen DB gehoert -> 'database disk image is malformed' beim Start.
    _prüfe_wiederherstellung() muss die Sidecar-Dateien vor dem Extract loeschen."""
    import sqlite3

    app_data = tmp_path / "profil"
    app_data.mkdir()
    db_path = app_data / "rechnungsfee.db"

    # alte DB + stale WAL/SHM simulieren
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE alt (id INTEGER)")
    con.execute("INSERT INTO alt VALUES (1)")
    con.commit()
    con.close()
    (app_data / "rechnungsfee.db-wal").write_bytes(b"stale-wal")
    (app_data / "rechnungsfee.db-shm").write_bytes(b"stale-shm")

    # Backup-ZIP mit gueltiger neuer DB
    neue_db = tmp_path / "neu.db"
    con = sqlite3.connect(str(neue_db))
    con.execute("CREATE TABLE neu (id INTEGER)")
    con.execute("INSERT INTO neu VALUES (42)")
    con.commit()
    con.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(neue_db, "rechnungsfee.db")
    marker = app_data / "restore_pending.zip"
    marker.write_bytes(buf.getvalue())

    monkeypatch.setattr(main, "RESTORE_MARKER_ZIP", marker)
    monkeypatch.setattr(main, "RESTORE_MARKER_DB", app_data / "restore_pending.db")
    monkeypatch.setattr(main, "APP_DATA_DIR", app_data)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    # _backup_datenbank() schreibt ins backups/-Unterverzeichnis der echten DB -
    # hier auf tmp umbiegen, damit nichts ins echte Profil landet
    monkeypatch.setattr(main, "_backup_datenbank", lambda: None)

    main._prüfe_wiederherstellung()

    assert not marker.exists()
    assert not (app_data / "rechnungsfee.db-wal").exists()
    assert not (app_data / "rechnungsfee.db-shm").exists()
    con = sqlite3.connect(str(db_path))
    assert con.execute("SELECT id FROM neu").fetchone()[0] == 42
    con.close()


def test_zip_bombe_mit_gelogenen_metadaten_wird_abgelehnt(tmp_path):
    """Regression: ZIP-Metadaten (file_size) lügen — real entpackte Bytes werden
    gestueckelt gezaehlt und das Limit hart durchgesetzt (kein zf.read in RAM)."""
    import io, zipfile
    from main import _pruefe_backup_eintraege
    _LIMIT = 1024 * 1024  # 1 MB Test-Limit statt 10 GB

    # Entry behauptet 1 Byte, entpackt aber 5 MB (hochkomprimierbar)
    daten = b"\0" * (5 * 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("uploads/bombe.bin")
        info.file_size = 1  # gelogen
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, daten)
    buf.seek(0)

    import pytest
    with zipfile.ZipFile(buf) as zf:
        with pytest.raises(ValueError):
            _pruefe_backup_eintraege(zf, tmp_path / "uploads", max_groesse=_LIMIT)
