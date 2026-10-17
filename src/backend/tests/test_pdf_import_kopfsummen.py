"""
Issue #450: PDF-Import mit Rabattzeile ergab widersprüchliche Kopfsummen.

1. Parser: Zwischensumme vor Rabatt (100) als Netto gelesen, USt fehlte im PDF → USt wurde als
   Brutto − Netto = 60 − 100 = −40 abgeleitet.
2. Speichern: Frontend und Backend prüften die PDF-Kopfsummen nur über Brutto - nach dem Löschen
   einer Position passte Brutto (60) weiter, Netto 100 / USt −40 wurden trotzdem übernommen.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.rechnungen import create_rechnung
from api.schemas_rechnungen import RechnungCreate, RechnungspositionCreate
from database.connection import Base
from database.models import Lieferant, Unternehmen
from utils.rechnungs_parser import _berechne_fehlende_summen


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parser_leitet_keine_negative_ust_aus_rabatt_zwischensumme_ab():
    felder = {"gesamt_netto": "100.00", "gesamt_brutto": "60.00"}
    _berechne_fehlende_summen(felder)
    assert "gesamt_ust" not in felder
    assert "gesamt_netto" not in felder  # unplausibles Netto verworfen
    assert felder["gesamt_brutto"] == "60.00"


def test_parser_normalfall_unveraendert():
    felder = {"gesamt_netto": "100.00", "gesamt_brutto": "119.00"}
    _berechne_fehlende_summen(felder)
    assert felder["gesamt_ust"] == "19.00"
    assert felder["ust_satz"] == "19"


def test_parser_ust_groesser_brutto_verworfen():
    felder = {"gesamt_ust": "80.00", "gesamt_brutto": "60.00"}
    _berechne_fehlende_summen(felder)
    assert "gesamt_netto" not in felder
    assert "gesamt_ust" not in felder


def test_parser_gutschrift_negativ_bleibt_erlaubt():
    felder = {"gesamt_netto": "-100.00", "gesamt_brutto": "-119.00"}
    _berechne_fehlende_summen(felder)
    assert felder["gesamt_ust"] == "-19.00"


# ---------------------------------------------------------------------------
# Speichern mit PDF-Kopfsummen (Overrides)
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Unternehmen(firmenname="Test", strasse="S", hausnummer="1", plz="12345", ort="O"))
    lieferant = Lieferant(firmenname="Online-Shop GmbH")
    session.add(lieferant)
    session.commit()
    session.refresh(lieferant)
    yield session, lieferant.id
    session.close()


def _eingang(lieferant_id, positionen, n, u, b):
    return RechnungCreate(
        typ="eingang", datum=date(2026, 10, 1), ist_entwurf=True, lieferant_id=lieferant_id,
        positionen=positionen,
        netto_gesamt_override=Decimal(n), ust_gesamt_override=Decimal(u), brutto_gesamt_override=Decimal(b),
    )


def test_fall_aus_issue_widerspruechlicher_kopf_wird_verworfen(db):
    session, lid = db
    # verbleibende Position nach dem Löschen: 60 € netto, 0 % USt
    pos = [RechnungspositionCreate(beschreibung="Artikel", menge=Decimal("1"), netto=Decimal("60.00"), ust_satz=Decimal("0"))]
    r = create_rechnung(_eingang(lid, pos, "100.00", "-40.00", "60.00"), session)
    assert (r.netto_gesamt, r.ust_gesamt, r.brutto_gesamt) == (Decimal("60.00"), Decimal("0.00"), Decimal("60.00"))


def test_stimmiger_kopf_mit_rundungsdifferenz_wird_uebernommen(db):
    # Lieferant rundet die USt auf Rechnungsebene: 3 × 0,99 netto → Positionsrundung 0,57 USt,
    # PDF weist 0,56 USt aus. Das soll weiterhin der PDF-Kopf sein (Zweck der Overrides).
    session, lid = db
    pos = [RechnungspositionCreate(beschreibung=f"P{i}", menge=Decimal("1"), netto=Decimal("0.99"), ust_satz=Decimal("19"))
           for i in range(3)]
    r = create_rechnung(_eingang(lid, pos, "2.97", "0.56", "3.53"), session)
    assert (r.netto_gesamt, r.ust_gesamt, r.brutto_gesamt) == (Decimal("2.97"), Decimal("0.56"), Decimal("3.53"))


def test_kopf_netto_passt_nicht_zu_positionen(db):
    # Brutto passt zufällig, Netto/USt nicht (andere Steueraufteilung) -> Positionen gelten
    session, lid = db
    pos = [RechnungspositionCreate(beschreibung="A", menge=Decimal("1"), netto=Decimal("119.00"), ust_satz=Decimal("0"))]
    r = create_rechnung(_eingang(lid, pos, "100.00", "19.00", "119.00"), session)
    assert (r.netto_gesamt, r.ust_gesamt, r.brutto_gesamt) == (Decimal("119.00"), Decimal("0.00"), Decimal("119.00"))
