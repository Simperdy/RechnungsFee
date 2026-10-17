"""
Issue #442: externe Backups (NAS/USB/SMB) wuchsen unbegrenzt. Nach einem erfolgreichen Backup
bleiben je Zielordner nur die neuesten backup_extern_anzahl (Standard 5) eigenen Dateien -
fremde Dateien im selben Ordner werden nie angefasst.
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database.connection import Base
from database.models import Unternehmen


@pytest.fixture
def db_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "rechnungsfee.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "APP_DATA_DIR", tmp_path)
    return engine


def _unternehmen(engine, ziel: Path, anzahl: int | None = None) -> None:
    session = sessionmaker(bind=engine)()
    kw = {} if anzahl is None else {"backup_extern_anzahl": anzahl}
    session.add(Unternehmen(
        firmenname="Test GmbH", strasse="Teststr.", hausnummer="1", plz="12345", ort="Testort",
        backup_extern_pfad_1=str(ziel), backup_extern_passwort="test-passwort-123",
        backup_extern_pfad_1_lokal_ok=True, **kw,
    ))
    session.commit()
    session.close()


def _alte_backups(ziel: Path, n: int) -> list[str]:
    ziel.mkdir(parents=True, exist_ok=True)
    namen = [f"rechnungsfee_2025010{i}_120000.zip.enc" for i in range(1, n + 1)]
    for name in namen:
        (ziel / name).write_bytes(b"alt")
    return namen


def test_standard_behaelt_fuenf_neueste(db_setup, tmp_path):
    ziel = tmp_path / "nas"
    alte = _alte_backups(ziel, 7)
    fremd = ziel / "urlaubsfotos.zip"
    fremd.write_bytes(b"x")
    _unternehmen(db_setup, ziel)

    ergebnis = main.backup_erstellen()

    assert not ergebnis.get("fehler")
    eigene = sorted(p.name for p in ziel.glob("rechnungsfee_*.zip.enc"))
    assert len(eigene) == 5
    # neues Backup + die 4 jüngsten alten bleiben, die 3 ältesten sind weg
    assert eigene[:4] == alte[3:]
    assert not any(n in eigene for n in alte[:3])
    assert fremd.exists()


def test_eigene_anzahl(db_setup, tmp_path):
    ziel = tmp_path / "usb"
    _alte_backups(ziel, 4)
    _unternehmen(db_setup, ziel, anzahl=2)

    main.backup_erstellen()

    assert len(list(ziel.glob("rechnungsfee_*.zip.enc"))) == 2


def test_zu_loeschende_backups_ignoriert_fremde_namen():
    namen = ["rechnungsfee_20250101_120000.zip.enc", "rechnungsfee_20250102_120000.zip.enc",
             "rechnungsfee_alt.zip.enc", "rechnungsfee_20250103_120000.db", "notiz.txt"]
    assert main._zu_loeschende_backups(namen, 1) == ["rechnungsfee_20250101_120000.zip.enc"]
    assert main._zu_loeschende_backups(namen, 5) == []


def test_schema_grenzen():
    from api.schemas import UnternehmenBase
    import pydantic
    basis = dict(firmenname="X", strasse="S", hausnummer="1", plz="12345", ort="O")
    assert UnternehmenBase(**basis).backup_extern_anzahl == 5
    with pytest.raises(pydantic.ValidationError):
        UnternehmenBase(**basis, backup_extern_anzahl=0)
