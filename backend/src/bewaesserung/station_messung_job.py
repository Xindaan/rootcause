"""T-0505: taeglicher Abruf der gemessenen Referenz (DWD-Station via Bright Sky).

**Wofuer.** Der Mehrmodell-Mitschnitt (`wetter_modell_job`) sammelt, was die
Modelle SAGEN. Damit das auswertbar wird, braucht es daneben, was tatsaechlich
EINGETRETEN ist -- und zwar aus einer Quelle, die keines der bewerteten
Modelle ist.

**Warum nicht `wetter_archiv`.** Das waere naheliegend und waere falsch.
`wetter_archiv.py` ruft die Open-Meteo-Archive-API ohne `models`-Parameter;
der Default ist an diesem Standort `ecmwf_ifs`, nicht ERA5 (nachgemessen
05.08.2026, T-0507). Damit hat diese "Ground-Truth" zwei Probleme: sie ist
ein Modellprodukt derselben Familie, gegen die hier gemessen wird -- IFS
gegen IFS zu bewerten misst nichts --, und sie ist deutlich nasser als die
Messung: Juli 2026 am Referenzstandort 55,0 mm gegen 30,5 mm (`models=era5`)
und 25,2 mm an der Station.

**Was diese Quelle nicht kann.** Die naechste DWD-Station ist 12,1 km
entfernt (Oberkraemer-Marwitz, Stations-ID 03205). Eine Punktmessung in
dieser Entfernung verfehlt konvektive Zellen, und zwar in beide Richtungen:
am 26.07.2026 mass die Station 8,6 mm, waehrend ERA5 am Standort 1,3 mm sah.
Deshalb ist diese Reihe die PRIMAERE, aber nie die einzige Referenz -- die
Auswertung in `docs/analyse/modellvergleich_gate.py` rechnet immer auch
gegen ERA5 und meldet, wenn beide zu verschiedenen Ergebnissen kommen.

**Nachholfenster.** Bewusst nicht "seit wann sind wir stumm?" allein. Genau
daran ist T-0504 gescheitert: ein aus der letzten vorhandenen Stunde
abgeleitetes Fenster wird nach einem TEILWEISEN Nachlauf blind fuer die
Luecken davor -- die letzte Stunde ist dann jung, das Loch in der Mitte
bleibt. Deshalb faehrt hier immer ein Basis-Fenster von `BASIS_TAGE` mit,
zusaetzlich zu einer etwaigen laengeren Luecke. Der DWD liefert fuer dieselbe
Stunde ohnehin spaeter geprueftere Werte nach (`current` -> `historical`),
ein Ueberlappen ist also nicht nur unschaedlich, sondern erwuenscht.
Siehe Memory `fehlerpattern_luecke_am_ende_statt_loch_in_der_mitte`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import structlog

logger = structlog.get_logger()

BRIGHT_SKY_URL = "https://api.brightsky.dev/weather"

# Bright Sky mischt in `/weather` echte Beobachtungen und MOSMIX-VORHERSAGEN
# in dieselbe Liste; unterschieden wird das nur ueber `observation_type` in
# `sources`. Am 05.08.2026 lieferte der Endpoint fuer unseren Standort eine
# Quelle "ORANIENBURG" in 2,9 km Entfernung -- naeher als jede Messstation
# und deshalb verfuehrerisch -- mit `observation_type: forecast` und ohne
# `dwd_station_id`, mit Werten bis in den Folgetag.
#
# Die als Messung zu speichern waere der schlimmstmoegliche Fehler an genau
# dieser Stelle: die Auswertung wuerde Vorhersage gegen Vorhersage pruefen
# und jedem Modell eine Trefferquote bescheinigen, die nur die Uebereinstimmung
# zweier Modelle misst. Deshalb Positiv-Liste statt Negativ-Filter -- ein
# kuenftiger neuer Typ faellt dann heraus statt stillschweigend herein.
MESS_TYPEN = frozenset({"historical", "current", "synop"})

INTERVALL_STUNDEN = 24
BASIS_TAGE = 7          # immer mitgeholt, auch wenn die Reihe aktuell aussieht
MAX_TAGE = 90           # Deckel pro Abruf, damit wir die API nicht hammern
HTTP_TIMEOUT_S = 30.0


def _parse_stunden(daten: dict) -> list[dict]:
    """Formt die Bright-Sky-Antwort in Speicher-Zeilen um.

    Zwei Eigenheiten der API:

    - Zeitstempel kommen mit Offset ("+02:00"). Wir legen sie NAIV LOKAL ab,
      wie `wetter_vorhersage` und `wetter_archiv` -- sonst laesst sich in der
      Auswertung nicht joinen. Der Offset wird also abgeschnitten, nachdem
      er interpretiert wurde, nicht ignoriert.
    - `precipitation` darf `None` sein (Stationsausfall). Das ist NICHT 0 mm.
      Solche Stunden werden ausgelassen statt als "kein Regen" gespeichert --
      ein fehlender Messwert als Trockenheit zu buchen wuerde jedes Modell
      zu Unrecht bestrafen, das dort Regen vorhergesagt hat.
    - Vorhersage-Quellen werden verworfen (s. `MESS_TYPEN`).
    """
    quellen = {
        q["id"]: q for q in (daten.get("sources") or []) if "id" in q
    }
    out: list[dict] = []
    for eintrag in daten.get("weather") or []:
        mm = eintrag.get("precipitation")
        if mm is None:
            continue
        zeit_text = eintrag.get("timestamp")
        if not zeit_text:
            continue
        quelle = quellen.get(eintrag.get("source_id")) or {}
        typ = quelle.get("observation_type")
        if typ not in MESS_TYPEN:
            continue
        lokal = datetime.fromisoformat(zeit_text).replace(tzinfo=None)
        distanz_m = quelle.get("distance")
        out.append({
            "zeitstempel": lokal.isoformat(),
            "niederschlag_mm": float(mm),
            "station_id": quelle.get("dwd_station_id"),
            "station_name": quelle.get("station_name"),
            "distanz_km": (
                round(distanz_m / 1000.0, 1) if distanz_m is not None else None
            ),
            "beobachtungs_typ": typ,
        })
    return out


class StationMessungJob:
    """Holt Stundenmessungen der naechstgelegenen DWD-Station je Standort."""

    def __init__(
        self,
        speicher,
        standorte: list[tuple[str, float, float]],
        intervall_stunden: int = INTERVALL_STUNDEN,
        basis_tage: int = BASIS_TAGE,
    ):
        self._speicher = speicher
        self._standorte = standorte
        self._intervall = timedelta(hours=intervall_stunden)
        self._basis_tage = basis_tage
        self._letzter_lauf: datetime | None = None

    async def _hole(
        self, breite: float, laenge: float, von: datetime, bis: datetime,
    ) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                antwort = await client.get(BRIGHT_SKY_URL, params={
                    "lat": breite,
                    "lon": laenge,
                    "date": von.date().isoformat(),
                    # `last_date` markiert bei Bright Sky den BEGINN des
                    # letzten Tages, nicht dessen Ende (verifiziert 05.08.:
                    # date=X&last_date=X liefert genau eine Stunde). Um den
                    # Zieltag vollstaendig zu bekommen, einen Tag weiter.
                    "last_date": (bis + timedelta(days=1)).date().isoformat(),
                    "tz": "Europe/Berlin",
                    "units": "dwd",
                })
                antwort.raise_for_status()
                return antwort.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("station_messung.abruf_fehlgeschlagen",
                           fehler=str(exc), breite=breite, laenge=laenge)
            return None

    def _fenster(
        self, juengste: datetime | None, jetzt: datetime,
    ) -> tuple[datetime, datetime]:
        """Abrufbereich: immer Basis-Fenster, bei laengerer Luecke mehr."""
        basis_start = jetzt - timedelta(days=self._basis_tage)
        if juengste is None:
            start = jetzt - timedelta(days=MAX_TAGE)
        else:
            start = min(basis_start, juengste)
        aeltest_erlaubt = jetzt - timedelta(days=MAX_TAGE)
        return max(start, aeltest_erlaubt), jetzt

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Laeuft hoechstens alle `intervall_stunden`. Gibt Zeilen zurueck."""
        jetzt = jetzt or datetime.now()
        if self._letzter_lauf is not None:
            if jetzt - self._letzter_lauf < self._intervall:
                return 0
        # T-0563: der Anker wird weiterhin VOR der Arbeit gesetzt, und das
        # ist hier Absicht -- er ist das Rate-Limit gegen Bright Sky, nicht
        # die Erfolgsmeldung (gleiche Trennung wie T-0562 im DHS-Nachtrag).
        # Was fehlte, ist die Sichtbarkeit: die Schleife unten schluckt
        # jeden Standort-Fehler einzeln, ein Totalausfall sperrte den Job
        # also stumm fuer `intervall_stunden`. Laut
        # [[fehlerpattern_archiv_lag_als_kein_regen]] ist Bright Sky die
        # Regen-WAHRHEIT -- ein stiller 24-h-Blackout ist hier kein
        # kosmetisches Problem.
        self._letzter_lauf = jetzt

        geschrieben = 0
        fehler = 0
        for standort_id, breite, laenge in self._standorte:
            try:
                juengste = await self._speicher.juengste_station_messung(
                    standort_id,
                )
                von, bis = self._fenster(juengste, jetzt)
                daten = await self._hole(breite, laenge, von, bis)
                if not daten:
                    continue
                stunden = _parse_stunden(daten)
                n = await self._speicher.upsert_station_messung(
                    standort_id, stunden, jetzt,
                )
                geschrieben += n
                stationen = sorted({
                    s["station_name"] for s in stunden if s.get("station_name")
                })
                logger.info(
                    "station_messung.gespeichert",
                    standort=standort_id, stunden=n,
                    von=von.date().isoformat(), bis=bis.date().isoformat(),
                    summe_mm=round(
                        sum(s["niederschlag_mm"] for s in stunden), 2,
                    ),
                    stationen=stationen,
                )
            except Exception:
                fehler += 1
                logger.exception("station_messung.standort_fehler",
                                 standort=standort_id)
        if fehler and fehler == len(self._standorte):
            # Kein einziger Standort lieferte -- das ist ein Ausfall des
            # Jobs, kein Einzelfehler, und muss als solcher im Log stehen.
            logger.error(
                "station_messung.komplettausfall",
                standorte=len(self._standorte),
                naechster_versuch_in_h=self._intervall.total_seconds() / 3600,
            )
        return geschrieben
