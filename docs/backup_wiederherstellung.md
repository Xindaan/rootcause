# DB-Backup und Wiederherstellung

## Wo liegen die Backups?

```
backend/daten/backup/
  taeglich/    bewaesserung_YYYY-MM-DD.db.gz   (letzte 14 Tage, rolliert)
  monatlich/   bewaesserung_YYYY-MM.db.gz      (erster Snapshot je Monat, dauerhaft)
```

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

2. **Aktuelle DB beiseite legen (Sicherheit)**
   ```
   cp backend/daten/bewaesserung.db backend/daten/bewaesserung.db.vor-restore
   ```

3. **Backup zurueckspielen** (passendes Datum waehlen)
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

5. **Service starten**
   ```
   ./service.sh start
   ```

6. **Smoke-Test**
   - `curl localhost:8090/api/zonen` — Zonen sind da.
   - Frontend oeffnen, Wasserbilanz rendert, letzte Messungen sichtbar.

## Alt-Bestand nachtraeglich komprimieren

`tools/migriere_backups_gzip.py` zieht die vorhandenen unkomprimierten
Snapshots nach. Es **loescht keine Daten** — es ersetzt eine Datei erst, wenn
die komprimierte Fassung verifiziert ist, und bricht bei jedem Fehler ab,
statt weiterzulaufen.

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
