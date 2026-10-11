# Brainstorming: MCP-Server für RechnungsFee

**Issue:** #429
**Status:** Reines Brainstorming, keine Umsetzungsentscheidung. Melder (duczz/Martin) hat auf
Rückfrage einen konkreten, durchdachten Vorschlag nachgeliefert (siehe unten) – noch keine
Entscheidung, ob/wann umgesetzt wird.

---

## Was MCP ist (Kurzfassung)

Model Context Protocol (Anthropic, Nov. 2024 offengelegt, seitdem auch von OpenAI, Microsoft
u. a. übernommen – kein Anthropic-Exklusivprotokoll). Ein MCP-Server exponiert "Tools"
(Funktionen mit Schema), ein KI-Client (Claude Desktop, etc.) ruft sie auf Anfrage des Nutzers
auf. Rein anfragegetrieben – RechnungsFee würde nie selbst etwas an die KI senden.

Architektur für RechnungsFee: ein lokaler MCP-Server-Prozess, der mit dem bereits lokal
laufenden Backend (Port 8002) spricht. Kein Cloud-Dienst, kein RechnungsFee-Konto nötig – die
einzige "Account"-Frage betrifft den KI-Anbieter (Claude.ai-Abo, API-Key, o. ä.), nicht
RechnungsFee selbst.

---

## Datenschutz: lokal vs. Cloud-Modell

Das Protokoll selbst macht keine Aussage darüber, wohin Daten fließen – das entscheidet allein
das Modell hinter dem KI-Client:

| Modell-Wahl | Datenfluss |
|---|---|
| Cloud (Claude, GPT, Gemini, …) | Von einem Tool-Aufruf zurückgegebene RechnungsFee-Daten (z. B. Rechnungsinhalte für eine Auswertung) werden Teil der Konversation beim jeweiligen Anbieter – wie Copy-Paste in den Chat. |
| Cloud, EU-Anbieter (z. B. Mistral) | Unterliegt direkt der DSGVO ohne Drittland-Problematik (Schrems II/US CLOUD Act) – datenschutzrechtlich klar besser als US-Anbieter, aber immer noch ein Drittanbieter-Vertrauensverhältnis. |
| Lokales Modell (Ollama, LM Studio, …) | Nichts verlässt den Rechner. Einzige Option ganz ohne Drittanbieter-Vertrauen. |

RechnungsFee ist bisher komplett lokal (SQLite pro Profil, kein eigener Server) – MCP wäre die
**erste** Funktion mit potenziellem Cloud-Datenfluss, aber nur als bewusste Opt-in-Entscheidung
des Nutzers (Wahl von Client/Modell liegt bei ihm). Falls umgesetzt: lokales Modell als
empfohlener Default, Cloud-Anbieter klar als Opt-in kennzeichnen.

---

## Wie groß müsste ein lokales Modell sein?

Keine gemessenen Benchmark-Zahlen dazu, sondern eine Einschätzung aus bekannten
Function-Calling-Mustern (z. B. Berkeley Function-Calling Leaderboard-Trends) – mit
entsprechendem Vorbehalt.

**Wichtiger als die Parameterzahl: wie viele Tools, wie gut deren Schema/Namen sind, und ob es
sich um Lese- oder Schreib-Aktionen handelt.**

### Faustregel nach Einsatzzweck

| Zweck | Risiko bei Fehler | Realistische Modellgröße | Hardware |
|---|---|---|---|
| **Auswertung/Statistik** (nur Lesen, z. B. "Umsatz pro Monat") | Gering – falsche Zusammenfassung ist gegen die echten Zahlen prüfbar | ~7–14 B (z. B. Qwen2.5-14B, Mistral-NeMo-12B), mit Function-Calling-Finetune | 8–16 GB VRAM, läuft auch brauchbar auf CPU |
| **Schreib-Aktionen** (Rechnung buchen, Zahlung erfassen, stornieren) | Hoch – falscher Betrag/falsche Rechnung hat echte Buchhaltungs-/GoBD-Folgen | Eher 24–32 B+ (z. B. Mistral-Small-24B, Qwen2.5-32B) **und/oder** ein Bestätigungsschritt im Tool-Design selbst (Vorschau statt Direktausführung) | 24 GB+ VRAM (z. B. eine RTX 3090/4090) |
| Komplexe Mehrschritt-Aktionen (mehrere verknüpfte Buchungen) | Hoch | 70 B+ nähert sich Cloud-Qualität an | 48 GB+ VRAM oder starke Quantisierung – für die meisten Freiberufler-Rechner nicht realistisch |

### Wichtigster Hebel: Tool-Design, nicht nur Modellgröße

