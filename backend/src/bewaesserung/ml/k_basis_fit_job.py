"""Hybrid Stufe 1: Fit-Job fuer die Trocknungs-Konstante `k_basis_pro_h`.

Pro Zone:
  1. Bewaesserungs-Schliessungs-Events der letzten 30 Tage holen.
  2. Pro Schliessung pruefen, ob die folgenden N Stunden eine
     **echte Trockenphase** waren: kein neues OEFFNEN binnen N h,
     kein Regen > `max_regen_im_fenster_mm` in der Zone, mindestens
     `min_phasen_dauer_h` Sensor-Daten verfuegbar.
  3. Sensor-Zeitreihe innerhalb der Phase nehmen, gegen das
     exponentielle Decay-Modell von `physik_trocknung.py` per Grid-
     Search ueber `k_basis`-Kandidaten fitten.
  4. Median ueber alle qualifizierten Phasen liefert das robuste
     `k_basis`. Mittlere ET0 der Phasen wird als `et0_basis_mm_pro_h`
     mitgespeichert (Skalierungs-Anker fuer Live-Prognose).
  5. UPSERT in Tabelle `physik_k_basis`.

T-0108-Pattern: `letzter_erfolg` / `letzter_fehler` sichtbar in
`/api/ml/status`. Read-only: aendert NICHTS an `pruefe_zone`,
`vorhersage_zone`, Blocker-Kaskade.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

import structlog

from bewaesserung.ml.physik_trocknung import fitte_k_und_welkepunkt_phase
from bewaesserung.modelle import (
    GesamtKonfig,
    KEINE_WASSER_AUSLOESER,
    MlPhysikDiagnoseKonfig,
    SensorMessung,
    VentilAktion,
    VentilEreignis,
    WetterArchivStunde,
)
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Rueckblick-Fenster fuer den Fit. 30 Tage matchen das
# Skalen-Mapping-Fit-Fenster.
RUECKBLICK_TAGE = 30


def _zone_zu_standort(konfig: GesamtKonfig) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for standort in konfig.standorte:
        for zid in standort.zonen:
            if standort.wetter_standort:
                mapping[zid] = standort.wetter_standort
    return mapping


def _wasser_intervalle(
    events: list[VentilEreignis],
) -> list[tuple[datetime, datetime]]:
    """Wasser-Fluss-Intervalle aus Ventil-Events — bewusst ALLE ausloser
    (auch unbekannt/ignoriert: der Fluss war physisch, nur buchhalterisch
    ignoriert; T-0350).

    - SCHLIESSEN mit dauer>0 -> [ts - dauer, ts]. Deckt den Kontaminations-
      Fall "Close ohne Live-OPEN" ab (DHS-Backfill, Orphan-Close), der die
      alte Nur-OEFFNEN-Logik unterlief.
    - OEFFNEN -> [ts, ts]: Wasser-Start bekannt, Ende offen (das zugehoerige
      SCHLIESSEN liefert das volle Intervall, falls vorhanden).
    """
    out: list[tuple[datetime, datetime]] = []
    for e in events:
        if e.aktion == VentilAktion.SCHLIESSEN and (e.dauer_sekunden or 0) > 0:
            out.append((
                e.zeitstempel - timedelta(seconds=e.dauer_sekunden),
                e.zeitstempel,
            ))
        elif e.aktion == VentilAktion.OEFFNEN:
            out.append((e.zeitstempel, e.zeitstempel))
    return sorted(out)


def _phasen_kandidaten(
    schliessungen: list[VentilEreignis],
    wasser_intervalle: list[tuple[datetime, datetime]],
    min_dauer_h: int,
    max_dauer_h: int,
    verbots_fenster: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Findet Trockenphasen [t_close, ende] nach SCHLIESSEN-Ankern.

    T-0350-Haertung: Eine Phase endet am naechsten **Wasser-Start**
    (aus `_wasser_intervalle`: OEFFNEN ODER rueckgerechneter Start eines
    SCHLIESSEN dauer>0 — frueher nur OEFFNEN, wodurch ein nachgetragener
    Close ohne Live-OPEN die Phase still kontaminierte). Ein Intervall,
    das den Phasen-Anker ueberspannt (Wasser lief beim Anker noch/wieder),
    verwirft die Phase. Phasen, die ein `verbots_fenster` (auto-ignore-
    Regime der Zone) ueberlappen, werden verworfen. Phasen unter
    `min_dauer_h` werden verworfen.
    """
    if not schliessungen:
        return []
    out: list[tuple[datetime, datetime]] = []
    min_dt = timedelta(hours=min_dauer_h)
    max_dt = timedelta(hours=max_dauer_h)
    for sc in sorted(schliessungen, key=lambda e: e.zeitstempel):
        start = sc.zeitstempel
        ende = start + max_dt
        verworfen = False
        for i_von, i_bis in wasser_intervalle:
            if i_bis <= start:
                # Endet vor/mit dem Anker (inkl. Anker-Event selbst).
                continue
            if i_von <= start:
                # Wasser laeuft ueber den Anker hinweg -> Phase unbrauchbar.
                verworfen = True
                break
            ende = min(ende, i_von)
        if verworfen:
            continue
        if ende - start < min_dt:
            continue
        if any(
            not (f_bis < start or f_von > ende)
            for f_von, f_bis in (verbots_fenster or [])
        ):
            continue
        out.append((start, ende))
    return out


