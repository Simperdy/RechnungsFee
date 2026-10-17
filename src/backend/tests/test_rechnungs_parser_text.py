"""
Text-Parser (PDF ohne eingebettetes XML) - Regressionstests.

Issue #438: Telefonrechnung mit Rabatt-Position ("Rabatt ... - 21,01 €") wurde falsch erkannt:
Rabatt fehlte (Summenlabel + abgesetztes Minus), Netto/USt/Brutto wurden nicht gefunden
("Nettobetrag EUR …", "MwSt.-Betrag 19,00% von …", "Rechnungsbetrag in EUR inkl. MwSt. …"),
dadurch USt-Satz 0 % an den Positionen; Lieferantenname enthielt die ganze Absenderzeile,
USt-IdNr. mit Leerzeichen ("Ust.Id.: DE123 456 789") wurde nicht erkannt.

Die übrigen Tests sichern Formate ab, die bereits vorher funktionierten (laut Code-Kommentaren
Vodafone/Telekom), damit die erweiterten Muster sie nicht verändern. Alle Texte sind fiktiv.
"""
from decimal import Decimal

import pytest

from utils.rechnungs_parser import RechnungParser, _betrag_de, _extrahiere_positionen, _normalisiere_text


def _parse(text: str):
    return RechnungParser().parse(_normalisiere_text(text), [])


TELEFON_MIT_RABATT = """\
Musterfon GmbH - Musterweg 12 - 12345 Musterstadt
Rechnungsnummer: R-1000001
Rechnungsdatum: 15.10.2026
Telefon: 0123 | 45 67 89 Ust.Id.: DE123 456 789
Rechnung September 2026
Anzahl Menge bzw.
Verbindungen Leistungsbeschreibung Zeit in HH:MM:SS Nettobetrag
Vertragsnr: 4711
7,00 Zone MOBIL 00:12:10 0,00 €
Glasfaser 150 (v1) 1,00 37,81 €
Rabatt Glasfaser 150 (v1) 6M - 21,01 €
Nettobetrag EUR 16,80 €
MwSt.-Betrag 19,00% von 16,80 € 3,19 €
Rechnungsbetrag in EUR inkl. MwSt. 19,99 €
"""


def test_issue_438_summen():
    f = _parse(TELEFON_MIT_RABATT).felder
    assert (f["gesamt_netto"], f["gesamt_ust"], f["gesamt_brutto"]) == ("16.80", "3.19", "19.99")
    assert f["ust_satz"] == "19"


def test_issue_438_positionen_mit_rabatt():
    pos = _parse(TELEFON_MIT_RABATT).positionen
    assert [(p.netto, p.ust_satz) for p in pos] == [("37.81", "19"), ("-21.01", "19")]
    assert pos[1].beschreibung.startswith("Rabatt Glasfaser")
    assert sum(Decimal(p.netto) for p in pos) == Decimal("16.80")


def test_issue_438_lieferant():
    f = _parse(TELEFON_MIT_RABATT).felder
    assert f["lieferant_name"] == "Musterfon GmbH"
    assert (f["lieferant_strasse"], f["lieferant_plz"], f["lieferant_ort"]) == ("Musterweg 12", "12345", "Musterstadt")
    assert f["lieferant_ust_id"] == "DE123456789"


def test_betrag_mit_abgesetztem_minus_und_tausenderpunkt():
    assert _betrag_de("- 21,01") == "-21.01"
    assert _betrag_de("-1.234,56") == "-1234.56"
    assert _betrag_de("1.234,56") == "1234.56"
    assert _betrag_de("21,01 €") == "21.01"


# ---------------------------------------------------------------------------
# Schutztests: vorher schon funktionierende Formate bleiben unverändert
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rabattzeile", ["Rabatt: -5,00 €", "Rabatt 10 % -5,00 €", "Rabatt gesamt -5,00 €"])
def test_rabatt_summenzeile_bleibt_keine_position(rabattzeile):
    text = f"""\
Musterfon GmbH, Musterweg 12, 12345 Musterstadt
Rechnungsnummer: R-1
Rechnungsdatum: 15.10.2026
Verbindungen Leistungsbeschreibung Zeit in HH:MM:SS Nettobetrag
Glasfaser 150 (v1) 1,00 37,81 €
Telefonflat 1,00 10,00 €
{rabattzeile}
Nettobetrag EUR 42,81 €
"""
    pos = _extrahiere_positionen(_normalisiere_text(text), "19")
    assert [p.netto for p in pos] == ["37.81", "10.00"]


def test_gedankenstrich_als_trenner_wird_nicht_negativ():
    text = """\
Shop GmbH, Hauptstr. 1, 12345 Musterstadt
Rechnungsnummer: 43
Rechnungsdatum: 01.10.2026
Pos Beschreibung Menge Preis
Beratung - 80,00 €
Gesamtbetrag: 95,20 €
"""
    pos = _parse(text).positionen
    assert [p.netto for p in pos] == ["80.00"]


def test_vodafone_format():
    text = """\
Vodafone GmbH, Postfach 10 10 52, 40839 Ratingen
Rechnungsnummer: 123456789
Rechnungsdatum: 01.10.2026
MwSt. 19% 9,20
Rechnungsbetrag in EUR 57,60
"""
    f = _parse(text).felder
    assert f["lieferant_name"] == "Vodafone GmbH"
    assert f["gesamt_brutto"] == "57.60"
    assert f["gesamt_ust"] == "9.20"
    assert f["ust_satz"] == "19"


def test_telekom_format():
    text = """\
Telekom Deutschland GmbH, Landgrabenweg 151, 53227 Bonn
Rechnungsnummer: 987654321
Rechnungsdatum: 01.10.2026
Nettobetrag 59,95 €
19% USt. auf 59,95 € 11,39 €
Rechnungsbetrag 71,34 €
"""
    f = _parse(text).felder
    assert (f["gesamt_netto"], f["gesamt_ust"], f["gesamt_brutto"]) == ("59.95", "11.39", "71.34")
    assert f["ust_satz"] == "19"


def test_ust_idnr_klassische_schreibweise():
    text = "Firma GmbH, Weg 1, 12345 Ort\nUSt-IdNr.: DE123456789\nRechnungsnummer: 1\n"
    assert _parse(text).felder["lieferant_ust_id"] == "DE123456789"