RechnungsFees Backend hat Dutzende Endpunkte (Rechnungen, Journal, Kunden, UStVA, Mahnwesen,
Bank-Import, …). Diese 1:1 als MCP-Tools zu exponieren würde selbst ein großes Modell bei der
Tool-Auswahl überfordern. Sinnvoller: eine kleine Zahl kuratierter, hochrangiger Tools (z. B.
`suche_kunde`, `liste_offene_rechnungen`, `erstelle_rechnung`, `buche_zahlung`,
`auswertung_zeitraum`) statt aller Rohendpunkte. Das senkt die nötige Modellgröße deutlich und
ist unabhängig von Hardware-Fragen umsetzbar.

**Fazit:** Für den von der Nutzerin genannten Anwendungsfall ("Auswertungen und Statistik")
ist ein handhabbares lokales Modell (14B-Klasse) schon heute plausibel. Für Schreibaktionen mit
echten Buchhaltungsfolgen würde ich zusätzlich zu einem größeren Modell eine
Bestätigungs-/Vorschau-Stufe im Tool selbst einplanen – unabhängig davon, wie groß das Modell
ist.

---

## Konkreter Vorschlag von Martin (duczz), Issue #429, 2026-10-11

### Human-in-the-Loop als Grundprinzip

Kein MCP-Tool finalisiert jemals direkt – Schreibaktionen legen immer nur einen **Entwurf**
an (`ist_entwurf=True`), die eigentliche Finalisierung bleibt ein expliziter Klick in der App.
Wichtige Erkenntnis: das ist **kein neuer Sicherheitsmechanismus**, den wir bauen müssten –
`ist_entwurf` existiert bereits als zentrales Konzept (Angebot, Rechnung, etc.) und
Finalisieren ist schon heute ein separater UI-Schritt. Ein MCP-Tool würde einfach denselben
Pfad nutzen, den das Frontend auch nimmt. GoBD-Integrität bleibt dadurch automatisch gewahrt.

### Anwendungsfälle

**A. Read-Only / Auswertungen (Zero-Risk, passt zu kleinen lokalen Modellen):**
- "Welche Rechnungen sind überfällig, wer muss gemahnt werden?"
- "Zeig mir Kontaktdaten und letzte 3 Rechnungen von Kunde XY."
- "Wie viel Umsatz in Q1, wie nah an der Kleinunternehmergrenze (§19)?"

**B. Entwürfe (Human-in-the-Loop):**
- Rechnungsentwurf per Prompt erstellen lassen.
- Eingangsrechnung/Beleg-PDF der KI geben → sie liest Rechnungsnummer/Lieferant/Betrag aus →
  legt den Beleg als Entwurf an.
- Kunde anlegen/abfragen.

### Vorgeschlagener Stufenplan

- **Phase 1 (MVP, Read-Only):** `liste_offene_rechnungen(status, faellig_filter)`,
  `suche_kunde(query)`/`zeige_kundenhistorie(kunde_id)`, `finanz_uebersicht(zeitraum)`. Mappt
  direkt auf bestehende Endpunkte (`get_offene_rechnungen`, Kundensuche, `api/cockpit.py`) –
  kein neuer Code in der Kernlogik, nur dünne Wrapper.
- **Phase 2 (Draft Creation):** `erstelle_rechnungsentwurf(...)`, `erstelle_kunde(...)`.

### Technischer Vorschlag (ursprünglich, 2026-10-11)

Schlanker, eigenständiger Python-Prozess mit `fastmcp`, spricht per HTTP nur mit dem bereits
laufenden Backend (`localhost:8002`) – kein Eingriff in die Kernlogik, keine
Datenbankänderungen an der Anwendung vorbei.

---

## Martins Antwort auf die Rückfragen, 2026-10-11

Verfeinert den obigen Vorschlag – **direkte Einbettung statt separates Paket**:

- MCP läuft **im selben Backend-Prozess** wie RechnungsFee selbst (startet/stoppt mit der App),
  kein eigener Hintergrundprozess für die erste Version. Ein eigenständiger Dienst wäre erst
  nötig, falls RechnungsFee irgendwann als dauerhaft laufende Webanwendung betrieben wird –
  dann könnte MCP vom selben Server mitlaufen.
- **Standardmäßig deaktiviert**, Toggle + abgestufte Berechtigungen (zunächst nur Lesezugriff
  auf Rechnungen/Kunden/offene Posten/Auswertungen, später optional Entwürfe/Schreibaktionen)
  in den Einstellungen.
- **Sicherheit:** Ist der Schalter aus, darf der Endpunkt technisch nicht erreichbar sein.
  Zugriffe müssen authentifiziert sein – RechnungsFee hat aktuell **keinerlei Auth** (reine
  lokale Desktop-App ohne Login), das wäre also ein neues Konzept. Ein einmalig generiertes,
  lokales Shared-Secret-Token (angezeigt in den Einstellungen, in die MCP-Client-Config
  eingetragen) reicht für den rein lokalen Kontext – keine volle OAuth-Infrastruktur nötig.
