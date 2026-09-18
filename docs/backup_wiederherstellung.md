# DB-Backup und Wiederherstellung

## Wo liegen die Backups?

```
backend/daten/backup/
  taeglich/    bewaesserung_YYYY-MM-DD.db.gz   (letzte 14 Tage, rolliert)
  monatlich/   bewaesserung_YYYY-MM.db.gz      (erster Snapshot je Monat, dauerhaft)
  bewaesserung_vor_deploy_JJJJMMTT_HHMMSS.db[.gz]   (letzte 3 Deploys)
  <Eingriffs-Sicherungen>                           (vor Tasks/Korrekturen)
backend/daten/backups/                              (Ad-hoc-Sicherungen bis 09/2026)
```

## Aufbewahrung

Seit 01.11.2026 gilt fuer `backend/daten/backup/` eine feste Regel. Anlass:
Die Platte lief voll, allein zehn rohe Deploy-Sicherungen belegten 4,3 GB.

| Art | Erkennung | Regel | Wer setzt sie um |
|---|---|---|---|
| Taeglich | `taeglich/` | 14 Tage (`retention_taeglich_tage`) | `backup.py` |
| Monatlich | `monatlich/` | je Monat eine, dauerhaft | `backup.py` |
| Deploy | `bewaesserung_vor_deploy_*` | letzte 3 Deploys; juengste roh, zwei davor gz; aeltere geloescht | `tools/sicherungen_aufbewahrung.py` |
| Eingriff | alle anderen Eintraege der Wurzel | ab 7 Tagen gz; Loeschen nur nach Freigabe, wenn der Task erledigt und die Sicherung mindestens 14 Tage alt ist | Werkzeug packt, Andre gibt Loeschen frei |
| Eingriff mit Zeilenloeschung | Name mit `cleanup`, `dedupe`, `reparatur`, `bereinig` | dauerhaft (gz) | Werkzeug packt, loescht nie |

Begruendung der Abweichungen von "Task-Sicherung bis Abschluss":
- **Deploy-Wiederholungen zaehlen einmal.** Sicherungen, die weniger als 30 min
  nach der vorigen liegen, sind ein Ereignis (22.10.2026: drei Sicherungen in
  einer Minute). Sonst verdraengen Wiederholungen echte Rueckwege.
- **Zeilenloeschende Eingriffe bleiben dauerhaft.** Eine Sicherung vor einer
  Bereinigung ist die einzige Kopie der geloeschten Zeilen (gemessen 01.11.:
  `backups/bewaesserung_vor_t0493_*` traegt 1.519 `sensor_messung`-Zeilen, die
  in der Live-DB fehlen). Messdaten bleiben vollstaendig; Platz kommt ueber
  gzip (Faktor ~5).
- **Komprimieren statt Loeschen, wo der Rueckweg noch zaehlt.** Nur die
  juengste Deploy-Sicherung bleibt roh, damit ein Rueckweg ohne Entpacken geht.

Vor einem Deploy ersetzt der Aufruf
`.venv/bin/python tools/sicherungen_aufbewahrung.py vor-deploy` das bisherige
Hand-`sqlite3 .backup`: Er sichert ueber die SQLite-Backup-API (Live-DB nur
lesend), prueft `quick_check` und rotiert erst danach. Schlaegt die Sicherung
fehl, wird nichts geloescht. `plan` zeigt den Bestand gegen die Regel samt
Loeschkandidaten, `aufraeumen --ausfuehren` wendet sie an (Deploy rotieren,
Eingriffe packen; Eingriffe loescht es nie). Die Live-DB und alles ausserhalb
der Wurzel sind gesperrt.

Bevor ein Loeschkandidat faellt, wird geprueft, ob er Zeilen traegt, die in
der juengsten Sicherung fehlen (Abgleich je Tabelle ueber den
Primaerschluessel, Sicherungen nur mit `?mode=ro&immutable=1` oeffnen, damit
keine `-shm`/`-wal` entstehen). Spaeter ergaenzte Zeilen (z. B.
`ml_vorhersage_log`, `empfehlungs_audit`), Wetter-Revisionen per
`INSERT OR REPLACE` und Umbuchungen mit Originalwert im `korrektur_log` sind
kein Verlust.

Der `BackupJob` laeuft im Entscheidungs-Loop, Intervall-gesteuert (Default
24 h). Erste Ausfuehrung nach Service-Start. Nutzt die native SQLite-Backup-API
(`aiosqlite.Connection.backup`), ist konsistent auch bei parallelem Schreiben.

Konfigurierbar in `config/default.yaml` unter `backup:`. Abschaltbar via
`aktiv: false`.

