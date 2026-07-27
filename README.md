# RootCause

**Causal, predictive watering for GARDENA & FYTA.** — *Know why. Water right.*

Ein Dashboard, das **vorhersagt**, wann Pflanzen Wasser brauchen — und **wie viel** —
statt nach starrem Zeitplan zu giessen. Es liest Bodenfeuchte (GARDENA + FYTA) und
Wetterdaten, prognostiziert den Feuchteverlauf per ML und leitet daraus eine
begruendete Giessdauer mit Sicherheitsreserve ab. Laeuft produktiv am Garten des
Autors.

*(Deutschsprachiges Projekt; englische Kurzfassung oben in der Tagline.)*

## Was es kann

- **ML-Feuchteprognose** — Delta-Regressor mit Quantil-Baendern (q10/q50/q90) fuer
  6h/12h/24h, SHAP-Erklaerbarkeit pro Zone.
- **Kausale Giess-Empfehlung** — nicht "unter Schwelle = giessen", sondern
  Welkepunkt + Trocknungsrate -> Reserve-Tage -> konkrete Giessdauer.
- **Automatische Kalibrierung** — Feldkapazitaet und Wirkungsrate (pp/min) je Zone
  aus der eigenen Historie, statt geratener Konstanten.
- **Bewaesserung ausfuehren** — automatisch (Opt-in pro Zone) oder manuell,
  inkl. Pre-Soak gegen hydrophobe Substrate.
- **Sicherheit** — Watchdog, Notfall-Stopp, Tages-Budget, Regen-Guard,
  Leck-Detektion.
- **Multi-Standort** — Garten, Balkon, Indoor; Regner, Ventile, Mikrodrip,
  Solar-Pumpe, Giesskanne.
- **Betrieb** — Wasser-Bilanz, CSV-Export, Wochen-Report, Push bei Anomalien,
  Auto-Retrain mit Deploy-Gate.

## Voraussetzungen

- **GARDENA Smart System** mit Developer-API-Zugang (Client-ID + Secret) — Pflicht.
- **FYTA-Pflanzensensoren** — optional, fuer Topf- und Indoor-Zonen.
- Python 3.10+, Node.js (fuer den Frontend-Build).
- Der Hintergrund-Service nutzt `launchd` und ist damit auf **macOS** zugeschnitten;
  das Backend selbst laeuft plattformunabhaengig.

## Status & Grenzen

Ehrlich vorab, damit die Erwartung stimmt:

- **Persoenliches Produktivsystem, kein Produkt.** Gewachsen an einem konkreten
  Garten — nicht als generische Loesung getestet.
- Der Giess-Historie-Backfill nutzt einen **inoffiziellen GARDENA-Endpoint**, naemlich
  die Customer-API hinter der GARDENA-App. Zugegriffen wird ausschliesslich auf die
  **eigenen Kontodaten mit den eigenen Zugangsdaten**, die man selbst in der `.env`
  hinterlegt. Das Repo enthaelt keine Credentials und umgeht keine Zugangskontrolle.
  Der Endpoint ist nicht dokumentiert und kann sich jederzeit aendern, dafuer gibt es
  einen Endpoint-Health-Check.
- Prognosen sind **probabilistisch**. Die Welkepunkt-Reserve ist ein
  Sicherheitspuffer, keine Garantie gegen Trockenstress.
- Doku, Code und Konfig-Schluessel sind **deutsch**.

## Inhalt