- **Mehrprofil:** Zielprofil sollte konfigurierbar sein; nutzt es das aktuell aktive Profil,
  muss ein Profilwechsel zuverlässig korrekt durchgereicht werden (keine stille
  Verbindung zur falschen DB).
- **Nachfrage:** Martin räumt offen ein, keine validierte Nachfrage über sich selbst hinaus zu
  kennen – reine Zukunftswette auf KI-gestützte Software-Bedienung, kein dringender Bedarf.
  Schlägt selbst einen kleinen, optionalen ersten Schritt vor, um das echte Interesse später
  besser einschätzen zu können.

**Einschätzung:** Die Einbettung direkt ins laufende Backend (statt separates Zusatzpaket) ist
architektonisch sauberer als der ursprüngliche Vorschlag – einfachere Lebenszyklus-Verwaltung,
kein zusätzlicher Installationsschritt für Nutzer. Wirft aber die Auth-Frage neu auf (siehe
oben), die bisher nirgends in RechnungsFee existiert.

---

## Größen-/Ressourcen-Analyse, 2026-10-11

Reale Messung (nicht geschätzt): `pip install` in einer Scratch-venv, abgeglichen gegen das,
was RechnungsFee über FastAPI ohnehin schon bündelt (`pydantic`, `starlette`, `anyio`,
`uvicorn`, `cryptography`, `python-multipart` sind bereits Pflicht-Abhängigkeiten).

| Variante | Neue Paketgröße (unkomprimiert) | Installer-Zuwachs (komprimiert, geschätzt) |
|---|---|---|
| Schlankes, offizielles `mcp`-SDK | ~9–11 MB | ~3–6 MB |
| `fastmcp`-Wrapper (Komfort-Framework) | ~30–50+ MB | entsprechend mehr |

`fastmcp` bringt viel CLI-/Dev-Tooling mit, das im eingebetteten Betrieb nie gebraucht wird
(`rich`, `pygments`, `cyclopts`, `beartype`, `watchfiles`, voller OAuth-Stack) – für eine
Einbettung ist das schlanke `mcp`-SDK die richtige Wahl, nicht `fastmcp`. Zum Vergleich:
`cryptography` allein (schon heute Pflicht-Abhängigkeit) wiegt mit 16 MB mehr als das
komplette zusätzliche `mcp`-SDK.

**Laufzeit-Ressourcen (wichtiger als Downloadgröße für schwache Laptops):**
- **Deaktiviert:** Pflichtanforderung an die Umsetzung, nicht nur Nice-to-have – das
  MCP-Modul darf beim Start gar nicht importiert/geladen werden, wenn der Schalter aus ist.
  Dann buchstäblich 0 zusätzlicher RAM-/CPU-Verbrauch.
- **Aktiviert:** Kein lokales KI-Modell läuft in RechnungsFee selbst, das läuft extern (Claude
  Desktop o. ä.) – RechnungsFee liefert nur Daten auf Anfrage. Mehrverbrauch vergleichbar mit
  ein paar zusätzlichen HTTP-Routen, niedriger einstelliger MB-RAM-Bereich, keine spürbare
  CPU-Last im Leerlauf.

---

## Offene Fragen (vor einer Umsetzungsentscheidung zu klären)

- **Auth-Konzept:** RechnungsFee hat aktuell keinerlei Authentifizierung – ein lokales
  Shared-Secret-Token (Martins Vorschlag) wäre der erste Auth-Mechanismus überhaupt in der App.
- **Mehrprofil:** Zielprofil konfigurierbar machen, oder aktives Profil mit zuverlässiger
  Wechsel-Erkennung nutzen?
- **Tool-Scope Phase 1:** Nur Lesen (Auswertungen, offene Rechnungen, Kundenhistorie) als
  erster, risikoärmerer Schritt – Entwürfe/Schreibaktionen explizit erst später.
- **Zielgruppe/Nachfrage:** Beide Beteiligten (Martin, Peter1061 implizit über #428s
  Detailgrad) sind technisch versierte Einzelnutzer – keine validierte breitere Nachfrage.
  Bleibt vorerst in der Roadmap-Ideenliste ohne festen Zeitplan, bis sich das ändert.

**Querverweis (Nutzerin, 2026-10-11):** Die bereits auf der Roadmap stehende "Docker-Version"
(containerisiertes Deployment für Selbst-Hoster, Backend+Frontend als Docker-Image) ist genau
das Szenario, das Martin mit "falls RechnungsFee mal als Webanwendung läuft" meint – dort gäbe
es bereits einen dauerhaft laufenden Server-Prozess, auf dem MCP ohne jeden zusätzlichen
Hintergrunddienst mitlaufen könnte. Beide Ideen passen architektonisch zusammen, auch wenn
keine von beiden aktuell terminiert ist.
