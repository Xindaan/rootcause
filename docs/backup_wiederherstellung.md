# DB-Backup und Wiederherstellung

## Wo liegen die Backups?

```
backend/daten/backup/
  taeglich/    bewaesserung_YYYY-MM-DD.db    (letzte 14 Tage, rolliert)
  monatlich/   bewaesserung_YYYY-MM.db       (erster Snapshot je Monat, dauerhaft)
```

Der `BackupJob` laeuft im Entscheidungs-Loop, Intervall-gesteuert (Default
24 h). Erste Ausfuehrung nach Service-Start. Nutzt die native SQLite-Backup-API
(`aiosqlite.Connection.backup`), ist konsistent auch bei parallelem Schreiben.

Konfigurierbar in `config/default.yaml` unter `backup:`. Abschaltbar via
`aktiv: false`.

## Wiederherstellung (Dry-Run fuer Pi-Umzug)

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
   cp backend/daten/backup/taeglich/bewaesserung_2026-04-19.db \
      backend/daten/bewaesserung.db
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

## Hinweise

- Der Job schreibt zuerst nach `<name>.db.tmp`, prueft `PRAGMA integrity_check`
  und wechselt erst bei `ok` atomar via `os.replace` auf den Zielnamen.
- Taegliche Snapshots werden nach `retention_taeglich_tage` rotiert.
- Monatliche Snapshots werden **nicht** automatisch geloescht. Bei Ueberlauf
  ueber `max_dateien` gibt es eine Warnung (`backup.monatlich_ueberlauf`).
- Fehler im Backup-Job werden isoliert geloggt (`backup.fehler`) und kippen
  den Service nicht. `_letzte_aktualisierung` bleibt ungesetzt, der naechste
  Zyklus versucht es erneut.