def _regen_im_fenster(
    wetter: list[WetterArchivStunde], von: datetime, bis: datetime,
) -> float:
    """Summe Niederschlag im halboffenen Intervall (von, bis]."""
    return sum(
        (w.niederschlag_mm or 0.0)
        for w in wetter if von < w.zeitstempel <= bis
    )


def _mittlere_et0(
    wetter: list[WetterArchivStunde], von: datetime, bis: datetime,
) -> float | None:
    werte = [
        w.et0_mm for w in wetter
        if von < w.zeitstempel <= bis and w.et0_mm is not None
    ]
    if not werte:
        return None
    return statistics.mean(werte)


def _event_in_ausschluss(
    event: VentilEreignis, konfig: GesamtKonfig, zone_id: str,
) -> bool:
    for f in getattr(konfig, "ml_ausschluss_fenster", []) or []:
        if f.zone_id == zone_id and f.von <= event.zeitstempel <= f.bis:
            return True
    return False


def _filtere_messungen_fenster(
    messungen: list[SensorMessung],
    konfig: GesamtKonfig,
    zone_id: str,
    wartungs_fenster: list[tuple[datetime, datetime]],
) -> list[SensorMessung]:
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


class KbasisFitJob:
    """Periodischer Fit-Job fuer `physik_k_basis`."""

    def __init__(
        self,
        speicher: Speicher,
        konfig: GesamtKonfig,
        physik_konfig: MlPhysikDiagnoseKonfig,
    ) -> None:
        self._speicher = speicher
        self._konfig = konfig
        self._physik = physik_konfig
        self._intervall = timedelta(hours=physik_konfig.intervall_stunden)
        self._letzte_aktualisierung: datetime | None = None
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
        if not self._physik.aktiv:
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
            logger.exception("k_basis_fit.fehler")
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
        von = jetzt - timedelta(days=RUECKBLICK_TAGE)
        # T-0484: frueher `z.ventil_kanal is not None`. Das war ein aeusseres
        # Gate, das ausschloss, was die innere Logik ausdruecklich einschliesst
        # -- `_wasser_intervalle` sagt in seinem eigenen Docstring "bewusst
        # ALLE ausloser, der Fluss war physisch". AquaBloom-Pulse sind echtes
        # Wasser mit bekannter Dauer und Litermenge, haben aber keinen
        # Ventil-Kanal; sie fielen deshalb nie in den k-Fit, obwohl der Fit
        # genau die Trocknung zwischen solchen Wassergaben lernen soll.
        # Gleiche Fehlerklasse wie T-0482 (aeusseres Zonen-Gate schliesst aus,
        # was die innere Logik einschliessen will).
        #
        # Die Pump-Zonen-Pruefung kommt bewusst aus `aquabloom_job` statt als
        # eigene Bedingung hier: sie prueft vier Pflichtfelder, und eine
        # zweite Fassung davon waere genau die Sorte zweiter Wahrheit, die
        # dieser Task beseitigt (Heuristik 1 der Projekt-CLAUDE.md).
        from bewaesserung.aquabloom_job import _ist_konfiguriert
        zonen_mit_wasser = [
            z for z in self._konfig.zonen
            if z.ventil_kanal is not None or _ist_konfiguriert(z)
        ]
        if not zonen_mit_wasser:
            return
        zone_zu_standort = _zone_zu_standort(self._konfig)
        # Bulk-Read: Ventil-Events einmal fuer alle Zonen.
        alle_events = await self._speicher.hole_alle_ventil_ereignisse(
            von, jetzt,
        )
        # Wetter-Archive pro Standort cachen.
        wetter_cache: dict[str, list[WetterArchivStunde]] = {}
        for standort_id in set(zone_zu_standort.values()):
            if not standort_id:
                continue
            wetter_cache[standort_id] = (
                await self._speicher.hole_wetter_archiv(
                    standort_id, von=von, bis=jetzt,
                )
            )
        gefittet = 0
        for zone in zonen_mit_wasser:
            standort = zone_zu_standort.get(zone.zone_id)
            wetter = wetter_cache.get(standort, []) if standort else []
            zone_events = [
                e for e in alle_events if e.zone_id == zone.zone_id
            ]
            # T-0350: Laeufe der Cross-Spray-Quell-Zonen treffen diese
            # Zone physisch mit -> brechen Trockenphasen (alle ausloser).
            quellen = set(
                getattr(zone, "cross_spray_quell_zonen", []) or [],
            )
            quell_events = [
                e for e in alle_events if e.zone_id in quellen
            ]
            wartungs_fenster = await self._wartungs_fenster(zone.zone_id, jetzt)
            erfolgreich = await self._fitte_zone(
                zone_id=zone.zone_id,
                wetter=wetter,
                zone_events=zone_events,
                quell_events=quell_events,
                welkepunkt=await self._welkepunkt_fuer_zone(zone.zone_id),
                jetzt=jetzt,
                wartungs_fenster=wartungs_fenster,
            )
            if erfolgreich:
                gefittet += 1
        if gefittet:
            logger.info(
                "k_basis_fit.abgeschlossen", anzahl_gefittet=gefittet,
            )

    async def _wartungs_fenster(
        self, zone_id: str, jetzt: datetime,
    ) -> list[tuple[datetime, datetime]]:
        try:
            rows = await self._speicher.hole_wartungs_fenster(
                nur_offen=True, zone_id=zone_id,
            )
        except Exception:
            logger.exception("k_basis_fit.wartungs_fenster_fehler")
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

    async def _welkepunkt_fuer_zone(self, zone_id: str) -> float | None:
        """Vereinfachte Welkepunkt-Aufloesung fuer den Fit:
        - zone.welkepunkt (manueller Override) gewinnt
        - sonst Kalibrierungs-Median `welkepunkt_proxy`
        - sonst None (Fit nicht moeglich, Phase wird uebersprungen)

        Bewusst NICHT der vollstaendige 4-stufige Pfad aus
        `entscheidung._hole_kalibrier_referenzen` (das braucht ML-
        Service-Kontext + p10-Tagesmin-Schaetzung) -- fuer den Fit
        reichen die zwei robusten Quellen. Welkepunkt-Drift waehrend
        der Fit-Phase ist vernachlaessigbar.
        """
        zone = next(
            (z for z in self._konfig.zonen if z.zone_id == zone_id),
            None,
        )
        if zone is None:
            return None
        if zone.welkepunkt is not None:
            return float(zone.welkepunkt)
        try:
            kandidaten = await self._speicher.hole_kalibrierungen(
                zone_id=zone_id, typ="welkepunkt_proxy", limit=50,
            )
        except Exception:
            return None
        # T-0350: wert<=0 ist physikalisch sinnlos (typisch: Kalibrier-
        # Rows aus einer Sensor-ausgebaut-Phase, Realfall hecke
        # welkepunkt_proxy=0.0 -> Fit mit mae=24pp). Verwerfen statt
        # auf eine Null-Asymptote zu fitten.
        werte = [
            float(k["wert"]) for k in kandidaten
            if k.get("wert") is not None and float(k["wert"]) > 0
        ]
        if len(werte) >= 3:
            return round(statistics.median(werte), 1)
        return None

    async def _fitte_zone(
        self,
        *,
        zone_id: str,
        wetter: list[WetterArchivStunde],
        zone_events: list[VentilEreignis],
        welkepunkt: float | None,
        jetzt: datetime,
        wartungs_fenster: list[tuple[datetime, datetime]] | None = None,
        quell_events: list[VentilEreignis] | None = None,
    ) -> bool:
        if welkepunkt is None:
            logger.info(
                "k_basis_fit.skip_kein_welkepunkt",
                zone_id=zone_id,
            )
            return False
        schliessungen = [
            e for e in zone_events
            if e.aktion == VentilAktion.SCHLIESSEN and e.dauer_sekunden > 0
            and e.ausloser not in KEINE_WASSER_AUSLOESER
            and not _event_in_ausschluss(e, self._konfig, zone_id)
            and not any(
                von <= e.zeitstempel <= bis
                for von, bis in (wartungs_fenster or [])
            )
        ]
        # T-0350: Phasen-Brecher sind ALLE Wasser-Intervalle — eigene Zone
        # (jeder ausloser, auch ignoriert/unbekannt) plus Cross-Spray-
        # Quell-Zonen. Bewusst OHNE Ausschluss-/Wartungs-Filter: auch ein
        # buchhalterisch ausgeblendeter Lauf hat physisch gewaessert.
        brecher = _wasser_intervalle(
            list(zone_events) + list(quell_events or []),
        )
        # T-0350: Fenster, in denen die Event-Semantik der Zone
        # unzuverlaessig ist (events_auto_ignorieren-Regime, z. B.
        # Fremd-Nutzung des Kanals) -> Phasen darin verwerfen.
        verbots_fenster = [
            (f.von, f.bis)
            for f in (
                getattr(self._konfig, "ml_ausschluss_fenster", []) or []
            )
            if f.zone_id == zone_id and f.events_auto_ignorieren
        ]
        kandidaten = _phasen_kandidaten(
            schliessungen, brecher,
            min_dauer_h=self._physik.min_phasen_dauer_h,
            max_dauer_h=self._physik.max_phasen_dauer_h,
            verbots_fenster=verbots_fenster,
        )
        if not kandidaten:
            logger.info(
                "k_basis_fit.skip_keine_phasen",
                zone_id=zone_id,
            )
            return False
        # Pro qualifizierter Phase ein Fit.
        ks: list[float] = []
        et0_phasen: list[float] = []
        maes: list[float] = []
        for von, bis in kandidaten:
            regen = _regen_im_fenster(wetter, von, bis)
            if regen > self._physik.max_regen_im_fenster_mm:
                continue
            et0_mittel = _mittlere_et0(wetter, von, bis)
            if et0_mittel is None or et0_mittel <= 0:
                # Ohne ET0 kein Skalen-Anker; Phase verwerfen.
                continue
            messungen = await self._speicher.hole_messungen(
                zone_id, von, bis,
            )
            messungen = _filtere_messungen_fenster(
                messungen,
                self._konfig,
                zone_id,
                wartungs_fenster or [],
            )
            # `hole_messungen` sortiert DESC -> ASC drehen.
            messungen_asc = list(reversed(messungen))
            if len(messungen_asc) < 3:
                continue
            # T-0400 (b): NICHT eine gemischte Zeitreihe ueber alle Geraete
            # bauen (Gardena-Index und FYTA-VWC liegen auf verschiedenen Skalen
            # -> verfaelschtes k). Stattdessen pro Geraet fitten (mit eigener,
            # gemeinsam gefitteter Asymptote) und die k-Werte medianen. `k` ist
            # eine skalen-freie Rate, die Asymptote nicht.
            t0 = messungen_asc[0].zeitstempel
            reihen_pro_geraet: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for m in messungen_asc:
                if m.boden_feuchte is None:
                    continue
                t_rel = (m.zeitstempel - t0).total_seconds() / 3600.0
                reihen_pro_geraet[m.geraet_id].append((t_rel, float(m.boden_feuchte)))
            geraet_ks: list[float] = []
            geraet_maes: list[float] = []
            for reihe in reihen_pro_geraet.values():
                if len(reihe) < 3:
                    continue
                ergebnis = fitte_k_und_welkepunkt_phase(
                    reihe,
                    et0_mittel_mm_pro_h=et0_mittel,
                    et0_basis_mm_pro_h=self._physik.et0_basis_mm_pro_h,
                    wp_hinweis=welkepunkt,
                )
                if ergebnis is None:
                    continue
                k_basis, _wp, mae = ergebnis
                geraet_ks.append(k_basis)
                geraet_maes.append(mae)
            if not geraet_ks:
                continue
            # Kombination ueber Geraete: Median der k-Werte (robust gegen ein
            # abweichendes Geraet). Ein einzelnes Geraet -> sein k unveraendert.
            ks.append(float(statistics.median(geraet_ks)))
            et0_phasen.append(et0_mittel)
            maes.append(float(statistics.median(geraet_maes)))
        if len(ks) < self._physik.min_phasen:
            logger.info(
                "k_basis_fit.skip_zu_wenig_phasen",
                zone_id=zone_id,
                phasen_gefunden=len(ks),
                min_phasen=self._physik.min_phasen,
            )
            return False
        k_median = float(statistics.median(ks))
        et0_basis_phasen = float(statistics.mean(et0_phasen))
        mae_median = float(statistics.median(maes)) if maes else None
        await self._speicher.upsert_k_basis(
            zone_id=zone_id,
            k_basis=k_median,
            et0_basis_mm_pro_h=et0_basis_phasen,
            n_phasen=len(ks),
            mae=mae_median,
            gefittet_am=jetzt,
        )
        logger.info(
            "k_basis_fit.gefittet",
            zone_id=zone_id,
            k_basis=round(k_median, 4),
            et0_basis_mm_pro_h=round(et0_basis_phasen, 4),
            n_phasen=len(ks),
            mae_pp=round(mae_median, 2) if mae_median is not None else None,
        )
        return True
