"""T-0181-Folge: Periodischer Fit der linearen Skalen-Transformation
zwischen fremden Sensor-Quellen (z.B. FYTA Terra Beam) und dem Gardena-
Sensor als Referenz.

Hintergrund
-----------
Multi-Sensor-Zonen (heute nur `waldblumenhain` mit Gardena + 2 FYTA)
mischen die Live-Median-Aggregation (T-0179c) ueber zwei Sensor-Skalen:

  - Gardena: 0-100 lokal kalibrierter Relativ-Index (bodenart-kalibriert)
  - FYTA Terra: `soil_moisture`, real ebenfalls 0-100 (T-0410 DB-gemessen:
    25 % der Werte > 65, max 100). Was der Wert genau IST, ist offen: die
    Hersteller-Doku nennt "VWC 0-65", das deckt sich nicht mit dem API-
    Payload -- also NICHT als Labor-VWC behandeln.

Konsens-Spannweite waldblumen 2026-05: 8-10 pp zwischen den beiden
Skalen bei gleichem realen Boden-Zustand. Das verfaelscht den Median
(T-0179c) und produziert Phantom-Trockenphasen oder verpasste
Bewaesserungs-Auslesungen.

Die Schreibseite des Mappings (`upsert_skalen_mapping`) und die
Aggregat-Anbindung (`letzte_messung_aggregiert`) sind seit Mai 2026
verdrahtet -- es fehlte nur ein Job, der die `(a, b)`-Koeffizienten
periodisch aus echten Paral-Sensor-Daten fittet. Dieser Job schliesst
die Lucke.

Verfahren
---------
Pro Zone mit `calibration_pair_quellen != []`:

1. 30 Tage Messungen pro Sensor-Quelle laden (Gardena als Referenz +
   die in `calibration_pair_quellen` aufgelisteten Quellen).
2. Pro Quelle-Messung den **zeitlich naechsten** Gardena-Wert (binnen
   ±15 min Toleranz) suchen. Cadence-Mismatch (Gardena 10 min vs
   FYTA 3-4 h) macht ein dichtes Sample auf Gardena-Seite, ein
   duenneres auf FYTA-Seite -- Pairing geht von FYTA aus, damit jede
   FYTA-Messung in maximal einem Paar landet.
3. Spannweite der gepaarten Roh-Werte pruefen
   (`min_spannweite_pp`) -- bei zu schmalem Bereich ist der lineare
   Fit nicht identifizierbar (Sand-Sommer 5 pp Schwankung kann kein
   Mapping kalibrieren).
4. `numpy.polyfit(roh, ref, deg=1)` -> `(a, b)`.
5. Residuum-MAE = mean(|a*roh + b - ref|). Wenn > `max_residual_pp`:
   Mapping nicht linear genug (vermutlich Sensor-Substrat-Problem),
   verwerfen.
6. UPSERT via `Speicher.upsert_skalen_mapping`.

Idempotent: kollidiert mit ML-Retrain/Backup-Bursts ueber
`_mit_lock_retry`. Crashes landen in `letzter_fehler` (T-0108-Pattern),
sichtbar via `/api/ml/status`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from bewaesserung.modelle import (
    DatenQuelle,
    GesamtKonfig,
    MlSkalenMappingKonfig,
    SensorMessung,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Toleranz fuer das Pairing FYTA <-> Gardena. 15 min deckt Gardena-
# 10-min-Cadence + Jitter ab; groesseres Fenster wuerde echte Substrat-
# Dynamik (Boden trocknet binnen 30 min messbar bei Sonne) als
# Skalen-Differenz interpretieren.
PAIRING_TOLERANZ_MIN = 15
# Rueckblick-Fenster fuer den Fit. 30 Tage gleicht Tages-Schwankungen
# aus, ohne durch Saison-Drift (Mai -> Juni Wechsel) verzerrt zu werden.
RUECKBLICK_TAGE = 30


def _paare_messungen(
    referenz: list[SensorMessung],
    fremd: list[SensorMessung],
    toleranz_min: int = PAIRING_TOLERANZ_MIN,
) -> list[tuple[float, float]]:
    """Liefert `(roh, ref)`-Paare: pro Fremd-Messung den zeitlich naechsten
    Referenz-Wert binnen `toleranz_min`. Keine Doppel-Nutzung der Fremd-
    Messung; Referenz-Messung darf mehrfach gepaart sein (gewollt, weil
    Gardena dichter taktet).

    `referenz` und `fremd` werden hier intern chronologisch sortiert
    -- Aufrufer muss nicht vorab sortieren.
    """
    if not referenz or not fremd:
        return []
    ref_sorted = sorted(referenz, key=lambda m: m.zeitstempel)
    fremd_sorted = sorted(fremd, key=lambda m: m.zeitstempel)
    paare: list[tuple[float, float]] = []
    toleranz = timedelta(minutes=toleranz_min)
    # Zwei-Pointer: weil ref_sorted aufsteigend ist, kann der Such-Index
    # mit fremd vorwaerts wandern, ohne O(N*M) zu kosten.
    idx_ref = 0
    for f in fremd_sorted:
        if f.boden_feuchte is None:
            continue
        # Schiebe `idx_ref` so weit nach vorne, dass `ref_sorted[idx_ref]`
        # der erste Eintrag >= `f.zeitstempel - toleranz` ist.
        unter = f.zeitstempel - toleranz
        while (
            idx_ref < len(ref_sorted)
            and ref_sorted[idx_ref].zeitstempel < unter
        ):
            idx_ref += 1
        # Kandidaten: idx_ref und idx_ref-1 (= letzter vor dem Fenster);
        # pruefe beide auf zeitliche Distanz und nimm den naeheren.
        bestes: SensorMessung | None = None
        beste_distanz: timedelta | None = None
        for kandidat_idx in (idx_ref - 1, idx_ref):
            if 0 <= kandidat_idx < len(ref_sorted):
                k = ref_sorted[kandidat_idx]
                if k.boden_feuchte is None:
                    continue
                distanz = abs(k.zeitstempel - f.zeitstempel)
                if distanz > toleranz:
                    continue
                if beste_distanz is None or distanz < beste_distanz:
                    bestes = k
                    beste_distanz = distanz
        if bestes is not None and bestes.boden_feuchte is not None:
            paare.append((float(f.boden_feuchte), float(bestes.boden_feuchte)))
    return paare


def _filtere_ausschluss_messungen(
    messungen: list[SensorMessung],
    konfig: GesamtKonfig,
    zone_id: str,
    wartungs_fenster: list[tuple[datetime, datetime]],
) -> list[SensorMessung]:
    """Entfernt statische ML- und offene Wartungs-Fenster aus Fit-Daten."""
    fenster = [
        f for f in (getattr(konfig, "ml_ausschluss_fenster", []) or [])
        if f.zone_id == zone_id
    ]
    out: list[SensorMessung] = []
    for messung in messungen:
        ts = messung.zeitstempel
        ausgeschlossen = False
        for f in fenster:
            if f.von <= ts <= f.bis:
                gid = getattr(f, "geraet_id", None)
                if gid is None or gid == messung.geraet_id:
                    ausgeschlossen = True
                    break
        if ausgeschlossen:
            continue
        if any(von <= ts <= bis for von, bis in wartungs_fenster):
            continue
        out.append(messung)
    return out


class SkalenMappingFitJob:
    """Periodisch (Default 24 h): fittet Skalen-Mapping pro
    `(zone_id, sensor_quelle)`.

    Lebenszyklus analog `KalibrationsJob` (T-0063) und `MlRetrainJob`
    (T-0011): konstruiert in `main.py`, getickt im `_entscheidungsloop`
    via `aktualisiere_wenn_faellig`. `letzter_erfolg`/`letzter_fehler`
    fliessen in `/api/ml/status` (T-0108).
    """

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        sm_konfig: MlSkalenMappingKonfig,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._sm = sm_konfig
        self._intervall = timedelta(hours=sm_konfig.intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
        # T-0108-Pattern: Sichtbarkeit fuer Status-Endpoint.
        self._letzter_erfolg: datetime | None = None
        self._letzter_fehler: dict | None = None

    @property
    def letzter_erfolg(self) -> datetime | None:
        return self._letzter_erfolg

    @property
    def letzter_fehler(self) -> dict | None:
        return self._letzter_fehler

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> bool:
        """Laeuft hoechstens einmal pro `intervall_stunden`.
        Return True wenn gelaufen (ungeachtet wie viele Mappings gefittet
        wurden -- "gelaufen" = der Scan ist durchgekommen ohne Crash).
        """
        if not self._sm.aktiv:
            return False
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return False
        try:
            await self._scan_alle_zonen(jetzt)
        except Exception as exc:
            logger.exception("skalen_mapping.fehler")
            self._letzter_fehler = {
                "zeit": jetzt.isoformat(),
                "typ": type(exc).__name__,
                "nachricht": str(exc)[:300],
            }
            return False
        self._letzte_aktualisierung = jetzt
        self._letzter_erfolg = jetzt
        self._letzter_fehler = None
        return True

    async def _scan_alle_zonen(self, jetzt: datetime) -> None:
        """Iteriert ueber Zonen mit `calibration_pair_quellen != []`."""
        von = jetzt - timedelta(days=RUECKBLICK_TAGE)
        zonen = [
            z for z in self._konfig.zonen
            if z.calibration_pair_quellen
        ]
        if not zonen:
            return
        gefittet = 0
        for zone in zonen:
            # Gardena ist immer die Referenz-Skala (Index 0-100 lokal
            # kalibriert per Bodenart-Setting in der App). Falls eine
            # Zone keinen Gardena-Sensor hat, gibt es nichts zu fitten.
            quellen_zum_lesen = sorted(
                {DatenQuelle.GARDENA.value, *zone.calibration_pair_quellen}
            )
            gruppen = await self._speicher.hole_messungen_pro_quelle(
                zone.zone_id, quellen_zum_lesen, von, jetzt,
            )
            wartungs_fenster = await self._wartungs_fenster(zone.zone_id, jetzt)
            referenz = _filtere_ausschluss_messungen(
                gruppen.get(DatenQuelle.GARDENA.value, []),
                self._konfig,
                zone.zone_id,
                wartungs_fenster,
            )
            if not referenz:
                logger.info(
                    "skalen_mapping.kein_referenz_sensor",
                    zone_id=zone.zone_id,
                )
                continue
            for quelle in zone.calibration_pair_quellen:
                if quelle == DatenQuelle.GARDENA.value:
                    # Referenz fittet nicht gegen sich selbst.
                    continue
                fremd = _filtere_ausschluss_messungen(
                    gruppen.get(quelle, []),
                    self._konfig,
                    zone.zone_id,
                    wartungs_fenster,
                )
                erfolgreich = await self._fitte_quelle(
                    zone_id=zone.zone_id,
                    quelle=quelle,
                    referenz=referenz,
                    fremd=fremd,
                    jetzt=jetzt,
                )
                if erfolgreich:
                    gefittet += 1
        if gefittet:
            logger.info(
                "skalen_mapping.abgeschlossen",
                anzahl_gefittet=gefittet,
            )

    async def _wartungs_fenster(
        self, zone_id: str, jetzt: datetime,
    ) -> list[tuple[datetime, datetime]]:
        try:
            rows = await self._speicher.hole_wartungs_fenster(
                nur_offen=True, zone_id=zone_id,
            )
        except Exception:
            logger.exception("skalen_mapping.wartungs_fenster_fehler")
            return []
        cap = jetzt + timedelta(days=1)
        out: list[tuple[datetime, datetime]] = []
        for row in rows:
            try:
                von = datetime.fromisoformat(row["von_am"])
                bis_raw = row.get("bis_am")
                bis = datetime.fromisoformat(bis_raw) if bis_raw else cap
            except (KeyError, TypeError, ValueError):
                continue
            out.append((von, bis))
        return out

    async def _fitte_quelle(
        self,
        *,
        zone_id: str,
        quelle: str,
        referenz: list[SensorMessung],
        fremd: list[SensorMessung],
        jetzt: datetime,
    ) -> bool:
        """Eine Quelle einer Zone fitten. Return True bei UPSERT, False
        bei Skip. Skip-Gruende werden geloggt, damit die Sammel-Phase
        nachvollziehbar bleibt.
        """
        if len(fremd) < self._sm.min_obs:
            logger.info(
                "skalen_mapping.skip_obs",
                zone_id=zone_id, quelle=quelle,
                n_fremd=len(fremd), min_obs=self._sm.min_obs,
            )
            return False
        paare = _paare_messungen(referenz, fremd)
        if len(paare) < self._sm.min_obs:
            logger.info(
                "skalen_mapping.skip_paare",
                zone_id=zone_id, quelle=quelle,
                n_paare=len(paare), min_obs=self._sm.min_obs,
            )
            return False
        # Spannweite der Roh-Werte. Wenn Sand-Sommer 5 pp Schwankung
        # hat, kann der Fit zwischen "a=1.0,b=0" und "a=0.5,b=+20"
        # nicht unterscheiden -- bewusst ueberspringen.
        roh = [p[0] for p in paare]
        ref = [p[1] for p in paare]
        spannweite = max(roh) - min(roh)
        if spannweite < self._sm.min_spannweite_pp:
            logger.info(
                "skalen_mapping.skip_spannweite",
                zone_id=zone_id, quelle=quelle,
                spannweite_pp=round(spannweite, 1),
                min_spannweite=self._sm.min_spannweite_pp,
            )
            return False
        # numpy ist via lightgbm-Stack ohnehin Pflicht-Dependency.
        import numpy as np
        x = np.asarray(roh, dtype=float)
        y = np.asarray(ref, dtype=float)
        # polyfit deg=1: kleinste Quadrate, ergibt [a, b] mit y = a*x + b.
        try:
            a, b = np.polyfit(x, y, 1)
        except Exception as exc:
            logger.warning(
                "skalen_mapping.polyfit_fehler",
                zone_id=zone_id, quelle=quelle, fehler=str(exc),
            )
            return False
        a = float(a)
        b = float(b)
        # T-0285: Korrelations-Guard. Residuum + Spannweite erkennen einen
        # Rausch-Fit NICHT -- bei unkorrelierten Sensoren (verschiedene
        # Mikro-Standorte) liefert polyfit eine instabile Steigung mit
        # zufaellig kleinem MAE (Realfall waldblumen: FYTA<->Gardena r~0,
        # Fit a=1.666 zog FYTA 58->51). |Pearson-r| unter Schwelle ->
        # KEIN valides lineares Mapping. Da hier Obs + Spannweite bereits
        # erfuellt sind (ausreichende Datenlage), wird ein evtl. frueher
        # gefittetes (Fehl-)Mapping geloescht -> Identity, statt eine
        # Verzerrung dauerhaft stehen zu lassen.
        with np.errstate(invalid="ignore"):
            korr = float(np.corrcoef(x, y)[0, 1])
        if not np.isfinite(korr) or abs(korr) < self._sm.min_korrelation:
            logger.info(
                "skalen_mapping.skip_korrelation",
                zone_id=zone_id, quelle=quelle,
                korr=None if not np.isfinite(korr) else round(korr, 3),
                min_korr=self._sm.min_korrelation,
            )
            await self._speicher.loesche_skalen_mapping(zone_id, quelle)
            return False
        residuum_mae = float(np.mean(np.abs(a * x + b - y)))
        if residuum_mae > self._sm.max_residual_pp:
            logger.info(
                "skalen_mapping.skip_residuum",
                zone_id=zone_id, quelle=quelle,
                mae_pp=round(residuum_mae, 2),
                max=self._sm.max_residual_pp,
            )
            await self._speicher.loesche_skalen_mapping(zone_id, quelle)
            return False
        await self._speicher.upsert_skalen_mapping(
            zone_id=zone_id,
            quelle=quelle,
            a=a, b=b,
            n_obs=len(paare),
            gefittet_am=jetzt,
        )
        logger.info(
            "skalen_mapping.gefittet",
            zone_id=zone_id, quelle=quelle,
            a=round(a, 4), b=round(b, 2),
            n_paare=len(paare),
            spannweite_pp=round(spannweite, 1),
            mae_pp=round(residuum_mae, 2),
        )
        return True
