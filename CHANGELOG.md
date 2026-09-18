# Changelog

Alle nennenswerten Aenderungen an diesem Projekt, neueste zuerst.
Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

Versionierung nach [SemVer](https://semver.org/lang/de/), Stand `0.4.1`
(`backend/pyproject.toml`). Das Projekt ist bewusst **vor 1.0**: es laeuft als
einzelne produktive Instanz und sagt keine stabile API zu. Solange die
Hauptversion 0 ist, traegt die zweite Stelle sowohl neue Funktionen als auch
Brueche; die dritte Stelle steht fuer Korrekturen, Doku und Aufraeumen ohne
neue Funktion.

Der oeffentliche Stand entsteht als Snapshot aus einem privaten Arbeits-Repo —
die Git-History hier zeigt daher nur den jeweils aktuellen Stand, nicht die
Entwicklung. Dieser CHANGELOG ist die Entwicklung.

---

## 0.4.1 — 2026-09-18

Nachzug zu 0.4.0 am selben Tag: keine neue Funktion, keine Verhaltensaenderung.

### Doku
- **Konfig-Referenz vollstaendig:** alle Felder einer Zone stehen jetzt mit
  Bedeutung und wirksamem Default in der README (vorher fehlten 30 von 72).
  Ein Test prueft die Klasse: kommt ein Zonen-Feld dazu, ohne dokumentiert
  zu sein, schlaegt er an.
- Die API-Tabelle nannte `POST /api/manuell/start`, eine Route, die es nicht
  gibt. Ersetzt durch die vier echten Ventil-Routen
  (`/api/ventil/manuell-start`, `-stop`, `/api/ventil/pre-soak-start`,
  `-stop`); Pre-Soak lief nie ueber die Manuell-Route. Auch hier prueft ein
  neuer Test die Klasse: jede Route, die die README nennt, muss in der App
  registriert sein.

### Removed
- Code der klassischen Uebersicht, deren Tab mit 0.4.0 entfallen war
  (Zonen-Karte, Status-Leiste und die nur von dort genutzten Komponenten),
  dazu zwei Komponenten, die schon laenger nirgends mehr eingebunden waren
  (Detail-Drawer, Schwellen-Anzeige). Zusammen 15 Dateien, rund 3300 Zeilen.
  Keine sichtbare Aenderung.

### Development
- Die Frontend-Pruefskripte liegen unter `frontend/tests/` und laufen damit
  auch im oeffentlichen Stand: `npm run test:overview`, `test:segmente`,
  `test:bilanz`, `test:giessfenster`.

---

## 0.4.0 — 2026-09-18

### Security
- js-yaml 4.3.1 -> 4.3.2 (Dependabot-Meldung, Schweregrad hoch). Wie beim
  letzten Mal nur die Build-Toolchain und nur das Lockfile; `npm audit`
  meldet danach 0 Schwachstellen.

### Added
- **Giessfenster aus der Verdunstung** (`giessfenster_et0`, pro Zone): statt
  fester Uhrzeiten eine weite Klammer, in der ein Start nur erlaubt ist, wenn
  danach genug Verdunstung zum Abtrocknen folgt und waehrenddessen nicht zu
  viel. Beides aus der stuendlichen ET0-Vorhersage. Modi `aus` / `schatten`
  (nur auswerten und loggen, zum Kalibrieren) / `aktiv`. Im Aktivmodus wird
  vorausschauend gegossen, bevor das Fenster zugeht.
- **Zentraler Hahn-Entscheider** fuer Zonen an einem geteilten Haupthahn:
  exklusive Zonen laufen wirklich allein; eine Pre-Soak-Sequenz behaelt den
  Hahn ueber ihre Einsickerpause, ausser eine Zone mit heute verfallendem
  Zeitfenster braucht ihn. Dazu eine **Wartezeit-Wache**
  (`watchdog.hahn_wache`), die meldet, wenn eine Zone zu lange wartet.
- **Randomisierter Dosis-Test** (`dosis_test`): misst an einem Ventil, ab
  welcher Laufzeit ein Sensor ueberhaupt reagiert (Stufen in zufaelliger,
  per Seed reproduzierbarer Reihenfolge).
- **Herkunft an jeder ML-Prognose**: auf welcher Messzeile und mit welchem
  physischen Geraet gerechnet wurde, plus `gueltig` / `ungueltig_grund` in
  der API. Die Karte ersetzt eine nicht belegbare Prognose durch
  `24h: veraltet` bzw. `24h: Sensortausch`, und das Guete-Urteil gilt fuer
  alle Konsumenten, auch fuer die Entscheidungs-Engine.
- **Sensor-Identitaet** (Geraetewechsel unter derselben ID) in ML-Features,
  Inferenz und Drift-Log; die Feuchte-Linie im Chart ist an der Grenze eines
  Kalibrierfensters unterbrochen statt einen Sensortausch als Trocknung zu
  zeichnen.
- **Testansicht "Übersicht (neu)"** am Ende der Tab-Leiste: kompakte
  Zonenliste mit Filtern und Detailbereich, auf dem Handy Drill-down. Die
  bisherige Übersicht bleibt Startansicht; der klassische Tab entfaellt.
- Warnungen: Dauerzustands-Warnungen gedrosselt, Ereignis-Warnungen laufen
  nach sieben Tagen ab, und die Pruefungen "Bewaesserung ohne Wirkung" bzw.
  "Sensor eingefroren" sind pro Zone abschaltbar (`wirkungs_alarm_aktiv`,
  `eingefroren_alarm_aktiv`) fuer Sensoren, bei denen sie strukturell falsch
  anschlagen.
- Der Feature-Bau fuer das Response-Modell laeuft wie der fuer die
  Feuchteprognose optional im Subprozess (`feature_bau_subprozess`).

### Changed
- **Lead-Sensor ausgefallen** ist ein eigener Zustand: das KRITISCH-Band
  meldet keinen Schwellenalarm mehr auf einem der uebrigen Sensoren, sondern
  nennt die tatsaechliche Quelle der angezeigten Zahl.
- Der Kontaktverlust-Guard (Messwert 0,0) urteilt ueber das Fenster-Maximum
  statt ueber den Vorgaengerwert, und das Frontend uebernimmt das Urteil des
  Backends, statt es nachzurechnen.
- Das Anzeige-Fenster fuer "aktuelle Feuchte" ist an den Entscheidungs-
  Horizont gekoppelt (4 h); aelter wird der Wert gedaempft mit Alter gezeigt.
- Plateau-Erkennung prueft die Richtung; die Aufloesungsgrenze folgt dem
  jeweiligen Geraet statt einer globalen Annahme.
- Cross-Spray wird nur noch als Ursache genannt, wenn es belegt ist.
- Der Replay-Schutz nach einem WebSocket-Reconnect prueft gegen den eigenen
  Ventilzustand statt gegen die Uhr (er hatte echte Schliess-Ereignisse
  langer Pulse verschluckt).

### Fixed
- Aus einem Code-Review: Doppelstart-Race und Persistenzfehler im
  Ventil-Schaltpfad, Reconnect-Sync erkennt auch unbekannte laufende
  Fremdguesse, Idempotenz und Trigger-Isolation der Jobs, ML-Deploy-Gate und
  Band-Kalibrierung, UTC-Datumsgrenzen.
- Nach einem Ruhezustand des Hosts konnte der eigene Ventilzustand als
  falscher Zeuge dienen; Laufdauern sind jetzt gedeckelt, Phantomzeilen
  werden bereinigt.
- Der Nachtrag aus der GARDENA-Historie erkennt spaete Live-Laeufe per
  Zeitintervall statt per Zeitpunkt und bricht nicht mehr ab.
- Ein Wetter-Ausfall sah im Tagesplan aus wie eine Trockenprognose; die
  Bilanz-Kachel nannte regelmaessig die falsche Ursache.
- Eine Pflanze ohne zugeordnetes Geraet gilt nicht mehr als Sensor-Ausfall.
- Der Status-Code eines abgelehnten Ventil-Kommandos bleibt erhalten.
- Zwei Konfig-Schluessel kamen nie aus der YAML an, es galt still der
  Default: `feature_bau_subprozess` und `plant_optimum_intervall_stunden`.
  Beide werden jetzt gelesen und sind per Test abgesichert.

### Doku & Entwicklung
- README: Tab-Beschreibung auf den aktuellen Stand, Konfig-Referenz fuer die
  neuen Schluessel. Zwei Eintraege nannten YAML-Bloecke, die der Parser gar
  nicht liest (`ml_bewaesserungs_response`, `bewaesserungs_response_retrain`);
  richtig ist `ml.bewaesserungs_response`.
- Beispiel-Konfig: auskommentierte Vorlagen fuer `giessfenster_et0`,
  `watchdog.hahn_wache`, `dosis_test` und `feature_bau_subprozess`.
- Die Test-Suite laeuft im oeffentlichen Stand vollstaendig durch: Tests,
  die die private Live-Konfig pruefen, tragen den Marker
  `@pytest.mark.live_config` und werden hier uebersprungen. Die Frontend-Logik
  der neuen Ansicht hat eigene Node-Tests (`npm run test:overview`).

---

## 0.3.0 — 2026-08-09

### Security
- Frontend-Abhaengigkeiten aktualisiert und damit zwoelf gemeldete
  Schwachstellen geschlossen (postcss, js-yaml, vite, brace-expansion,
  @babel/core). Alle betrafen ausschliesslich die **Build-Toolchain**
  (devDependencies) — die Laufzeit-Abhaengigkeiten waren nicht betroffen, und
  nichts davon war je Teil des ausgelieferten Bundles. Nur das Lockfile hat
  sich geaendert; die Versionsbereiche in `package.json` deckten die
  gepatchten Versionen bereits ab.

### Added
- **Ausfall-Erkennung pro Sensor-Geraet** statt pro Zone. Eine Zone mit mehreren
  Sensoren verdeckte bisher den Ausfall eines einzelnen Geraets, weil die
  juengste Messung der Zone als "frisch" zaehlte.
- **FYTA-Geraetestatus** (Batteriestand, Verbindung) wird ausgewertet und in der
  Sensor-Diagnose angezeigt.
- **Mehrmodell-Wettermitschnitt**: mehrere Vorhersagemodelle werden parallel
  protokolliert und gegen die spaeter gemessene Realitaet ausgewertet, um die
  Modellwahl zu belegen statt zu behaupten.
- **Eindeutigkeitsvertrag fuer die Messwert-Tabelle** (Datenbank-Constraint plus
  Migration) — verhindert Doppel-Eintraege aus ueberlappenden Nachhol-Laeufen.
- **Aufbewahrungsregel fuer Modellversionen**: alte Response-Modelle werden
  aufgeraeumt, aber Versionen, an denen eine getroffene Entscheidung haengt,
  sind vom Aufraeumen ausgenommen.
- ML-gestuetzte Dosisberechnung ist **pro Zone schaltbar** statt global.

### Changed
- **Wetter-Archiv auf ERA5 umgestellt.** Die zuvor genutzte Archivquelle
  erfand in einem relevanten Anteil der Faelle Regen, der nie fiel — mit
  entsprechend falschen Rueckblick-Bilanzen.
- Die Feldkapazitaets-Kalibrierung laeuft nur noch fuer Zonen, die ueberhaupt
  Regen abbekommen; ueberdachte und Topf-Zonen sind ausgenommen.
- Cross-Spray-Regel vereinheitlicht (eine Definition statt drei Fassungen an
  verschiedenen Stellen); pumpengespeiste Zonen fliessen in den Wirkungs-Fit ein.
- Die Faelligkeitspruefung des Retrainings nutzt eine Zaehlabfrage statt den
  vollen Feature-Aufbau — der Aufbau lief bisher **vor** dem Gate, das ihn
  meistens verworfen hat.
- Ensemble-Horizont von 48 h gestrichen: die Prognoseguete trug so weit nicht.
- Backups werden gzip-komprimiert und streamend geschrieben.
- Log-Rotation und ein Wartungs-Loop ergaenzt.

### Fixed
- **Rueckkopplungsschleife im Manuell-Giessen-Dialog**: unter Umstaenden 35–119
  Anfragen pro Sekunde gegen die eigene API, jetzt 0,3/s. Ursache war ein
  Effekt-Hook, der seine eigene Abhaengigkeit neu erzeugte.
- Poll-Zyklen im Frontend beenden sich jetzt auch im Fehlerfall sauber
  (`try/finally`), statt einen Timer zurueckzulassen.
- Der Aufbau der Response-Features blockierte den Event-Loop und liess damit
  Ventil-Callbacks warten.
- "Naechste N Stunden" zaehlte ab dem Listenanfang statt ab jetzt — bei einer
  aelteren Vorhersageliste wurden dadurch vergangene Stunden ausgewertet.
- Die Paarung verwaister Ventil-Ereignisse nutzt jetzt Zone **und** Ventil-ID;
  eine N+1-Abfrage dabei entfiel.
- Das Nachfuellen von Sensor-Luecken laeuft periodisch statt nur beim Neustart.
- Der Guard gegen Kontaktverlust (Messwert 0,0) entscheidet anhand der
  **Trajektorie** statt am Einzelwert — ein echter Trockenwert nahe null wurde
  sonst faelschlich als Sensordefekt verworfen.
- Ein leerer oder fehlgeschlagener Feature-Aufbau drosselt sich, statt in jedem
  Zyklus erneut vergeblich zu laufen.
- Fehlende Migration fuer die Ensemble-Spalten nachgetragen; ein API-Endpunkt
  lieferte faelschlich 404.

---

## 0.2.0 — 2026-07-27

### Added
- **Cycle-and-Soak (Mehrfach-Puls-Hauptdose).** Die berechnete Hauptdose kann in
  mehrere gleich lange Pulse mit Einsickerpausen aufgeteilt werden
  (`haupt_pulse`, `haupt_puls_pause_min`). Auf Sandboden laeuft eine lange Dose
  am Stueck oberflaechlich ab. Die Gesamt-Wassermenge bleibt unveraendert —
  die Aufteilung vervielfacht sie nicht.
- **Erkennung herstellerseitiger Kalibriersprunge** bei den Pflanzensensoren:
  faellt eine ganze Sensorgruppe im selben Messintervall, waehrend die
  unabhaengige Kontrollgruppe still steht, ist das kein Bodenereignis. Mit
  Meldung und optionaler Quarantaene der betroffenen Werte.
- **Regen-Ensemble und Radar-Nowcast** als Datenschicht (Shadow — protokolliert,
  entscheidet nichts).
- **Wasserbilanz nach FAO-56** als Shadow-Job, taeglich gegen die real
  getroffene Entscheidung gerechnet.
- Ausloeser-Klassen fuer Fremdwasser und Zeitplan: Wasser wird nach Quelle
  getrennt gefuehrt, statt alles als eigene Bewaesserung zu zaehlen.
- Warnung bei Erreichen des Tagesbudgets, mit eigener Schwelle.

### Changed
- **Regen-Gate wirkungsbasiert** statt an einer reinen Millimeter-Schwelle:
  entscheidend ist, was der Regen im Boden bewirkt, nicht die Menge.
- Der Watchdog unterscheidet einen toten Sensor von echtem Trockenstress und
  kennt die konfigurierten Ausnahme-Fenster.
- Die Alarmgrenze ist an die tatsaechliche Sensor-Aufloesung gekoppelt; darunter
  ist ein "Sprung" nicht unterscheidbar von Quantisierungsrauschen.

### Fixed
- Ein leeres Kanal-zu-Ventil-Mapping meldete **Erfolg statt Fehler** — die
  Bewaesserung galt als ausgefuehrt, ohne dass Wasser lief.
- Ein bereits offenes Ventil war fuer den Kritisch-Bypass unsichtbar, wodurch
  derselbe Kanal ein zweites Mal geoeffnet werden konnte.
- Die Zonen-Karte zeigte pauschal "Manuell" statt des echten Ausloesers.
- Das Frische-Fenster zaehlt ab Puls-Start statt ab Beginn der gesamten
  Vorwaesser-Sequenz.
- Ein Zweitsensor konnte einen laufenden Vorgang vorzeitig beenden.

---

## 0.1.0 — 2026-07-20 — Erstveroeffentlichung

Erster oeffentlicher Stand. Das Projekt lief zu diesem Zeitpunkt bereits
mehrere Monate produktiv; enthalten war im Wesentlichen:

### Added
- Dashboard fuer Bodenfeuchte aus zwei Sensor-Oekosystemen (GARDENA Smart
  System und FYTA) mit Multi-Standort-Wetter.
- **ML-Feuchteprognose** (Delta-Regressor mit Quantilbaendern q10/q50/q90) je
  Zone und Horizont, mit SHAP-Erklaerung der wichtigsten Einflussgroessen.
- **Kausale Giess-Empfehlung**: aus Sensorwert, Welkepunkt und Trocknungsverlauf
  wird eine begruendete Giessdauer mit Sicherheitsreserve abgeleitet — statt
  einer reinen Schwellenregel.
- Automatische Kalibrierung von Feldkapazitaet und Wirkungsrate aus der
  eigenen Messhistorie.
- Ventilsteuerung als dreistufiges Opt-in (global, pro Zone, pro Vorgang),
  Vorwaesserung gegen hydrophobe Substrate, Not-Stopp.
- Wasser-Bilanz, Leck-Erkennung, Ereignis-Nachtrag aus mehreren Quellen,
  Historie-Export, Wochenbericht.
- Watchdog mit Push-Benachrichtigung bei Anomalien; Health-Check fuer die
  genutzten inoffiziellen Schnittstellen.
- Automatisches woechentliches Nachtraining mit Deploy-Gate: ein neues Modell
  wird nur uebernommen, wenn es auf allen Horizonten besser abschneidet.
- Datenbank-Backup mit Rotation und optionalem Off-Host-Spiegel.
