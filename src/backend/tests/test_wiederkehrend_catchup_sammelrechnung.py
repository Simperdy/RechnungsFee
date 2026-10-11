"""
Regressionstest für Issue #432-Folgefund: Nachholen verpasster Perioden in EINER Sammelrechnung.

Vorher erzeugte pruefen_intern()/entwurf_jetzt() pro Aufruf nur einen Entwurf und rückte
naechstes_datum nur um ein Intervall vor - war eine Vorlage mehrere Perioden überfällig (App
lange nicht geöffnet), hätte das erst über mehrere zukünftige App-Starts verteilt nachgeholt
werden können, nicht auf einmal. Jetzt bündelt ein einziger Aufruf alle fälligen Perioden in
EINER Rechnung (mehrfache Positionsblöcke, je mit Periodenhinweis ab der zweiten Periode) und
rückt naechstes_datum direkt bis zur nächsten zukünftigen Periode vor.
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.wiederkehrend import entwurf_jetzt, pruefen_intern, _naechstes_datum
from database.connection import Base
from database.models import Rechnung, Rechnungsposition, Rechnungsvorlage, Unternehmen


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(Unternehmen(firmenname="Test GmbH", strasse="Teststr.", hausnummer="1", plz="12345", ort="Testort"))
    session.commit()
    yield session
    session.close()


def _vorlage(db, naechstes_datum: date, intervall: str = "monatlich") -> Rechnungsvorlage:
    v = Rechnungsvorlage(
        bezeichnung="Monatliche Wartung", intervall=intervall,
        naechstes_datum=naechstes_datum,
        positionen_json='[{"beschreibung": "Wartung", "menge": "1", "netto": "100.00", "ust_satz": "19"}]',
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def test_fuenf_verpasste_monate_landen_in_einer_rechnung(db):
    """App lange nicht offen gewesen (Vorlage seit 2020 überfällig) - ein Aufruf soll alle
    verpassten Monate auf einmal nachholen, nicht nur den ersten."""
    v = _vorlage(db, date(2020, 1, 1))

    entwurf_jetzt(v.id, db)

    rechnungen = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).all()
    assert len(rechnungen) == 1  # genau EINE Sammelrechnung, nicht mehrere Einzelentwürfe

    positionen = db.query(Rechnungsposition).filter(Rechnungsposition.rechnung_id == rechnungen[0].id).all()
    assert len(positionen) >= 5  # mind. 5 Positionsblöcke (einer je nachgeholter Periode)
    # Ab der zweiten Periode steht der Zeitraum in der Beschreibung.
    assert any("Periode" in p.beschreibung for p in positionen)

    # naechstes_datum liegt nach dem Aufholen in der Zukunft, nicht nur einen Monat weiter.
    assert v.naechstes_datum > date.today()


def test_einzelne_faellige_periode_bleibt_unveraendert_ohne_periodenhinweis(db):
    """Normalfall (keine Rückstände): Beschreibung bleibt wie gehabt, kein "(Periode ...)"-Zusatz."""
    v = _vorlage(db, date.today())

    entwurf_jetzt(v.id, db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    positionen = db.query(Rechnungsposition).filter(Rechnungsposition.rechnung_id == rechnung.id).all()
    assert len(positionen) == 1
    assert positionen[0].beschreibung == "Wartung"


def test_vorzeitiger_manueller_trigger_erstellt_genau_eine_periode(db):
    """entwurf_jetzt() läuft "ohne Datumscheck" - naechstes_datum liegt noch in der Zukunft,
    trotzdem muss genau ein (das kommende) Periodenblock entstehen, nicht keiner."""
    kuenftig = _naechstes_datum(date.today(), "monatlich")
    v = _vorlage(db, kuenftig)

    entwurf_jetzt(v.id, db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    assert rechnung is not None
    positionen = db.query(Rechnungsposition).filter(Rechnungsposition.rechnung_id == rechnung.id).all()
    assert len(positionen) == 1
    assert v.naechstes_datum == _naechstes_datum(kuenftig, "monatlich")


def test_pruefen_intern_buendelt_ebenfalls(db):
    v = _vorlage(db, date.today().replace(day=1))
    v.naechstes_datum = date(date.today().year - 1, 1, 1)
    db.commit()

    pruefen_intern(db)

    rechnungen = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).all()
    assert len(rechnungen) == 1
    positionen = db.query(Rechnungsposition).filter(Rechnungsposition.rechnung_id == rechnungen[0].id).all()
    assert len(positionen) >= 10  # mehr als ein Jahr monatlicher Rückstand
