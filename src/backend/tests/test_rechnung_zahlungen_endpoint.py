"""
Issue #448: GET /rechnungen/{id}/zahlungen lieferte für jede Rechnung mit mindestens einer
Zahlung HTTP 500 - der Endpoint baute ZahlungKompakt ohne das Pflichtfeld beschreibung.
Liefert jetzt dieselbe Liste wie RechnungResponse.zahlungen, inkl. Storno-Kennzeichnung.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.rechnungen import get_rechnung_zahlungen, storno_rechnung
from api.schemas import StornoRequest
from database.connection import Base
from database.models import Journaleintrag, Rechnung


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _bezahlte_rechnung(db) -> Rechnung:
    rechnung = Rechnung(
        typ="ausgang", rechnungsnummer="RE-260001", datum=date(2026, 5, 1),
        brutto_gesamt=Decimal("119.00"), bezahlt=True, bezahlt_betrag=Decimal("119.00"),
        zahlungsstatus="bezahlt", ist_entwurf=False, immutable=True,
    )
    db.add(rechnung)
    db.flush()
    db.add(Journaleintrag(
        datum=date(2026, 5, 3), belegnr="J-260001", beschreibung="Zahlung RE-260001",
        zahlungsart="Bank", art="Einnahme",
        netto_betrag=Decimal("100.00"), ust_satz=Decimal("19"), ust_betrag=Decimal("19.00"),
        brutto_betrag=Decimal("119.00"), vorsteuerabzug=False,
        rechnung_id=rechnung.id, immutable=True,
    ))
    db.commit()
    return rechnung


def test_rechnung_mit_zahlung_liefert_liste(db):
    rechnung = _bezahlte_rechnung(db)

    zahlungen = get_rechnung_zahlungen(rechnung.id, db)

    assert len(zahlungen) == 1
    z = zahlungen[0]
    assert z.beschreibung == "Zahlung RE-260001"
    assert z.brutto_betrag == Decimal("119.00")
    assert z.rechnung_id == rechnung.id
    assert z.storniert is False and z.ist_storno is False


def test_rechnung_ohne_zahlung_liefert_leere_liste(db):
    rechnung = Rechnung(typ="ausgang", rechnungsnummer="RE-260002", datum=date(2026, 5, 1),
                        brutto_gesamt=Decimal("50.00"))
    db.add(rechnung)
    db.commit()

    assert get_rechnung_zahlungen(rechnung.id, db) == []


def test_storno_kennzeichnung_wie_in_rechnung_response(db):
    rechnung = _bezahlte_rechnung(db)
    storno_rechnung(rechnung.id, StornoRequest(grund="falsche Kategorie"), db)
    db.expire_all()

    zahlungen = get_rechnung_zahlungen(rechnung.id, db)

    original = [z for z in zahlungen if not z.ist_storno]
    gegen = [z for z in zahlungen if z.ist_storno]
    assert len(original) == 1 and original[0].storniert is True
    # Gegenbuchung haengt per gruppe_id an der Kette, nicht zwingend per rechnung_id
    assert all(z.beschreibung.startswith("STORNO ") for z in gegen)