**Seit 01.08.2026: Snapshots liegen gzip-komprimiert.** Gemessen an
`bewaesserung_2026-07-25.db`: 195,3 MB -> 38,4 MB, Faktor 5,1. Es wird dabei
nichts geloescht und nichts ausgeduennt — nur die Ablageform aendert sich, alle
Messdaten bleiben vollstaendig erhalten und ueberpruefbar.

Ablauf pro Lauf: SQLite schreibt den Snapshot in eine temporaere Roh-Datei
(`.bewaesserung_....db.roh`, fuehrender Punkt -> wird von den Rotations-Globs
bewusst nicht getroffen), die anschliessend chunkweise nach `.db.gz`
komprimiert und danach geloescht wird. Alle Kopien (Monats-Snapshot, Spiegel)
laufen streamend ueber `shutil.copyfileobj`, ohne die Datei komplett in den
RAM zu laden.

**Alt-Bestand.** Unkomprimierte `.db`-Snapshots aus der Zeit davor werden
weiterhin gefunden, gezaehlt und rotiert. Rotation und Status-Endpoint
gruppieren beide Ablageformen pro Tag: ein Tag, der aus der Retention faellt,
verliert beide Formen. Zum einmaligen Nachziehen des Alt-Bestands gibt es
`tools/migriere_backups_gzip.py` (siehe unten).

## Wiederherstellung (Dry-Run fuer einen Host-Umzug)

1. **Service stoppen**
   ```
   ./service.sh stop
   ```

   Danach pruefen, dass kein Prozess die DB mehr offen hat (auch keine
   `sqlite3`- oder Notebook-Sitzung). Kein Output = frei:
   ```
   lsof backend/daten/bewaesserung.db*
   ```

2. **Aktuelle DB beiseite legen (Sicherheit)**

   Ueber die SQLite-Backup-API, nicht per `cp`: nach einem unsauberen Stopp
   (Absturz, SIGKILL, zweite offene Verbindung) steckt ein Teil der Daten
   noch in `bewaesserung.db-wal`, und eine Kopie der Hauptdatei allein waere
   unvollstaendig. `.backup` liest den WAL mit.
   ```
   sqlite3 backend/daten/bewaesserung.db \
      ".backup backend/daten/bewaesserung.db.vor-restore"
   ```

3. **Backup zurueckspielen** (passendes Datum waehlen)

   Zuerst die WAL-Begleitdateien der alten DB beiseite legen (verschieben,
   nicht loeschen). Bleiben sie liegen, spielt SQLite beim naechsten Oeffnen
   ihre Seiten auf das zurueckgespielte Backup: je nach Alter tauchen die
   Zeilen wieder auf, die der Restore loswerden sollte, oder es entsteht ein
   Seitenmischstand. Der Integrity-Check in Schritt 4 bemerkt den ersten
   Fall nicht.
   ```
   for s in -wal -shm; do
     [ -e "backend/daten/bewaesserung.db$s" ] && \
       mv "backend/daten/bewaesserung.db$s" "backend/daten/bewaesserung.db$s.vor-restore"
   done
   ```

   Dann entpacken bzw. kopieren:
   ```
   # komprimiert (Standard): entpackt direkt an den Zielort
   gunzip -c backend/daten/backup/taeglich/bewaesserung_2026-04-19.db.gz \
      > backend/daten/bewaesserung.db

   # unkomprimierter Alt-Bestand
   cp backend/daten/backup/taeglich/bewaesserung_2026-04-19.db \
      backend/daten/bewaesserung.db
   ```

   Vorher pruefen, dass das Archiv unbeschaedigt ist — das kostet Sekunden
   und verhindert, dass eine kaputte Datei ueber die Live-DB laeuft:
   ```
   gzip -t backend/daten/backup/taeglich/bewaesserung_2026-04-19.db.gz
   ```
   Kein Output = in Ordnung.

   Nur hineinschauen, ohne die Live-DB anzufassen:
   ```
   gunzip -c backend/daten/backup/taeglich/bewaesserung_2026-04-19.db.gz > /tmp/pruef.db
   sqlite3 /tmp/pruef.db "SELECT COUNT(*) FROM sensor_messung;"
   ```

4. **Integrity-Check**
   ```
   sqlite3 backend/daten/bewaesserung.db "PRAGMA integrity_check;"
   ```
   Muss `ok` liefern.

   **Nur fuer Snapshots vor September 2026 (u. a. die Monats-Snapshots
   2026-04 bis 2026-08, T-0610 PERS-4):** Diese Staende enthalten noch
   doppelte Zeilen in `sensor_messung`. Der aktuelle Code legt beim Start
   den eindeutigen Index aus T-0493 an und bricht dann mit
   `IntegrityError: UNIQUE constraint failed: sensor_messung...` ab. Vor dem
   ersten Start deshalb die Duplikate auf der zurueckgespielten DB
   entfernen (Dienst ist gestoppt, das Skript schreibt nur in diese Datei):
   ```
   .venv/bin/python scripts/migration_t0493_sensor_dedupe.py --dry-run
   .venv/bin/python scripts/migration_t0493_sensor_dedupe.py --apply
   ```
   Das Skript liegt nur im privaten Arbeits-Repo; im oeffentlichen Ableger
   ist es nicht enthalten.