[Quickstart](#quickstart) · [Nutzung](#nutzung) · [Konfiguration](#konfiguration) ·
[Troubleshooting](#troubleshooting) · [Development](#development) ·
[Architektur](#architektur)

## Quickstart

```bash
# Einmalig: Abhaengigkeiten installieren
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cd frontend && npm install && cd ..

# Konfig anlegen (Vorlage: config/default.example.yaml)
cp config/default.example.yaml config/default.yaml
# Standorte, Zonen, Geo-Koordinaten und Sensor-Zuordnung an eigenes Setup anpassen.

# .env anlegen (Vorlage: .env.example)
cp .env.example .env
# Pflicht: GARDENA_CLIENT_ID, GARDENA_CLIENT_SECRET (Developer-API)
# Empfohlen: GARDENA_CUSTOMER_EMAIL, GARDENA_CUSTOMER_PASSWORD (fuer DHS-Backfill)
# Empfohlen: FYTA_ACCESS_TOKEN (fuer FYTA-Pflanzensensoren)

# Starten
./start.sh              # Interaktiv mit Browser
./start.sh --build      # Frontend neu bauen und interaktiv starten
./start.sh --dev        # Backend + Vite Dev-Server

# Service-Betrieb im Hintergrund
./service.sh install            # Service installieren und starten
./service.sh status             # Service/API pruefen
./service.sh logs               # Logs live ansehen
./service.sh restart            # Nach Backend-Aenderungen
./service.sh restart --build    # Nach Frontend-Aenderungen
./service.sh stop               # Service stoppen

# Logs auch per Finder-Doppelklick auf logs.command (statt ./service.sh logs)
```

Dashboard: http://127.0.0.1:8090

## Nutzung

### Dashboard-Tabs

**Uebersicht**
- **StatusLeiste** (oben): alle Zonen nach OK / Giessen bald / Kritisch / Nass. Klick springt zur Zone.
- **ZonenKarten**: pro Zone Feuchte-Wert + Historie-Chart (24h/48h/7d/30d). Jede Karte enthaelt:
  - **"Warum"-Panel**: Status der letzten Entscheidung inline + Drawer mit Historie und Begruendungen (z.B. "Feuchte ausreichend", "Regen erwartet").
  - **Schwellen-Vorschlag**: kleine 💡-Zeile "Vorschlag X/Y % (aktuell X/Y %, Pflanze A-B %)", wenn aus 30 Tagen Historie + Pflanzen-Optimum ein abweichender Wert ableitbar ist. Rein informativ — deine Schwellen bleiben unveraendert, bis du sie selbst anpasst. Tooltip erklaert Quelle (empirisch vs. optimum-dominiert).
  - **Wasser-Bilanz**: Zugefuehrt / Verdunstet / Bilanz in Litern, umschaltbar 24h/7d/30d, mit Quellenanzeige (Archiv / Forecast / gemischt; "indikativ"-Badge bei Forecast).
  - **ML-Kurzinfo**: Feuchte-Vorhersage fuer 6h/12h/24h. Tooltip zeigt q10–q90-Spanne.
  - **"Erklaerung (Top-5 Features)"**: aufklappbar pro Zone. Zeigt SHAP-Beitraege der wichtigsten Features pro Horizont als farbige Balken (gruen = treibt Prognose hoch, orange = runter). Laedt on-demand — das Dashboard-Polling bleibt schlank.
  - **Chart-Band**: Recharts-Area zwischen q10 und q90 unter der ML-Prognose-Linie zeigt die Unsicherheit visuell — enges Band = sicher, breites Band = nur Trend.
  - **Manuell-Giessen**: Button fuer Schlauch-Bewaesserung (Dauer + optional Liter + optional Zeitstempel).
- **Wetterkarte**: Temperatur + Niederschlag pro Standort.

**Ops**
- **KPI-Zeile**: Shadow-Empfehlungen, Bewaesserungen heute, Blocker, Wetter-/Sensor-Warnungen.
- **Tag-Events**: alle Ventil-Events heute mit Quelle (Live / Gardena-App / manuell / Sensor-Heuristik) und Ausloeser-Badge. Ungeklaerte Heuristik-Events koennen per Klick klassifiziert oder geloescht werden.
- **Timeline**: Entscheidungen, Wetter-Events, Sensor-Warnungen chronologisch.
- **Notfall-Stopp**: roter Button im Header, wenn ein Ventil aktiv ist.

**Historie**
- Zeitraum-Filter (24h/7d/30d), Zone, Blocker-Typ, Limit 100-5000.
- Tabelle aller Entscheidungen mit Zeit, Zone, Scope, Aktion, Dauer, Blocker, Begruendung.
- **"Als CSV exportieren"**: Download der aktuellen Filter-Auswahl als CSV (fuer Excel / Pandas / Langzeit-Auswertung).

**Gieß-Historie**
- Pro Zone: was das System **getan** hat (im Unterschied zum Entscheidungs-Log der Historie) — wann, wie lange, **wie** (Einzel-Lauf vs. Pre-Soak inkl. Phasen Vorwaessern/Hauptdose), Ausloeser (Automatik/Manuell).
- Pre-Soaks (Puls + Pause + Hauptdose) erscheinen als **ein** Lauf (Backend-Marker `lauf_gruppe`, keine zeitliche Heuristik). Ignorierte / Cross-Spray-Laeufe sind ausgegraut (zaehlen nicht als echte Zonen-Bewaesserung).
- Zonen-Auswahl + 7/14/30-Tage-Fenster.

**Uebersicht v2**

Drei-Schichten-Refactor der Zonen-Karte als **additiver Tab** neben
"Uebersicht" und "Uebersicht (neu)". Bestehende Tabs bleiben byte-stabil
— Rollback durch Tab-Wechsel, keine Datei-Tausch-Operation noetig.

Jede Karte hat drei Schichten:

1. **Glance** (Sekunden-Blick): Header mit Zonen-Name, `KANAL X ·
   STRATEGIE` als Sub-Titel, Badges (AUTOMATIK / Sensor-Warnungen /
   Quellen-Marker `G`+`F`), grosse Feuchte-Prozent rechts mit
   `→ Trend pp/h`, Schwellen-Bar mit Welkepunkt-Marker + Korridor +
   Optimum-Band + Sub-Sensor-Markern + Ist-Punkt.
2. **Action** (Was-tun-Schicht): zeigt aktuelle Empfehlung mit
   strukturierter Detail-Zeile `praeventiv: 22 min · ML: 41 min ·
   Rate 0.13 pp/min` und Reserve-Tag `Wohl-Min in ~14h`. Lauf-Banner
   fuer aktive Bewaesserung dimmt die Empfehlung automatisch
   (Hinweis "MANUELL AKTIV — EMPFEHLUNG PAUSIERT"). Stop-Button
   in derselben Box.
3. **Inspect** (Tief-Dive, auf-/zuklappbar): Feuchte-Verlauf-Chart
   mit ML-Prognose-Band (24h/48h/7d/30d toggle), Modell-Drift-Ampel
   mit Ratio inline (`6h 3.9 pp 1.9x` macht Schwellen-Logik direkt
   sichtbar), FYTA-KPI-Block (Feuchte/Licht/Temperatur/Salz mit
   Optimum-Baendern), Wasser-Bilanz (24h/7d/30d), Top-5-ML-Features
   ("Erklaerung"-Toggle), Multi-Sensor-Diagnose-Tabelle (nur bei
   Multi-Sensor-Zonen wie Waldblumenhain).
   Expand-State wird pro Zone persistiert (`localStorage:
   zk-v2-expanded-{zone_id}`).

**Sortierung pro Standort**: `kritisch → giessen → warn → ok`. Bei
Gleichstand niedrigste Feuchte zuerst. Outdoor-Reihenfolge fest
: Bambus → Waldblumen → Magerwiese → Hecke.

**Lauf-Banner mit Optimistic-Update**: nach `Live starten` /
`Stop`-Klick erscheint der Banner sofort (kein 5-30 s-Polling-Lag),
weil der Frontend-State direkt nach erfolgreichem POST gesetzt
wird; Polling synchronisiert sich beim naechsten Cycle.

**Performance**: ein Bulk-Endpoint `/api/dashboard-snapshot`
ersetzt pro Karte 3 Per-Karte-Polls (Messwerte + ML + Empfehlung).
Beim Tab-Wechsel 1 HTTP-Call statt 42, Polling-Cadence 60 s. Inspect-
Sub-Komponenten (Recharts, FYTA-KPI, ML-SHAP, Wasser-Bilanz, Drift-
Ampel) sind via `React.lazy` aus dem Initial-Bundle (~200 KB gzip)
heraus — laden erst beim Aufklappen.

### Kausale Giess-Empfehlung

Statt nur "X % unter Schwelle = bewaessern" liefert das System eine kausale Erzaehlung: aktueller Sensor + Welkepunkt + Decay (ET0+Regen) -> Reserve-Tage -> Empfehlungs-Typ + konkrete Dauer. Sichtbar in jeder ZonenKarte:

> "Sensor 55 %, Welkepunkt 50 %. Ohne Giessen morgen frueh 50 %, uebermorgen 45 %. 12 min jetzt -> deckt 3 Tage."

Vier Empfehlungs-Typen: `akut` (Welke <= 1.5 Tage), `praeventiv` (Reserve unter `sicherheits_tage`), `wohlfuehl_grenze` (Sensor unter `optimum_min`, sanfter Hinweis), `kein_bedarf`. Bei laengerer Abwesenheit via `?sicherheits_tage=7` hochsetzen.

**Vier Bewaesserungs-Strategien pro Zone** — `bewaesserungs_strategie: korridor | haeufig_klein | selten_gross | konstant_niedrig`:
- **korridor** (Default): Welkepunkt-basiert + sanfter `wohlfuehl_grenze`-Hinweis, backwards-kompatibel.
- **haeufig_klein**: Wohlfuehl-Min als primaerer Trigger, oft + wenig (Topfpflanzen, frisch gesetzte Stauden).
- **selten_gross**: nur `akut` oder `kein_bedarf`, Ziel = Feldkapazitaet (Tiefwurzler wie Bambus, etablierte Buesche).
- **konstant_niedrig**: Trigger nur bei Welkepunkt + 5 pp Reserve (Sukkulenten, Magerwiese).

**Mehrfach-Takt**: bei `max_dauer_sekunden` ausgereizt + weiterhin Bedarf empfiehlt das System "X min jetzt + Y min in Z h" als zwei Dosen statt eines unrealistisch langen Stosses.

**Saisonale Feuchte-Regimes**: pro Zone optionale Regime-Liste mit MM-DD-Range. Aktives Regime ueberschreibt `optimum_*`, `feuchte_schwelle_*`, `feuchte_kritisch`, `welkepunkt`. Sinnvoll fuer Magerwiese-Sommer-Trockenphase, Anwachs-Phase frischer Pflanzungen, Sukkulenten-Winterruhe.

**ML-Vergleich strategie-aware**: das ML-Modell rechnet auf das gleiche Ziel wie das kausale Modell — bei `haeufig_klein` auf `optimum_max`, bei `selten_gross` auf Feldkapazitaet, bei `korridor` auf `feuchte_schwelle_min`. Vorher kapitulierte ML mit "sensor_ueber_ziel" sobald der Sensor genau an `feuchte_schwelle_min` lag, obwohl das kausale Modell weiter bewaessern wuerde. Zusatz: `ml_status_grund` im API-Response erklaert dem Frontend in Klartext, warum kein ML-Wert da ist (`sensor_ueber_ziel` / `kein_bedarf` / `modell_fehlt` / `inverse_kein_wert` / `inferenz_fehler` / `konfig_aus` / `service_aus` / `keine_messung` / `zone_monitoring` / `zone_unbekannt` / `ok`). Das Dashboard zeigt entweder den Vergleichswert oder einen klaren Hinweis-Block ("kein Vorschlag (Sensor schon am Ziel)"), keine stille Luecke mehr.

### Manuelle Bewaesserung mit Pre-Soak

Live-Manuell-Bewaesserung direkt ueber das Dashboard. Optional mit Pre-Soak-Sequenz: kurze Vorwaesserung (z. B. 5 min) -> Pause (z. B. 30 min, lasst das Wasser einsickern) -> Hauptdose (z. B. 60 min, auf Wunsch als Cycle-and-Soak in mehrere Pulse aufgeteilt, s. u.). Nuetzlich auf Sandboden, der bei direkter Volldosis zu Tiefensickerung neigt. Stop-Button auch fuer extern (Gardena-App) gestartete Bewaesserungen. Pre-Soak-Sequenzen ueberleben Backend-Restart. Default-Werte fuer Vor-/Pause-Dauer kommen aus `pre_soak_min`/`pre_soak_pause_min` der Zone.

### Liter-basiertes Manuell-Logging

Pro Zone konfigurierbar, ob der "Gegossen"-Button Sekunden- oder Liter-/ml-Optionen zeigt. Default `sekunden` (Schlauch); fuer FYTA-Topfpflanzen mit Giesskanne ist `ml` intuitiver:

```yaml
- zone_id: zitrus
  logging_einheit: ml
  logging_optionen_ml: [100, 250, 500, 1000]
```

Backend rechnet aus `liter` eine Pseudo-`dauer_sekunden` (mind. 1 s, damit kleine Dosen wie 50 ml nicht aus dauer-basierten ML-Features fallen) und schreibt beides ins `ventil_ereignis`. Bilanz nutzt das `liter`-Feld kanonisch.

### AquaBloom-Auto-Logging

Fuer FYTA-Topfpflanzen mit Gardena-AquaBloom-Solar-Pumpe (kein Smart-Ventil, also keine WebSocket-Events) schreibt das Backend synthetische Bewaesserungs-Events in `ventil_ereignis`, basierend auf der eingestellten Pumpen-Frequenz und -Dauer. Das verschafft ML, Bilanz und Wirkungsrate echte Datenpunkte ohne taegliches Mitloggen.

**Konfig pro Zone** (Beispiel `zitrus` + `kasten_4`):

```yaml
aquabloom_pumpen_dauer_sekunden: 600          # 10 min pro Puls
aquabloom_pumpen_intervall_stunden: 48        # alle 48 h
aquabloom_anker_zeitstempel: "2026-05-09T08:00:00"
aquabloom_tropfer_anzahl: 1                   # Tropfer in dieser Zone
aquabloom_tropfer_liter_pro_stunde: 0.5       # Gardena-Spec
aquabloom_aktiv_ab: "05-01"
aquabloom_aktiv_bis: "10-01"
```

**Modell**: `liter = dauer_s/3600 × tropfer_anzahl × tropfer_l_h` pro Puls (1 Tropfer × 0.5 L/h × 600 s ≈ 0.083 L). Job laeuft 1×/h, holt fehlende Pulse seit dem letzten DB-Eintrag bzw. Konfig-Anker (`max(letzter_db, konfig)` — bei Frequenzwechsel den Anker auf den letzten realen Pumpenpuls setzen). ML-Behandlung: Wirkungsrate-Median ignoriert AquaBloom (zu kleine Pulse), Response-Features nehmen nur Rows mit `delta_6h > 1.0` ins Training (sonst dominieren delta≈0-Datenpunkte das Modell).

### Wirkungsrate-Auto-Kalibrierung

Pro Zone wird die echte Wirkungsrate (`delta_pp / dauer_min`) automatisch aus historischen SCHLIESSEN-Events gelernt — kein Hardcode mehr. 5-stufige Aufloesungs-Kette: manueller Konfig-Override -> Kalibrierungs-Median (n>=3 in 90 Tagen) -> Default 1.0. Plus log-Decay-Korrektur fuer Tiefensickerung bei langen Dosen (Bambus-Mikrodrip alpha~ -0.6, Sprinkler-Sand schwaecher) und Plateau-Modell fuer Saettigungs-Effekte. Garbage-Filter verhindert dass Stale-CLOSED-Phantom-Dauer den Median korrumpiert.

### Empfehlungs-Audit-Log

Stuendlicher Snapshot der kausalen Empfehlung pro Zone, automatische Eval gegen den realen Sensor-Wert nach 6 und 24 h. Datengrundlage fuer den Vertrauens-Aufbau vor `ventilsteuerung_aktiv: true`. Endpoint `/api/empfehlungs-audit?zone_id=&tage=` mit Stats. UI-Rueckklassifikation alter Events ueber `PATCH /api/ventil-ereignis/{id}?paar=true` mit Audit-Trail.

### Watchdog-Push per iMessage

Proaktive iMessage-Pushes bei System-Anomalien — fuer Vergesslichkeits- und Urlaubs-Faelle, in denen der tagliche Dashboard-Check ausfaellt.

Heute zwei Trigger:
- **Empfehlung 3 Tage in Folge "akut"** fuer dieselbe Zone — moeglicher stiller Sensor-Defekt oder echter Trockenstress, den der User uebersehen hat (Pre-Mortem Akt 1).
- **Husqvarna-Soft-Ban-Frueherkennung**: juengste Gardena-Sensor-Messung aelter als 90 min — Cadence-Einbruch wie am 23.04.2026, der spaeter zum 67-h-Block fuehrte. Erstinstall (nie eine Messung) wird bewusst nicht gepusht, damit das Setup ruhig bleibt.

**Aktivierung**: `watchdog.aktiv: true` in `config/default.yaml` + Empfaenger via `watchdog.empfaenger` oder env `IMESSAGE_EMPFAENGER`. Throttle 24 h pro Trigger-Klasse + Zone (in DB persistiert, ueberlebt Restart). Defaults konservativ — Push ist Zweit-Linie, nicht Erst-Linie.

**Trigger C — Endpoint-Schema-Drift**: wenn DHS oder FYTA-Login laenger als 24 h einen Nicht-OK-Status haben (Schema umbenannt, Auth dauerhaft kaputt), sendet der Watchdog einen Push. Der `EndpointHealthJob` (Default `aktiv: true`) macht 1x/Tag einen leichtgewichtigen Probe-Call gegen beide Endpoints und persistiert das Ergebnis in `endpoint_health`. Inoffizielle Schnittstellen werden so frueh erkannt, wenn sie kippen.

### Wochen-Report per iMessage

Sonntags 20:00 bekommt der konfigurierte iMessage-Empfaenger eine Zusammenfassung der vergangenen Woche:
- Bewaesserungen pro Zone (Anzahl + Liter)
- Regen + Verdunstung pro Standort
- ML-Drift (MAE pro Horizont)
- Aktive Warnungen

**Aktivierung**: `wochen_report.aktiv: true` in `config/default.yaml`. Empfaenger: entweder explizit via `wochen_report.empfaenger` oder Fallback auf den ersten Zonen-Benachrichtigungs-Empfaenger.

### Ventilsteuerung

**Opt-In, dreistufig**: Eine Zone wird nur dann autonom bewaessert, wenn **alle drei** zutreffen:
1. `ventilsteuerung_aktiv: true` (global, Master-Switch; Default `false` = Log-Only, protokolliert nur, wann/wie lange es bewaessern wuerde).
2. `modus: automatik` (Zone, Shadow-Entscheidungslogik aktiv).
3. `auto_loop_opt_in: true` (Zone; Default `false`).

Damit laesst sich der Auto-Loop **pro Strang** scharfschalten: das globale Flag aktivieren, aber nur die gewuenschten Zonen per `auto_loop_opt_in` freigeben — andere `automatik`-Zonen bleiben Shadow (loggen weiter, feuern nicht). Manuelle Endpoints + Pre-Soak sind unabhaengig (Hand-am-Ruder).

**Auto-Pre-Soak:** Zonen mit `pre_soak_modus: immer` werden vom Auto-Loop als Pre-Soak gegossen (Puls + Pause + Hauptdose) statt als Einzellauf — Pflicht fuer Sprinkler-Zonen. Die Sequenz laeuft als **loop-getriebene State-Machine** (kein `asyncio.sleep`-Task): der wake-sichere Entscheidungsloop treibt die Phasen per Wall-Clock, sodass die Hauptdose auch nach einem Laptop-Sleep in der Pause zuverlaessig nachgezogen wird.

**Cycle-and-Soak (Mehrfach-Puls-Hauptdose):** Auf Sandboden laeuft eine lange Dose am Stueck oberflaechlich ab, statt einzusickern. Mit `haupt_pulse: 3` + `haupt_puls_pause_min: 21` wird die **berechnete** Hauptdose in drei gleich lange Pulse mit Einsickerpausen **aufgeteilt** — aus 90 min am Stueck werden 3x30 min. Wichtig: die Gesamt-Wassermenge bleibt exakt die, die das Plateau-Modell ausrechnet; `haupt_pulse` vervielfacht die Dosis **nicht**. Default `1`/`0` = das bisherige Verhalten (ein Hauptlauf am Stueck), Bestandszonen aendern sich also nicht. Vor jedem Puls fragt ein `puls_gate` erneut nach, ob weitergegossen werden darf — ein abgebrochener Lauf giesst keine Restpulse nach.

Zonen mit gleichem `ventil_kanal` teilen einen Wasserkreis. Der Start richtet sich nach der trockensten bzw. groessten-Defizit-Zone, nicht mehr nach dem Durchschnitt. Eine bereits sehr nasse Nachbarzone blockiert den Start nicht; sie wirkt nur als Max-Stop, wenn der Kanal bereits aktiv ist.

Sicherheitsmechanismen:
- **Watchdog-Timer**: schliesst Ventil nach `max_dauer_sekunden + 30s`.
- **Max-Stop pro Kanal**: aktive Kanaele werden gestoppt, sobald eine Kanal-Zone `feuchte_schwelle_max + 10` ueberschreitet.
- **Gardena-Server-Timeout** (3600s) als zweites Sicherheitsnetz.
- **Notfall-Stopp** via Dashboard oder `POST /api/notfall-stopp`.
- **Startup-Recovery**: schliesst offene Ventile nach Neustart, setzt Live-Manuell-Bewaesserungen + Pre-Soak-Sequenzen ueber Restart hinweg fort.
- **Sensor-Ausreisser-Filter** (MAD): einzelne Spikes kippen keine Entscheidung.
- **Leck-Detektor**: warnt wenn Bewaesserung keine Wirkung zeigt oder Sensor ueber 48 h konstant bleibt.
- **Festklemm-Sensor-Block**: bei offener `SENSOR_EINGEFROREN`-Warnung returnt `_robuste_feuchte` `None` -> Empfehlung faellt auf KEINE_MESSUNG-Pfad statt in eine wochenlange Akut-Spirale.
- **Wirkungsrate-Garbage-Filter**: Stale-CLOSED-Phantom-Dauer (z. B. 119 min statt nominal 90 min nach py-smart-gardena-Reconnect) wird vor Aufnahme in den Kalibrierungs-Median rausgefiltert -- Hard-Cap 120 min + Per-Zone-Cap fuer AUTOMATIK.
- **ML-Saettigungs-Cap**: drei Stufen gegen Mean-Reversion bei Saturierung + Regen — eng (>95 %/>5 mm), mittel (>85 %/>7 mm), weit (>75 %/>10 mm).
- **Single-Instance-Lock**: Lock-File unter `~/Library/Application Support/de.xindaan.pflanzen-dashboard/bewaesserung.pid` -- ueberlebt macOS-Sleep-Wake (vorher /tmp, das raeumt macOS auf).
- **Hahn-Cluster + Durchfluss-Budget**: mehrere Zonen am gleichen Wasserhahn teilen sich den Druck. Konfig pro Zone `hahn_cluster` + `verbrauch_lpm` + optional `exklusiv: true` fuer druckabhaengige Sprinkler. Pro Cluster `max_durchfluss_lpm` = echtes Hahn-Maximum. Pre-flight Lock prueft (a) Exklusivitaet: jede markierte Zone blockt Mitstart unabhaengig vom Volumen, (b) Volumen: `summe(verbrauch_lpm) <= max_durchfluss_lpm`. Reject mit `HAHN_BELEGT` + Klartext-Begruendung. Auto-Loop sortiert die Bedarfs-Kandidaten nach Akut-Score (Sensor - feuchte_kritisch), damit kritische Zonen Vorrang haben. Wirkungsrate-Auto-Kalibrierung verwirft Datenpunkte, deren Eval-Fenster mit einem Geschwister-Lauf am gleichen Hahn ueberlappt (Druck-Konkurrenz verfaelscht Sensor-Antwort). Default-Konfig leer = altes Verhalten unveraendert (Backward-Compat).
- **Shadow-Pause-Fallback**: verhindert Spam wiederholter Empfehlungen im Opt-In-Mode.

### API-Auth

Alle API-Endpoints (Ausnahme: `/api/health`) brauchen einen API-Key
im Header `X-Api-Key`. Zwei Rollen:

- **read**: alle GETs (Dashboard-Daten, Status, Historie).
- **control**: zusätzlich alle mutierenden Endpoints (Giessen, Notfall-
  Stopp, Ventil-Steuerung, Pre-Soak, Ereignis-Korrektur). Token-Bucket-
  Ratelimit 5/min/Key. Read ist auf control implizit enthalten.

Schluessel anlegen / verwalten:

```bash
# Neuen Schluessel erzeugen (Klartext wird NUR EINMAL angezeigt)
python -m bewaesserung.api_auth schluessel-erstellen --rolle control --label iphone-andre

# Alle Schluessel listen
python -m bewaesserung.api_auth liste

# Schluessel entfernen
python -m bewaesserung.api_auth loeschen --label iphone-andre
```

Die Hashes liegen unter
`~/Library/Application Support/de.xindaan.pflanzen-dashboard/api_keys.json`
(Mode `0600`). scrypt-gehasht, der Klartext landet nirgends auf Disk.

**Web-Frontend**: liest den Key aus `localStorage.pflanzen_api_key`
(per Hand setzen) bzw. fallback aus Vite-Env-Var `VITE_API_KEY`.
Ablage in `frontend/.env.local` (gitignored ueber `*.local`-Pattern,
greift sowohl in `vite dev` als auch in `vite build`).
**Wichtig**: NICHT `.env.development.local` -- das wird vom `vite build`
(Production-Mode) ignoriert, der Key landet dann nicht im Bundle. Ohne
Key (weder localStorage noch Env) → 401.

**iPhone-App**: Key landet im Keychain.

`/api/health` bleibt absichtlich public-minimal (`{"ok": true,
"zeitstempel": ...}`), damit `service.sh` und externe Liveness-Probes
weiter laufen. Versions-/Konfig-Details liegen unter
`/api/health/detail` (read-geschuetzt).

### API-Endpoints

Auth-Faustregel (Details s. oben): `/api/health` ist public, alle
weiteren GETs brauchen einen `read`-Key, POST/PATCH/DELETE brauchen
`control`. Vollstaendige Route-zu-Rolle-Matrix in
`backend/src/bewaesserung/api_auth.py:ROUTE_ROLLEN`.

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/api/zonen` | GET | Alle Zonen mit aktuellem Zustand |
| `/api/zonen/{id}/messwerte` | GET | Messwert-Verlauf (?stunden=48) |
| `/api/zonen/{id}/bilanz` | GET | Wasser-Bilanz (?fenster=24h\|7d\|30d) |
| `/api/zonen/{id}/ereignisse` | GET | Ventil-Events einer Zone |
| `/api/zonen/{id}/giess-historie` | GET | Giess-Laeufe einer Zone, gruppiert (?tage=N) — Pre-Soaks als ein Lauf |
| `/api/ventil-ereignisse` | GET | Ventil-Events gefiltert (?von&bis&zone_id) |
| `/api/ventil-ereignis/{id}` | PATCH | Ausloeser / Zeit / Liter korrigieren |
| `/api/ventil-ereignis/{id}` | DELETE | Event entfernen (War-Regen / Fehlalarm) |
| `/api/prognose` | GET | Bewaesserungs-Prognosen |
| `/api/standorte` | GET | Konfigurierte Wetter-Standorte mit Koordinaten |
| `/api/wetter` | GET | Wettervorhersage fuer alle Standorte (Multi-Standort-Dashboard) |
| `/api/wetter/{standort}` | GET | Wettervorhersage pro Standort |
| `/api/entscheidungen` | GET | Entscheidungs-Log (?zone_id&limit&format=csv\|json) — CSV-Export fuer Historie-Tab/Excel |
| `/api/ventil-status` | GET | Aktive Bewaesserungen |
| `/api/notfall-stopp` | POST | Alle Ventile sofort schliessen |
| `/api/giessen` | POST | Manuelles Giessen loggen (Zeit/Liter optional) |
| `/api/ops/summary` | GET | KPI-Zeile (Shadow, Bewaesserungen, Blocker, ...) |
| `/api/ops/timeline` | GET | Entscheidungen + Events chronologisch |
| `/api/schwellen-vorschlag` | GET | Datengetriebene Schwellen-Vorschlaege pro Zone |
| `/api/kalibrierung/{id}` | GET | Feldkapazitaet + Welkepunkt-Proxy pro Zone |
| `/api/health` | GET | Liveness-Check (public-minimal: `{ok, zeitstempel}`) |
| `/api/health/detail` | GET | Versions-/Konfig-Status (read-geschuetzt) |
| `/api/ml/status` | GET | ML-Modell-Status + Retrain-Infos (letzter Lauf, Gate-Faktor) |
| `/api/ml/vorhersage/{id}` | GET | ML-Feuchte-Vorhersage (6h/12h/24h) inkl. `q10`/`q90`; `?details=top_features` liefert SHAP-Beitraege |
| `/api/ml/drift` | GET | Rollender MAE pro Horizont (?zone_id&fenster=7d\|30d), Ampel `rot` wenn `mae > 1.5 × baseline` |
| `/api/ml/drift/log` | GET | Letzte N Drift-Log-Zeilen pro Horizont (Prognose vs. Ist + q10/q90) |
| `/api/ml/dauer-drift` | GET | 6h-Sensor-Ist-Delta-Drift fuer ML-Bewaesserungs-Response-Modell pro Zone |
| `/api/zonen/{id}/empfehlung-jetzt` | GET | Dry-Run-Giess-Empfehlung mit kausaler Erzaehlung + ML-Drift-Ampel |
| `/api/empfehlungs-audit` | GET | Audit-Trail Empfehlung vs. realer Sensor-Wert nach 6/24h (?zone_id&tage) |
| `/api/ventil-ereignis/{id}?paar=true` | PATCH | Korrektur eines Ventil-Event-Paars (OEFFNEN+SCHLIESSEN) inkl. Audit-Spalte |
| `/api/manuell/start` | POST | Live-Manuell-Bewaesserung ueber Dashboard, optional mit Pre-Soak-Sequenz |

## Konfiguration

`config/default.yaml` — zentrale Konfiguration:
- **ventilsteuerung_aktiv**: `false` (Log-Only) oder `true` (aktive Steuerung) — globaler Master-Switch (siehe Ventilsteuerung: Scharfschalten ist dreistufig).
- **Zonen**: Schwellen, Budget, bevorzugte Zeiten, Modus, `flaeche_m2`, `anteil_kanal` (Anteil am gemeinsamen Kanal-Wasser). Optional pro Zone:
  - `auto_loop_opt_in` — gibt diese Zone fuer den autonomen Auto-Loop frei. Greift nur zusammen mit globalem `ventilsteuerung_aktiv: true` + `modus: automatik`. So laesst sich der Auto-Loop pro Strang scharfschalten (z. B. nur Bambus), waehrend andere `automatik`-Zonen Shadow bleiben.
  - `pre_soak_modus` — `nie` = Auto-Loop giesst als Einzellauf; `immer` = als Pre-Soak (Puls + Pause + Hauptdose, mit `pre_soak_min`/`pre_soak_pause_min`). Pflicht fuer Sprinkler-Zonen (Einzellauf benetzt nur die Oberflaeche). Greift wie `auto_loop_opt_in` nur im scharfen Auto-Loop.
  - `haupt_pulse` / `haupt_puls_pause_min` — Cycle-and-Soak: teilt die Hauptdose in N gleich lange Pulse mit Einsickerpause dazwischen (z. B. `3` + `21` -> 3x30 min statt 90 min am Stueck). Teilt die Dosis auf, vervielfacht sie nicht. Default `1`/`0` = ein Hauptlauf am Stueck.
  - `optimum_feuchte_min` / `optimum_feuchte_max` — Pflanzen-Optimum-Range. Dominiert Schwellen-Vorschlag, wenn empirische Historie darunter liegt. Waldblumenhain 40/60, Bambuswald 60/75.
  - `welkepunkt` — manueller Override fuer den Stress-Punkt. Wenn nicht gesetzt, wird der Welkepunkt zur Laufzeit aus einer 5-stufigen Aufloesungs-Kette ermittelt: (0) **aktives `feuchte_regime[*].welkepunkt`** → (1) `zone.welkepunkt` (manuell) → (2) `feldkapazitaet_messung`-Kalibrierungs-Median bei n≥3 → (3) p10 der Tagesminima der letzten 90 Tage minus 3 pp Sicherheitsmarge → (4) `feuchte_kritisch`-Fallback (Regime-aware). Die Quelle wird ans Frontend durchgereicht ("aus Regime", "aus Sensor-Historie", "aus Kalibrierungs-Median", etc.). Saisonale Anpassung passiert primaer ueber den Decay (ET0+Regen); fuer hartharte Phasen-Wechsel (Magerwiese, Sukkulenten) das Regime nutzen. **Empfehlung: erstmal nicht manuell setzen** — Aufloesungs-Kette macht den Job sauber.
  - `sicherheits_tage` — wieviele Tage Reserve ueber Welkepunkt eine Bewaesserungs-Empfehlung schaffen soll. Default 3 deckt durchschnittliches Wochenende. Bei laengeren Abwesenheiten via API-Param `?sicherheits_tage=7` ueberschreibbar.
  - `versickerungs_karenz_stunden` — wie lange nach einem Ground-Truth-Bewaesserungsende die Sensor-Heuristik keinen Phantom-Event mehr erzeugt. Sandboden mit Sprinkler braucht 6 (Waldblumenhain), Mikrodrip 3 (Default).
  - `bewaesserungs_strategie` — `korridor` / `haeufig_klein` / `selten_gross` / `konstant_niedrig`. Steuert die Trigger-Logik der kausalen Empfehlung (siehe oben).
  - `delta_pp_pro_minute` — Pro-Zone-Wirkungsrate (Sensor-Anstieg pro Bewaesserungs-Minute). Wenn None: Auto-Kalibrierung aus `feldkapazitaet_messung typ='wirkungsrate'`. Realistische Werte: Sprinkler-Sand ~0.17, Mikrodrip-Bambus ~0.4. Konfig-Override hat Vorrang vor Kalibrierungs-Median.
  - `feuchte_regime` — saisonale/phasen-abhaengige Override-Liste, siehe unten.
- **FYTA**: Pflanzen-IDs und Zone-Zuordnung. Polling holt **alle** Datenpunkte aus dem `list-measurements`-Range. Beim Service-Start laeuft zusaetzlich ein Lueckenfuellungs-Hook, der die letzten 7 Tage automatisch nachzieht und mit existierenden Eintraegen via `(zone_id, zeitstempel, geraet_id)` dedupliziert. Restart-Luecken durch Crashes/Auth-Lag/DB-Lock-Konflikte werden so automatisch geschlossen. Status sichtbar in der CLI-Console `[Backfill-Hook] fertig: X importiert, Y duplikate, Z Tage`.
- **Wetter**: Standorte mit Koordinaten.
- **bilanz**: Durchflussrate pro Kanal + manueller Schlauch (L/min).
- **schwellen_adaption**: ET0-abhaengige Schwellen-Anhebung bei Hitze.
- **ml_ausschluss_fenster**: Zeitraeume, die beim ML-Training **und** beim Schwellen-Vorschlag ignoriert werden (z.B. Sensor-Umzug, nicht-eingeschlemmte Initial-Phase). Aktuelles Beispiel: `bambuswald_yogaraum` 2026-04-06 bis -07 (94 × 0.0 % haetten sonst das 10-Perzentil verzerrt).
- **ml.retrain**: Auto-Retrain — `aktiv`, `intervall_tage`, `trainings_fenster_tage`, `gate_faktor`, `folds`, `quantile`, `monotone_constraints`, `start_verzoegerung_minuten` (Default 30 — verhindert dass der erste Retrain direkt nach Service-Start die CPU blockiert und Dashboard-Polls verzoegert). `monotone_constraints: true` ist Opt-In und constrained nur `niederschlag_summe_*` monoton steigend. Nach jedem erfolgreichen Training wird das Gate `mae_neu < gate_faktor * mae_alt` pro Horizont geprueft; nur bei Bestehen aller drei werden die neuen Modelle deployed, sonst Rollback.
- **kalibrierung**: Feldkapazitaet + Welkepunkt-Proxy + Wirkungsrate — `aktiv`, `intervall_stunden` (Default 1, idempotent), `regen_min_mm`, `plateau_max_delta`, `welkepunkt_min_tage`, `saison_monate`. Erkennt Feldkapazitaet per Regen-Peak + 12-24h-Plateau, Welkepunkt-Proxy per Sensor-Minimum nach Trockenphase (Saison Mai-Sept), Wirkungsrate aus SCHLIESSEN-Events mit Garbage-Filter.
- **plant_optimum_intervall_stunden**: FYTA-API-Abruf-Intervall fuer `plant.measurements.moisture.values.min_good/max_good`. Default 24h, Cache in Tabelle `plant_optimum`.
- **wochen_report**: `aktiv`, `empfaenger` (optional — Fallback auf ersten Zonen-Benachrichtigungs-Empfaenger), Wochentag/Uhrzeit. Sonntags 20:00 iMessage-Zusammenfassung.
- **watchdog**: `aktiv`, `empfaenger` (optional — Fallback auf `IMESSAGE_EMPFAENGER` env), `intervall_minuten`, `throttle_stunden`, `akut_in_folge_tage`, `husqvarna_max_alter_minuten` (Default 90 — juengste Gardena-Messung aelter = Block-Push). Aktuell Default ON in `default.yaml`.
- **feuchte_regime** pro Zone: saisonale oder phasen-abhaengige Schwellen-Overrides. Liste von Eintraegen mit `von_mm_dd`/`bis_mm_dd` (MM-DD-Range, Wraparound ueber Jahreswechsel erlaubt) plus optionalen Werten `optimum_min/max`, `feuchte_schwelle_min/max`, `feuchte_kritisch`, `name`, `grund`. Felder mit `null` werden vom Zone-Default uebernommen. Sinnvoll fuer Magerwiesen mit Sommer-Trockenphase, Sukkulenten/Zitrus mit Winter-Ruhe oder Anwachs-Phasen. Beispiel:<br/>`feuchte_regime:`<br/>`  - {name: anwachs, von_mm_dd: "04-01", bis_mm_dd: "06-30", optimum_min: 40, optimum_max: 60, grund: "Aussaat-Phase"}`<br/>`  - {name: sommer_trocken, von_mm_dd: "07-01", bis_mm_dd: "09-30", feuchte_schwelle_min: 12, feuchte_kritisch: 5, optimum_min: 15, optimum_max: 25, grund: "Trockenphase gegen Konkurrenzgraeser"}`
- **backup**: DB-Backup-Job — Intervall, Retention, Verzeichnis, Monatlich-Schalter. **Off-Mac-Spiegel**: optionales `spiegel_verzeichnis` fuer Off-Mac-Backup (z. B. iCloud Drive). Mac-Disk-Defekt wuerde sonst alle lokalen Backups gleichzeitig mitnehmen. Tilde-Expansion + Spiegel-Rotation wie lokales Verzeichnis. Spiegel-Fehler (z. B. iCloud offline) sind isoliert geloggt und retried beim naechsten Tick.
- **endpoint_health**: Schema-Probe-Calls fuer DHS + FYTA. `aktiv` (Default true), `intervall_stunden` (Default 24). Pre-Mortem-Akt-4-Schluss: erkennt stille Schema-Drift inoffizieller Endpoints, Watchdog-Trigger C feuert iMessage bei > 24 h-Fehler.
- **ml_bewaesserungs_response**: Pro-Zone-Modell fuer empfohlene Giess-Dauer. `aktiv` (Shadow-Logging), `wirksam` (scharf statt Heuristik), `min_events`, Drift-Eval-Endpoint `/api/ml/dauer-drift`.
- **bewaesserungs_response_retrain**: Auto-Retrain fuer Response-Modelle, eigenes Gate.
- **sensor_namen** — Klartextnamen pro Sensor-Geraet-ID fuer
  die SensorListeDiagnose-Tabelle (nur sichtbar bei Multi-Sensor-Zonen
  wie Waldblumenhain). FYTA-Namen werden automatisch aus
  `fyta.pflanzen[].name` vor-belegt (Schluessel `fyta_<fyta_id>`),
  manuelle Eintraege ueberschreiben. Gardena-UUIDs **muessen** hier
  gepflegt werden, sonst zeigt das Frontend die gekuerzte ID als
  Fallback. Beispiel:<br/>
  `sensor_namen:`<br/>
  `&nbsp;&nbsp;"11111111-1111-1111-1111-111111111111": "Staudenbeet (Gardena)"`<br/>
  `&nbsp;&nbsp;"fyta_100001": "Topfpflanze A (FYTA Terra 11 cm)"`

Umgebungsvariablen (`.env`, siehe `.env.example`):
- `GARDENA_CLIENT_ID`, `GARDENA_CLIENT_SECRET` — Developer-API (Pflicht fuer Live-Steuerung).
- `GARDENA_CUSTOMER_EMAIL`, `GARDENA_CUSTOMER_PASSWORD` — Customer-Login fuer DHS-Backfill (inoffiziell, aber stabil genutzt; kein Fallback fuer verlorene WebSocket-Events).
- `FYTA_EMAIL`, `FYTA_PASSWORD` — Email + Passwort des FYTA-Accounts. Backend holt sich automatisch ein Access-Token, refresht bei 401/403 und cached in `backend/daten/fyta_customer_token.json`. Kein manuelles Token-Extrahieren mehr noetig.
- `FYTA_ACCESS_TOKEN` (Legacy) — manuell aus web.fyta.de LocalStorage extrahiertes Bearer-Token. Bleibt als Fallback aktiv, solange `FYTA_EMAIL`/`FYTA_PASSWORD` leer sind. Bei neuen Installs nicht mehr setzen.
- `IMESSAGE_EMPFAENGER` — fuer Push-Benachrichtigungen.
- `WETTER_BREITE`, `WETTER_LAENGE` — Default-Standort (einzeln ueberschreibbar in `config/default.yaml`).

## Troubleshooting

- **Dashboard leer**: Backend laeuft? → `curl http://127.0.0.1:8090/api/zonen`
- **Keine FYTA-Daten**: Token abgelaufen? → neuen Token in `.env` eintragen.
- **Gardena nicht verbunden**: Firewall/VPN? Gardena braucht WebSocket-Verbindung.
- **DHS-Backfill inaktiv**: `GARDENA_CUSTOMER_*` in `.env` setzen, neu starten.
- **"ML-Vorhersage deaktiviert" im Log**: Modelle liegen nicht unter `backend/daten/ml/`. Neu trainieren (siehe Development) oder Rollback aus `ml_archiv/<datum>/`.
- **Kein q10/q90 im Chart**: Service laedt nur Punkt-Modelle (nicht Quantile). Entweder Quantile-Retrain laufen lassen (`ml.cli trainiere --quantile`) oder auf den naechsten Auto-Retrain warten (wenn `ml.retrain.aktiv: true`).
- **Dashboard startet 5-10 Min langsam**: Der erste Retrain lief direkt nach Service-Start und blockierte den Event-Loop mit LightGBM. Dagegen greift `ml.retrain.start_verzoegerung_minuten: 30` — wenn dein Config-Wert niedriger ist, anheben.
- **Schwellen-Vorschlag unsinnig niedrig (z.B. 5 %)**: Der Sensor hat Messwerte aus einer Nicht-Boden-Phase geliefert (Einschlemmen, Umzug). Loesung: Zeitraum in `ml_ausschluss_fenster` aufnehmen — Perzentile werden dann um diese Periode herum berechnet. Beispiel `bambuswald_yogaraum` 2026-04-06/07.
- **"Backend nicht erreichbar" waehrend des Retrains (historisch)**: Ab 2026-04-19 laeuft das Training in `asyncio.to_thread`, der Event-Loop bleibt frei — kein Freeze mehr.
- **"Bewaesserungen heute: 0" trotz Aktivitaet**: die KPI zaehlt nur **heute seit 00:00 lokal**. Gestern-Events (z.B. Einschlemmen am Abend) zaehlen nicht.
- **WebSocket-Event fehlt**: Heartbeat-Zeile `ws_events=...` zeigt Counter — siehe `docs/t-0055-websocket-diagnose.md`.
- **Watchdog-Fehlalarm `Husqvarna-Soft-Ban`**: tritt auf, wenn `husqvarna_max_alter_minuten` zu eng gesetzt ist. Realdaten zeigen ~3-8 Messungen/h gesamt (alle Sensoren), pro Sensor ~1/h. Default 90 min Schwelle ist sicher; bei stabilem Wetter koennen einzelne Sensoren auch >60 min schweigen.
- **Watchdog wiederholt nach Fehlalarm 24 h still**: Throttle-Eintrag in `watchdog_event` muss geloescht werden:<br/>`sqlite3 backend/daten/bewaesserung.db "DELETE FROM watchdog_event WHERE typ='husqvarna_block';"` (mit gestopptem Backend). Gilt analog fuer andere Trigger-Klassen.
- **`database is locked`-Lockup**: ueblicherweise zwei `start.sh`-Instanzen parallel oder ein vergessener `sqlite3` CLI-Prozess. Lock-Datei pruefen: `cat ~/Library/Application\ Support/de.xindaan.pflanzen-dashboard/bewaesserung.pid`. Mit gestopptem Backend ggf. loeschen + neu starten.
- **Endpoint-Health-Push fuer `gardena_dhs` oder `fyta`**: ein echter Schema-Drift oder Auth-Bruch in der inoffiziellen API. `endpoint_health`-Tabelle zeigt Details:<br/>`sqlite3 backend/daten/bewaesserung.db "SELECT * FROM endpoint_health;"`

## Development

```bash
# Backend-Tests (vollstaendige Suite)
cd backend && PYTHONPATH=src ../.venv/bin/pytest -q

# Frontend Lint + Build
cd frontend && npx eslint src/ && npx vite build

# FYTA-Daten nachziehen (Luecken fuellen)
cd backend && PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.fyta_backfill --von 2026-02-22 --bis 2026-04-06

# Wetter-Archiv manuell nachziehen (Open-Meteo ERA5)
cd backend && PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.wetter_archiv --von 2026-02-01 --bis 2026-04-12

# Luftfeuchte-Backfill fuer VPD-Feature
cd backend && PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.wetter_archiv \
    --luftfeuchte-backfill --von 2026-04-09 --bis 2026-04-13
```

### ML-CLI (`bewaesserung.ml.cli`)

```bash
cd backend

# Punkt-Modelle trainieren (3 Modelle fuer 6h/12h/24h)
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli trainiere \
    --von 2026-02-20 --bis 2026-04-19 --folds 3

# Quantile-Modelle trainieren
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli trainiere --quantile \
    --von 2026-02-20 --bis 2026-04-19 --folds 3

# Dry-Run / Gate-Check — trainiert ins *_gatecheck/, vergleicht MAE,
# modifiziert Live-Modelle NICHT. Nuetzlich fuer Validierung vor `aktiv: true`.
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli trainiere \
    --quantile --gate-check --von 2026-02-20 --bis 2026-04-19 --folds 3

# Monotone-Regen-A/B — trainiert Default und Monotone in
# Temp-Verzeichnisse, modifiziert Live-Modelle NICHT.
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli trainiere \
    --quantile --monotone-ab --von 2026-02-20 --bis 2026-04-19 --folds 3

# Gate-Check mit aktivierten Monotone-Constraints.
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli trainiere \
    --quantile --gate-check --monotone-constraints \
    --von 2026-02-20 --bis 2026-04-19 --folds 3

# Baseline-Vorhersage evaluieren (Regel-basierter Trend + Wetter)
PYTHONPATH=src ../.venv/bin/python3 -m bewaesserung.ml.cli baseline \
    --von 2026-04-01 --bis 2026-04-19
```

### Retrain-Deployment (manuell)

Nur wenn Auto-Retrain (`ml.retrain.aktiv: true`) aus ist oder eine spezielle
Konstellation einen Sofort-Deploy braucht:

1. Vorheriges Live-Verzeichnis archivieren: `mv backend/daten/ml backend/daten/ml_archiv_$(date +%F)`
2. Neues Trainingsverzeichnis an die Live-Stelle verschieben: `mv daten/ml_neu backend/daten/ml`
3. Service neu starten (`./service.sh restart` oder Ctrl+C + `./start.sh`).
4. Verify: Startlog zeigt `3 Punkt-Modell(e) + 6 Quantile-Modell(e) geladen`.

Im Regelfall uebernimmt der `MlRetrainJob` genau das: Training in `_tmp/`,
Gate-Pruefung, atomarer Deploy nach `_archiv/<datum>/` bei Bestehen.

### DB-Backups

Der Service legt taeglich einen Snapshot der SQLite-DB an (native
SQLite-Backup-API, integrity-verifiziert, atomar ueber `os.replace`):

```
backend/daten/backup/
  taeglich/    bewaesserung_YYYY-MM-DD.db    (letzte 14 Tage)
  monatlich/   bewaesserung_YYYY-MM.db       (erster je Monat, dauerhaft)
```

**Off-Mac-Spiegel**: mit `backup.spiegel_verzeichnis` (z. B.
iCloud Drive) wird jeder Snapshot zusaetzlich dort abgelegt. Mac-Disk-
Defekt wuerde sonst alle lokalen Backups gleichzeitig mitnehmen. Spiegel-
Fehler (iCloud offline) sind isoliert geloggt und beim naechsten Tick
retried.

Konfiguration: `config/default.yaml`, Block `backup:` (Intervall, Retention,
Verzeichnis, Spiegel, Abschalter). Wiederherstellung siehe
[docs/backup_wiederherstellung.md](docs/backup_wiederherstellung.md).

## Architektur

```
backend/src/bewaesserung/
  main.py                  — Einstiegspunkt, verdrahtet alle Jobs, Entscheidungs-Loop, Single-Instance-Lock
  entscheidung.py          — Entscheidungslogik + Min-Start/Max-Stop pro Kanal + MAD-Filter + 4 Bewaesserungs-Strategien + kausale Empfehlung mit Welkepunkt-Reserve + FeuchteRegime-Override
  ventil_sicherung.py      — Safety-Wrapper (Watchdog, Notfall-Stopp, Retry-Limit)
  pre_soak.py              — Pre-Soak-Sequenz (Vorwasser + Pause + Hauptdose) mit Restart-Recovery
  gardena_client.py        — Gardena Smart System API (OAuth, WebSocket) + Diagnose-Metriken
  gardena_customer_auth.py — Husqvarna-IAM Customer-Login fuer smart.gardena.com
  gardena_web_backfill.py  — DHS-Backfill (smart.gardena.com/v1/dhs) als Haupt-Event-Quelle
  sensor_dhs_backfill.py   — Sensor-Bodenfeuchte/-Temperatur via DHS preset=sensor2
  fyta_client.py           — FYTA Pflanzensensor-API (Polling) + Auto-Login
  wetter.py                — Open-Meteo Wettervorhersagen
  wetter_archiv.py         — Open-Meteo Archive (ERA5-Ground-Truth) Job
  wetter_ereignisse.py     — Frost / Hitze / Starkregen-Erkennung
  bilanz.py                — Wasser-Bilanz pro Zone (Zugefuehrt / Verdunstet / Bilanz)
  leck_detektor.py         — Bewaesserung-ohne-Wirkung / Sensor-eingefroren (saisonal Mai-Sept)
  sensor_backfill.py       — Sensor-Heuristik-Fallback (Feuchte-Spruenge -> Ventil-Events) inkl. Aktiv-Lauf-Schutz
  sensor_health.py         — Ausfall- und Batterie-Warnungen
  speicher.py              — SQLite-Zeitreihenspeicher + Migrationen + WAL-Mode (Tabellen u.a. `plant_optimum`, `feldkapazitaet_messung`, `empfehlungs_audit`, `pre_soak_state`, `live_lauf_state`, `watchdog_event`, `endpoint_health`)
  backup.py                — DB-Backup-Rotation taeglich/monatlich + Off-Mac-Spiegel
  api_server.py            — FastAPI REST-Server
  sensordaten.py           — Sensor-Callback-Verarbeitung + temperature-only-Beat-Filter
  benachrichtigung.py      — iMessage-Versand (osascript)
  watchdog.py              — Proaktives iMessage-Push bei Anomalien (akut-3-Tage / Husqvarna-Stille / Endpoint-Schema-Drift)
  endpoint_health.py       — Schema-Probe-Calls fuer DHS + FYTA, persistiert Status
  schwellen_vorschlag.py   — Datengetriebene Schwellen-Vorschlaege + Regime-Override
  plant_optimum_job.py     — FYTA-API-Abruf `min_good/max_good`, 24h-Cache
  kalibrierung.py          — Feldkapazitaet + Welkepunkt-Proxy + Wirkungsrate-Auto-Kalibrierung mit Garbage-Filter
  report.py                — Wochen-Report-Generator + iMessage-Job (ISO-Wochen-Idempotenz)
  empfehlungs_audit_job.py — Snapshot Empfehlung + Eval gegen realen Sensor-Wert nach 6/24h
  ml/
    features.py            — Feature Engineering (~65 Features inkl. VPD, Bilanz-Delta, ziel_delta_Xh)
    training.py            — Walk-Forward-CV, Delta-Regressor, Band-Scale, Sample-Weighting + Monotone-Constraints
    vorhersage.py          — Laden + Live-Inferenz, q10/q90-Band, Saettigungs-Cap (3 Stufen)
    evaluation.py          — MAE/RMSE/R²/Pinball-Loss/Abdeckungsrate/Baseline
    drift_job.py           — Persistiert Live-Prognosen, gleicht mit Ist ab + Catchup-Loop
    retrain_job.py         — Woechentlicher Auto-Retrain mit Deploy-Gate
    response_features.py   — Pro-Zone-Response-Features fuer Bewaesserungs-Dauer-Modell
    response_training.py   — Forward-Quantile + Inverse-Punkt-Modell pro Zone
    response_vorhersage.py — Live-Inference fuer empfohlene Giess-Dauer (Shadow + scharf)
    response_retrain_job.py — Auto-Retrain fuer Response-Modelle mit Drift-Gate
    cli.py                 — CLI fuer Training / Baseline / Gate-Check / Monotone-A/B / Response
  konfig.py                — YAML-Config-Loader

frontend/src/
  App.tsx                  — Layout + Tab-Routing
  komponenten/
    ZonenKarte.tsx         — Pro-Zone-Hauptkarte
    EntscheidungsErklaerung.tsx — "Warum"-Panel pro Zone
    WasserBilanz.tsx       — KPI-Block Zugefuehrt/Verdunstet/Bilanz
    FeuchteAnzeige.tsx     — Feuchte-Wert-Darstellung + Schwellen-Indikator
    StatusLeiste.tsx       — Zonen-Klassifikation oben
    WetterKarte.tsx        — Temperatur + Niederschlag pro Standort
    ManuellesGiessen.tsx   — Modal fuer Schlauch-Bewaesserung
    MLStatusBadge.tsx      — Modell-Status-Badge (geladen / fehlt / veraltet)
    MLAttributierung.tsx   — On-Demand-SHAP-Beitraege pro Horizont
    HistorieTab.tsx        — Entscheidungs-Historie mit Filter + CSV-Export
    OpsTab.tsx             — Ops-Review-Seite
    OpsFilter.tsx          — Filter-Leiste fuer Ops-Timeline (Typ / Zone / Zeit)
    OpsKpiZeile.tsx        — KPI-Karten (Shadow, Bewaesserungen, Blocker, ...)
    OpsTimeline.tsx        — Chronologie-Timeline
    OpsTimelineEintrag.tsx — Einzel-Eintrag der Ops-Timeline (Severity, Icon, Aktion)
    TagEvents.tsx          — Heutige Ventil-Events + Klassifikations-Buttons
    EntscheidungsLog.tsx   — Letzte 30 Entscheidungen, einklappbar
    FehlerGrenze.tsx       — ErrorBoundary um Tabs + ZonenKarten [Review-Fix F16]

config/default.yaml        — Zonen, Schwellen, Standorte, Bilanz, Schwellen-Adaption,
                             Kalibrierung, Plant-Optimum, Wochen-Report, ML-Ausschluss-Fenster
docs/                      — Technische Notizen (WebSocket-Diagnose, Design-Prompts,
                             Recherche-Synthesen, Feldkapazitaets-Check, Phase-4-Report)
```

### ML-Pipeline (Phase 4 + Regen-Fixes)

Die Feuchte-Prognose basiert auf einem LightGBM-Stack mit 65 Features
(Sensor-Lags, Rolling-Averages, Wetter, Bewaesserung, VPD, Bilanz-Delta).

| Task | Inhalt |
|---|---|
| **VPD** | `relative_humidity_2m` aus Open-Meteo + Magnus-Formel → `vpd_mittel_{6,12,24}h` als Transpirations-Feature. Archive-Backfill via `wetter_archiv --luftfeuchte-backfill`. |
| **Drift-Log** | Jede Live-Inferenz landet in `ml_vorhersage_log` mit `prognose_ziel_zeit` + Modell-Version. Stuendlicher Job evaluiert gegen Sensor-Messung ±30 min zum Ziel. `/api/ml/drift` liefert rollende MAE pro Horizont mit Ampel (rot wenn `> 1.5 × baseline`). |
| **Bilanz-Delta** | `bilanz_liter_{6,12,24}h` + `bilanz_diff_24h = feuchte_diff - bilanz_liter/flaeche_m2` als Residual fuer Versickerung und Wurzel-Aufnahme (NaN fuer Zonen ohne `flaeche_m2`). |
| **Quantile Regression** | Pro Horizont drei Modelle (q10/q50/q90, Pinball-Loss). `aktuell_{H}h.lgbm` zeigt auf q50 fuer Backwards-Compat. Frontend rendert Unsicherheits-Band zwischen q10 und q90. Bei Crossing bleibt q50 stabil, nur das Band wird korrigiert. |
| **Auto-Retrain** | Woechentlicher In-Service-Retrain, Training in `<live>_tmp/`. Gate `mae_neu < gate_faktor × mae_alt` fuer alle drei Horizonte — Pass → atomarer Deploy, alte Modelle nach `<live>_archiv/<datum>/`; Ablehnung → tmp loeschen, Live unberuehrt. Opt-In via `ml.retrain.aktiv`. `start_verzoegerung_minuten` (Default 30) verhindert Dashboard-Lags beim Service-Start. |
| **Saettigungs-Cap (3 Stufen)** | Post-Processing gegen Mean-Reversion bei Saettigung + Regen. (Eng) `>95 %/>5 mm -> aktuell-2`, (Mittel) `>85 %/>7 mm -> aktuell-4`, (Weit) `>75 %/>10 mm -> aktuell-6`. Mittel/Weit ergaenzt 2026-05-04 nach Pre-Mortem-Akt 5: 6h-MAE war gemildert, 12h+24h noch nicht. |
| **Sample-Weighting** | Training gewichtet Regen- und Saettigungs-Samples 3x. Greift erst mit neuer Trainingsrunde und wirkt allmaehlich. |
| **Delta-Regressor** | **Kernfix der Regen-Ignoranz**: Modelle lernen `ziel_delta_Xh = ziel - aktuell` statt absolutes Feuchteniveau. Inferenz rechnet `aktuell + delta_q50` und clippt erst danach auf 0-100 %. Eliminiert den 44 %-Mean-Reversion-Anker, der beim absoluten Regressor bei Saettigung (100 %) eine Prognose zurueck Richtung Trainings-Mittelwert erzwang. Dry-Run-Ergebnis: 6h MAE −30 %, 12h −19 %, 24h −26 %. |
| **Band-Scale** | Nach jedem Training wird die empirische q10/q90-Abdeckung auf Holdout gemessen und ein Skalierfaktor pro Horizont in `band_scale_{h}h.json` gespeichert. Inferenz skaliert die Bandbreite damit, Zielkorridor 75-85 %. Vermeidet 27-Modell-Explosion (= eigene Quantile pro Klasse). |
| **Monotone-A/B** | Opt-In-Constraints: `+1` fuer `niederschlag_summe_*`, `0` fuer alle anderen Features. CLI `--monotone-ab` vergleicht Default vs. Monotone mit Gesamt-MAE und Regen-Slice-MAE, ohne Live-Modelle zu veraendern. |
| **Feldkapazitaets-/Welkepunkt-Kalibrierung** | 6h-Job sucht Regen-Peaks (mindestens `regen_mindestmenge_mm`) und pruefd 24h spaeter, ob die Sensor-Feuchte ein Plateau zeigt — das ist die zonen-spezifische Feldkapazitaet. Welkepunkt-Proxy: Sensor-Minimum nach Trockenphase in Saison Mai-Sept. Ergebnisse in Tabelle `feldkapazitaet_messung`, per `/api/kalibrierung/{zone_id}` abrufbar. Liefert die Grundlage fuer spaetere physikalische Schwellen-Kalibrierung. |

### Erklaerbarkeit & Schwellen

Neben der reinen Prognose liefert das Dashboard mehrere Hilfen fuer den User,
die Entscheidungen nachzuvollziehen und zu verbessern:

- **SHAP-Panel**: aufklappbare Zeile pro Zone zeigt die Top-5-Feature-
  Beitraege pro Horizont via LightGBM `pred_contrib=True`. Laedt lazy — kein
  Dashboard-Polling-Overhead. Gruene Balken treiben Prognose hoch, orange runter.
- **Schwellen-Vorschlag**: aus 30 Tagen Sensor-Historie werden 10-/90-
  Perzentile mit ±5 %-Puffer berechnet, gefiltert durch `ml_ausschluss_fenster`.
  Liefert einen Vorschlag, **ohne** die Config zu aendern — Entscheidung bleibt
  beim User.
- **Optimum-Dominanz**: wenn eine Zone `optimum_feuchte_min/max` hat
  (aus Config oder FYTA-Cache), wird ein zu niedriger empirischer Vorschlag
  nach oben korrigiert (`quelle=optimum_dominiert`). Rationale: wenn der User
  historisch zu wenig gegossen hat, darf der Vorschlag nicht diese schlechte
  Gewohnheit zementieren.
- **FYTA-Optimum-Cache**: 24h-Job liest
  `plant.measurements.moisture.values.min_good/max_good` pro FYTA-Pflanze
  und legt sie in Tabelle `plant_optimum` ab. Wird als Fallback genutzt,
  wenn keine Config-Werte gesetzt sind.
- **Historie-Tab + CSV-Export**: vollstaendige Entscheidungs-Historie
  mit Filter (Zeitraum, Zone, Blocker). Export als CSV fuer Excel / Pandas.

### Event-Pipeline

Bewaesserungs-Events landen in `ventil_ereignis` aus mehreren Quellen, mit Prioritaet:

1. **Live-WebSocket** (`ventil_id` = UUID): Gardena-Cloud pusht Events via py-smart-gardena — Ground truth.
2. **User-manuell** (`ventil_id='manuell'`): ueber `POST /api/giessen` oder das Dashboard-Modal.
3. **Gardena-Web-DHS** (`ventil_id='gardena_web'`): periodischer Pull des inoffiziellen `smart.gardena.com/v1/dhs`-Endpoints (faengt Offline-Phasen und WebSocket-Verluste ab).
4. **Sensor-Heuristik** (`ventil_id='sensor_heuristik'`): Fallback wenn DHS ausfaellt; erkennt Events ueber Feuchte-Spruenge, markiert sie als `ausloser=UNBEKANNT` fuer Nutzer-Klassifikation.

Dedup-Fenster: pro `(zone_id, zeitstempel ±60s)` nur **ein** Event-Paar; niedrigere Prioritaet skip.

**Stack**: Python 3.13 + asyncio, FastAPI, aiosqlite, LightGBM, httpx, structlog | React 19, Recharts, Vite, TypeScript

## Lizenz

MIT — siehe [LICENSE](LICENSE).
