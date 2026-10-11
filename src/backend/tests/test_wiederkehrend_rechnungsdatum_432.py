"""
Regressionstest für Issue #432.

_erstelle_entwurf() setzte das Rechnungsdatum bisher auf vorlage.naechstes_datum (den
GEPLANTEN Termin) statt auf das tatsächliche Erstellungsdatum. Im Normalbetrieb (App läuft
regelmäßig, der automatische Check läuft nahe am geplanten Termin) fiel das kaum auf. War die
App aber länger nicht offen oder wurde der "jetzt erstellen"-Button (entwurf_jetzt(), bewusst
"ohne Datumscheck") frühzeitig ausgelöst, bekam die gerade erst erstellte Rechnung ein stark
abweichendes Datum - real gemeldet: eine am 11.10.2026 erstellte Rechnung trug das Datum
11.03.2023. Das verstößt gegen §14 Abs. 4 UStG (Ausstellungsdatum muss dem tatsächlichen Tag
der Rechnungsstellung entsprechen) und kann eine Rechnung in einen bereits abgeschlossenen
Steuerzeitraum zurückdatieren.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.wiederkehrend import entwurf_jetzt, pruefen_intern
from database.connection import Base
from database.models import Rechnung, Rechnungsvorlage, Unternehmen


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


def _vorlage(db, naechstes_datum: date, zahlungsziel_tage: int = 14) -> Rechnungsvorlage:
    v = Rechnungsvorlage(
        bezeichnung="Monatliche Wartung", intervall="monatlich",
        naechstes_datum=naechstes_datum, positionen_json="[]",
        zahlungsziel_tage=zahlungsziel_tage,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def test_manuelle_erstellung_bekommt_heutiges_datum_trotz_stark_veraltetem_termin(db):
    """Exaktes Szenario aus Issue #432: naechstes_datum liegt Jahre in der Vergangenheit."""
    v = _vorlage(db, date(2023, 3, 11))

    entwurf_jetzt(v.id, db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    assert rechnung.datum == date.today()
    assert rechnung.datum != date(2023, 3, 11)


def test_faellig_am_wird_aus_dem_tatsaechlichen_erstellungsdatum_abgeleitet(db):
    v = _vorlage(db, date(2023, 3, 11), zahlungsziel_tage=14)

    entwurf_jetzt(v.id, db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    assert rechnung.faellig_am == date.today() + timedelta(days=14)


def test_automatischer_check_verwendet_ebenfalls_das_heutige_datum(db):
    """pruefen_intern() (App-Start) ist vom selben Fix betroffen - gleiche Ursache
    (_erstelle_entwurf()), gleiche Korrektur."""
    v = _vorlage(db, date.today() - timedelta(days=400))

    pruefen_intern(db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    assert rechnung is not None
    assert rechnung.datum == date.today()


def test_normalfall_naechstes_datum_ist_heute_bleibt_unveraendert(db):
    """Regressionsschutz: im Normalbetrieb (Termin = heute) ändert sich am Ergebnis nichts."""
    v = _vorlage(db, date.today())

    entwurf_jetzt(v.id, db)

    rechnung = db.query(Rechnung).filter(Rechnung.vorlage_id == v.id).first()
    assert rechnung.datum == date.today()
