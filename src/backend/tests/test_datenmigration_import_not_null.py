"""
Regressionstest: CSV-Import (Datenübernahme) schlägt auf einer frischen Datenbank
fehl mit "sqlite3.IntegrityError: NOT NULL constraint failed":

- lieferanten.ust_idnr_validiert  (INSERT in _lieferant_anlegen fehlt die Spalte)
- kunden.mahnung_gesperrt / kunden.mahnung_warnung (in _kunde_anlegen fehlen sie)
- artikel.vk_eingabe (in _artikel_anlegen fehlt die Spalte)

Ursache: Die Spalten sind in models.py als NOT NULL ohne server_default definiert -
beim create_all einer neuen DB entsteht dadurch eine Spalte OHNE SQL-DEFAULT -,
und die rohen INSERTs in api/datenmigration.py lassen die Spalten weg.
Bei migrierten Datenbanken war das Problem unsichtbar, weil die ALTER-TABLE-
Migrationen der betroffenen Spalten ein DEFAULT mitgeliefert haben.

Fix: INSERTs ergänzen (wirkt auch bei bereits bestehenden Datenbanken) UND
server_default in den Modellen (härtet das Schema künftiger Neuanlagen).
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from api.datenmigration import _artikel_anlegen, _kunde_anlegen, _lieferant_anlegen


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _pragma(db, tabelle):
    return {row[1]: row[4] for row in db.execute(text(f"PRAGMA table_info({tabelle})"))}


class TestImportAufFrischerDb:
    """Die rohen INSERTs der Datenübernahme müssen auf einer per create_all
    erzeugten Datenbank durchlaufen - ohne NOT-NULL-IntegrityError."""

    def test_lieferanten_import(self, db):
        status = _lieferant_anlegen(db, {
            "firmenname": "Test Lieferant GmbH",
            "land": "DE",
            "lieferantennummer": "L-00001",
        })
        db.commit()
        assert status == "importiert"
        row = db.execute(text(
            "SELECT ust_idnr_validiert, aktiv FROM lieferanten WHERE lieferantennummer = 'L-00001'"
        )).fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == 1

    def test_kunden_import(self, db):
        status = _kunde_anlegen(db, {
            "firmenname": "Test Kunde GmbH",
            "land": "DE",
            "kundennummer": "K-00001",
        })
        db.commit()
        assert status == "importiert"
        row = db.execute(text(
            "SELECT ust_idnr_validiert, ist_verein, ist_gemeinnuetzig, zugferd_aktiv, "
            "mahnung_gesperrt, mahnung_warnung FROM kunden WHERE kundennummer = 'K-00001'"
        )).fetchone()
        assert row is not None
        assert row == (0, 0, 0, 0, 0, 0)

    def test_artikel_import(self, db):
        status = _artikel_anlegen(db, {
            "bezeichnung": "Yoga-Stunde 60min",
            "vk_brutto": "19,00",
            "artikelnummer": "A-00001",
        })
        db.commit()
        assert status == "importiert"
        row = db.execute(text(
            "SELECT vk_eingabe, differenzbesteuerung, aktiv FROM artikel WHERE artikelnummer = 'A-00001'"
        )).fetchone()
        assert row is not None
        assert row[0] == "brutto"
        assert row[1] == 0
        assert row[2] == 1


class TestSchemaServerDefaults:
    """Die betroffenen NOT-NULL-Spalten müssen in einer frisch erzeugten
    Datenbank einen SQL-DEFAULT haben, damit auch rohe INSERTs ohne diese
    Spalten durchlaufen (models.py: server_default)."""

    @pytest.mark.parametrize("tabelle,spalten", [
        ("kunden", ["ust_idnr_validiert", "ist_verein", "ist_gemeinnuetzig",
                    "zugferd_aktiv", "mahnung_gesperrt", "mahnung_warnung", "aktiv"]),
        ("lieferanten", ["ust_idnr_validiert", "aktiv"]),
        ("artikel", ["vk_eingabe", "differenzbesteuerung", "aktiv"]),
    ])
    def test_default_vorhanden(self, db, tabelle, spalten):
        defaults = _pragma(db, tabelle)
        for spalte in spalten:
            assert defaults[spalte] is not None, (
                f"{tabelle}.{spalte} hat in der frisch erzeugten Datenbank keinen DEFAULT"
            )