5. **Service starten**
   ```
   ./service.sh start
   ```

   **Bei Snapshots vor September 2026 danach einmal neu starten**
   (`./service.sh restart`). Hatte der Snapshot noch das alte
   `live_lauf_state`-Schema (Primaerschluessel nur `kanal`, vor T-0203),
   baut der erste Start die Tabelle neu und verliert dabei die eben
   ergaenzten Spalten `lauf_gruppe` und `phase`; bis zum zweiten Start
   scheitert jedes Schreiben des Laufzustands (Ventil laeuft, aber ohne
   Restart-Recovery). Ob das zutrifft, zeigt vorher
   `sqlite3 backend/daten/bewaesserung.db "PRAGMA table_info(live_lauf_state);"`:
   steht `pk` nur bei `kanal` auf 1, ist der Neustart noetig; ein
   ueberzaehliger Neustart schadet nicht. Die Code-Loesung (Dedupe und
   Neubau-Reihenfolge in `speicher.py`) ist offen.

6. **Smoke-Test**
   - `curl localhost:8090/api/zonen` — Zonen sind da.
   - Frontend oeffnen, Wasserbilanz rendert, letzte Messungen sichtbar.

## Alt-Bestand nachtraeglich komprimieren

`tools/migriere_backups_gzip.py` zieht die vorhandenen unkomprimierten
Snapshots nach. Es **loescht keine Daten** — es ersetzt eine Datei erst, wenn
die komprimierte Fassung verifiziert ist, und bricht bei jedem Fehler ab,
statt weiterzulaufen. Wie das Dedupe-Skript oben liegt es nur im privaten
Arbeits-Repo (`tools/` ist im oeffentlichen Ableger nicht enthalten).

```
# Erst schauen, was passieren wuerde (Default, schreibt nichts):
.venv/bin/python tools/migriere_backups_gzip.py

# Wirklich migrieren:
.venv/bin/python tools/migriere_backups_gzip.py --ausfuehren
```

Verifikation pro Datei, bevor das Original entfernt wird:

1. vollstaendige Dekompression (prueft CRC32 + Laengenfeld, entspricht `gzip -t`),
2. entpackte Groesse == Originalgroesse **und** komprimiert echt kleiner,
3. SQLite-Header der entpackten Daten (`SQLite format 3`).

Schlaegt eine davon fehl, wird das halbfertige `.gz` entfernt, das Original
bleibt liegen, Exit-Code 1. Ebenfalls Abbruch: eine Datei mit **nicht leerem**
`-wal` daneben — dann steckt ein Teil der Daten noch im WAL und die `.db`
allein waere unvollstaendig. Leere `-wal`/`-shm`-Reste einer geschlossenen
sqlite3-Sitzung sind harmlos und blockieren nicht (im Bestand vom 01.08. bei
vier Ad-hoc-Sicherungen der Fall); sie werden weder geloescht noch veraendert.
Die Live-DB `backend/daten/bewaesserung.db` ist vom Sweep ausgenommen.

Der Sweep umfasst per Default **beide** Backup-Baeume. Dry-Run-Stand 01.08.,
31 Dateien:

| Ort | Dateien | Was |
|---|---|---|
| `backend/daten/backup/taeglich/` | 14 | rotierte Tages-Snapshots |
| `backend/daten/backup/monatlich/` | 5 | Monats-Snapshots |
| `backend/daten/backup/` (Wurzel) | 3 | alte `*_vor_T0*_cleanup.db` |
| `backend/daten/backups/` | 9 | manuelle Ad-hoc-Sicherungen |

Mit `--verzeichnis <pfad>` laesst sich das einschraenken (mehrfach moeglich).

## Hinweise

- Der Job schreibt zuerst nach `<name>.db.tmp`, prueft `PRAGMA integrity_check`
  und wechselt erst bei `ok` atomar via `os.replace` auf den Zielnamen.
  Danach wird komprimiert — ebenfalls ueber `.tmp` + `os.replace`, damit ein
  Abbruch kein halbes `.db.gz` hinterlaesst.
- Taegliche Snapshots werden nach `retention_taeglich_tage` rotiert.
- Monatliche Snapshots werden **nicht** automatisch geloescht. Bei Ueberlauf
  ueber `max_dateien` gibt es eine Warnung (`backup.monatlich_ueberlauf`).
- Fehler im Backup-Job werden isoliert geloggt (`backup.fehler`) und kippen
  den Service nicht. `_letzte_aktualisierung` bleibt ungesetzt, der naechste
  Zyklus versucht es erneut.
