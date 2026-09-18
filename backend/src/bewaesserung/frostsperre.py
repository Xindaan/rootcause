"""T-0611: Frostsperre fuer die Auto-Schleife.

Andres Entscheid 29.09.2026: kein automatisches Giessen, solange die
stuendliche Vorhersage (Lufttemperatur, 2 m) im Zyklusfenster unter
`frostsperre_schwelle_c` (Default 2 °C) liegt. Kein Durchbruch bei kritischer
Trockenheit -- die Sperre steht ueber dem Kritisch-Bypass und sitzt deshalb
NICHT in `giessfenster.bewerte_giessfenster` (dort ueberstimmt ihn der
Bypass, der bei allen drei Aufrufern ausserhalb liegt).

**Fenster.** Gesperrt ist ein Start, wenn die Vorhersage fuer die laufende
Stunde oder irgendeine Stunde bis Zyklusende + 1 h unter der Schwelle liegt.
Open-Meteo liefert Momentanwerte zur vollen Stunde; der Slot `floor(jetzt)`
und der Nachlauf von einer Stunde klammern das Zyklusintervall deshalb von
beiden Seiten ein. Ein 12-h-Horizont wie beim Frost-Ereignis
(`wetter_ereignisse.py`) wuerde im Herbst jeden Nachmittagslauf vor einer
kalten Nacht sperren.

**Fail-safe.** Fehlt die Vorhersage fuer das Fenster (Abruf gescheitert,
leere Liste, Luecke), wird gesperrt und der Grund genannt. Eine Luecke von
genau zwei Stunden ist erlaubt: an der Zeitumstellung im Fruehjahr fehlt die
lokale Stunde 02:00, ohne das waere jede Nacht im Maerz einmal gesperrt.
**Nur Oktober bis April** (Andres Entscheid zum Gate W1, B4): von Mai bis
September sperrt eine fehlende Vorhersage nicht; vorhandene Stunden unter
der Schwelle sperren weiterhin (Spaetfrost im Mai). Der Befund traegt
`vorhersage_fehlt`, damit UI und Push nicht "Frost" melden, wenn der Grund
die fehlende Vorhersage ist.

Reine Bewertung (`bewerte_frost`) plus ein duenner asynchroner Pruefer
(`FrostPruefer`), den Entscheidungsmotor und VentilSicherung teilen -- eine
Wahrheit fuer Entscheidungslog und Ventil.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

import structlog

from bewaesserung.kanal_zustand import pre_soak_ende_s, pre_soak_policy_aktiv

logger = structlog.get_logger()

#: Nachlauf hinter dem Zyklusende (Setzung im Task T-0611).
NACHLAUF = timedelta(hours=1)

#: Groesste zulaessige Luecke zwischen zwei Vorhersage-Stunden (Zeitumstellung).
MAX_LUECKE = timedelta(hours=2)

#: Monate, in denen eine fehlende Vorhersage sperrt (Andres Entscheid zum
#: Gate W1, B4: Oktober bis April). Mai bis September: kein Fail-safe.
FAILSAFE_MONATE = frozenset({10, 11, 12, 1, 2, 3, 4})


def failsafe_saison(jetzt: datetime) -> bool:
    """Sperrt eine fehlende Vorhersage in diesem Monat (Ortszeit)?"""
    return jetzt.month in FAILSAFE_MONATE


@dataclass(frozen=True)
class FrostBefund:
    """Ergebnis der Frostpruefung fuer ein Zyklusfenster."""

    gesperrt: bool
    #: Klartext fuer Entscheidungslog und UI, z. B. "Frostsperre: 1,4 °C um 06:00".
    grund: str | None = None
    #: Kaelteste Stunde im Fenster (None, wenn keine Vorhersage vorlag).
    min_temperatur_c: float | None = None
    zeitpunkt: datetime | None = None
    #: Gate W1 B4: Vorhersage fehlt/unvollstaendig. Mit `gesperrt` ist das
    #: die Fail-safe-Sperre (Oktober bis April), ohne `gesperrt` nur Info.
    vorhersage_fehlt: bool = False


def _naiv(wert: datetime) -> datetime:
    return wert.replace(tzinfo=None) if wert.tzinfo is not None else wert


def _zahl(wert: float) -> str:
    return f"{wert:.1f}".replace(".", ",")


def bewerte_frost(
    stunden: Iterable[Any], jetzt: datetime, zyklus_s: int, schwelle_c: float,
) -> FrostBefund:
    """Bewertet die stuendliche Vorhersage fuer [floor(jetzt), jetzt+Zyklus+1 h].

    `stunden`: Objekte mit `zeitstempel` (naiv, Ortszeit, Stundenanfang) und
    `temperatur` (°C), also `WetterStunde`. Eine Stunde ohne Temperatur
    (`None`, Gate W1 B9) zaehlt als fehlend (Vorhersage unvollstaendig).

    Reihenfolge: liegt eine vorhandene Stunde unter der Schwelle, ist der
    Grund Frost (auch bei Luecken). Sonst sperrt eine Luecke nur in der
    Fail-safe-Saison (`FAILSAFE_MONATE`).
    """
    jetzt = _naiv(jetzt)
    von = jetzt.replace(minute=0, second=0, microsecond=0)
    bis = jetzt + timedelta(seconds=max(int(zyklus_s or 0), 0)) + NACHLAUF
    im_fenster = sorted(
        (
            (_naiv(s.zeitstempel), float(s.temperatur))
            for s in stunden
            if von <= _naiv(s.zeitstempel) <= bis
            and s.temperatur is not None   # Gate W1 B9: fehlt, nicht 0 °C
        ),
        key=lambda paar: paar[0],
    )
    luecke = _luecke_im_fenster(im_fenster, von, bis)
    if im_fenster:
        zeitpunkt, kaelteste = min(im_fenster, key=lambda paar: paar[1])
        if kaelteste < schwelle_c:
            return FrostBefund(
                gesperrt=True,
                grund=f"Frostsperre: {_zahl(kaelteste)} °C um {zeitpunkt:%H:%M}",
                min_temperatur_c=kaelteste,
                zeitpunkt=zeitpunkt,
            )
    else:
        zeitpunkt, kaelteste = None, None
    if luecke is not None:
        if failsafe_saison(jetzt):
            return FrostBefund(
                gesperrt=True, grund=f"Frostsperre: {luecke} (fail-safe)",
                vorhersage_fehlt=True,
            )
        return FrostBefund(
            gesperrt=False,
            grund=f"Vorhersage unvollstaendig ({luecke}), Mai bis September "
                  "keine Fail-safe-Sperre",
            min_temperatur_c=kaelteste, zeitpunkt=zeitpunkt,
            vorhersage_fehlt=True,
        )
    return FrostBefund(
        gesperrt=False, min_temperatur_c=kaelteste, zeitpunkt=zeitpunkt,
    )


def _luecke_im_fenster(
    im_fenster: list[tuple[datetime, float]], von: datetime, bis: datetime,
) -> str | None:
    """Beschreibung der ersten Vorhersage-Luecke im Fenster, sonst None."""
    if not im_fenster or im_fenster[0][0] != von:
        return f"keine Vorhersage fuer {von:%H:%M}"
    for (t_a, _), (t_b, _) in zip(im_fenster, im_fenster[1:]):
        if t_b - t_a > MAX_LUECKE:
            return f"Vorhersage-Luecke nach {t_a:%H:%M}"
    letzte_noetige = bis.replace(minute=0, second=0, microsecond=0)
    if im_fenster[-1][0] < letzte_noetige:
        return f"keine Vorhersage bis {letzte_noetige:%H:%M}"
    return None


def zyklus_dauer_s(zone: Any, haupt_s: int) -> int:
    """Dauer des automatischen Zyklus, den der Auto-Loop fuer `zone` faehrt.

    Mit Pre-Soak-Policy (main.py) ist das die ganze Sequenz bis zum letzten
    Haupt-Puls -- dieselbe Rechnung wie `PreSoakManager.starte` mit den
    Werten, die der Auto-Loop uebergibt. Sonst die Einzeldose.
    """
    haupt_s = max(int(haupt_s or 0), 0)
    if not pre_soak_policy_aktiv(zone):
        return haupt_s
    haupt_min = max(1, (haupt_s + 59) // 60)
    return pre_soak_ende_s(
        int(zone.pre_soak_pause_min) * 60,
        haupt_min * 60,
        int(getattr(zone, "haupt_pulse", 1) or 1),
        int(getattr(zone, "haupt_puls_pause_min", 0) or 0) * 60,
    )


class FrostPruefer:
    """Frostpruefung je Zone ueber deren Wetter-Standort.

    `wetter_fuer_zone(zone_id)` liefert den WetterClient der Zone (dieselbe
    Zuordnung wie das Regen-Gate: `standorte[].wetter_standort`). Liegen die
    Zonen eines Kanals an mehreren Standorten, sperrt jeder einzelne;
    ohne Sperre meldet der Befund den kaeltesten Standort.
    """

    def __init__(
        self,
        wetter_fuer_zone: Callable[[str], Any],
        schwelle_c: float = 2.0,
        jetzt_func: Callable[[], datetime] = datetime.now,
    ):
        self._wetter_fuer_zone = wetter_fuer_zone
        self._schwelle_c = float(schwelle_c)
        self._jetzt = jetzt_func
        # Gate W1 B4: juengste Sperre je Zonenmenge (zone_ids) -> (Befund,
        # Zeitpunkt). Ein freier Befund fuer dieselbe Menge raeumt sie. Der
        # Watchdog meldet daraus gedrosselt per Push (Trigger K).
        self._sperren: dict[frozenset, tuple[FrostBefund, datetime]] = {}
        logger.info(
            "frostsperre.aktiv",
            schwelle_c=self._schwelle_c,
            failsafe_monate=sorted(FAILSAFE_MONATE),
        )

    @property
    def schwelle_c(self) -> float:
        return self._schwelle_c

    @property
    def letzte_sperre(self) -> tuple[FrostBefund, datetime] | None:
        """Gate W1 B4: juengste noch nicht durch einen freien Befund
        geraeumte Sperre (Befund, Zeitpunkt der Pruefung), sonst None."""
        if not self._sperren:
            return None
        return max(self._sperren.values(), key=lambda paar: paar[1])

    async def pruefe(
        self, zone_ids: Iterable[str], zyklus_s: int,
        jetzt: datetime | None = None,
    ) -> FrostBefund:
        jetzt = jetzt or self._jetzt()
        zone_ids = list(zone_ids)
        befund = await self._pruefe(zone_ids, zyklus_s, jetzt)
        schluessel = frozenset(zone_ids)
        if befund.gesperrt:
            self._sperren[schluessel] = (befund, jetzt)
        else:
            self._sperren.pop(schluessel, None)
        return befund

    async def vorhersagen_laden(self, zone_ids: Iterable[str]) -> None:
        """T-0610 E14: Vorhersagen der Standorte dieser Zonen in den Cache
        der WetterClients holen, ohne zu bewerten.

        Der Pre-Soak-Tick ruft das VOR seinem Lock auf; die Frostpruefung in
        `VentilSicherung.bewaessere` unter dem Lock trifft danach den Cache
        (Erfolg 30 min, Fehler 2 min Negativ-Cache) statt bis zu 10 s je
        Standort auf Open-Meteo zu warten. Bewertet und gesperrt wird weiter
        nur dort -- hier entsteht kein Befund und keine Sperre. Fehler
        werden nur geloggt: die eigentliche Pruefung sperrt fail-safe.
        """
        clients: dict[int, Any] = {}
        for zid in zone_ids:
            client = self._wetter_fuer_zone(zid)
            if client is not None:
                clients.setdefault(id(client), client)
        for client in clients.values():
            try:
                await client.hole_vorhersage()
            except Exception:
                logger.exception(
                    "frostsperre.vorladen_fehler",
                    standort=getattr(client, "standort_id", None),
                )

    def _fehlt(self, jetzt: datetime, grund: str) -> FrostBefund:
        """Fail-safe-Befund fuer eine fehlende Vorhersage: sperrt nur in
        der Fail-safe-Saison (Oktober bis April)."""
        if failsafe_saison(_naiv(jetzt)):
            return FrostBefund(
                gesperrt=True, grund=f"Frostsperre: {grund} (fail-safe)",
                vorhersage_fehlt=True,
            )
        return FrostBefund(
            gesperrt=False,
            grund=f"{grund}, Mai bis September keine Fail-safe-Sperre",
            vorhersage_fehlt=True,
        )

    async def _pruefe(
        self, zone_ids: list[str], zyklus_s: int, jetzt: datetime,
    ) -> FrostBefund:
        clients: dict[int, Any] = {}
        for zid in zone_ids:
            client = self._wetter_fuer_zone(zid)
            clients.setdefault(id(client), client)
        if not clients:
            return self._fehlt(jetzt, "keine Zone")
        frei: list[FrostBefund] = []
        for client in clients.values():
            try:
                vorhersage = await client.hole_vorhersage()
                befund = bewerte_frost(
                    vorhersage.stunden, jetzt, zyklus_s, self._schwelle_c,
                )
            except Exception:
                logger.exception(
                    "frostsperre.vorhersage_fehler",
                    standort=getattr(client, "standort_id", None),
                )
                befund = self._fehlt(jetzt, "Vorhersage nicht auswertbar")
            if befund.gesperrt:
                return befund
            frei.append(befund)
        return min(
            frei,
            key=lambda b: (
                b.min_temperatur_c if b.min_temperatur_c is not None
                else float("inf")
            ),
        )
