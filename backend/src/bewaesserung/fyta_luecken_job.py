"""T-0504: Periodische FYTA-Lueckenfuellung mit dynamischem Fenster.

**Das Problem.** `fyta_client.hole_aktuelle_werte` fragt bei jedem Poll
ausschliesslich ein Zwei-Tage-Fenster ab. Ein FYTA-Sensor ohne
Funkkontakt speichert lokal weiter und laedt seinen Bestand hoch, sobald
wieder ein Hub oder ein Handy in Reichweite ist -- diese Messungen tragen
ihr ECHTES Messdatum. Liegt der Kontaktverlust laenger als zwei Tage
zurueck, fallen sie aus dem Poll-Fenster und werden nie geholt. Der
einzige Rettungsanker war bis hierher ein Neustart
(`backfill_lueckenfuellung(tage_zurueck=7)` beim Service-Start).

**Warum das der Regelfall ist, nicht der Randfall.** Sechs der FYTA-
Sensoren stehen dauerhaft ausserhalb der Hub-Reichweite und syncen nur,
wenn zufaellig ein Handy in die Naehe kommt (Memory
`fyta_sensor_offline_hub_distanz` -- akzeptierter Dauerzustand, kein
Defekt). Fuer sie liefert JEDER Sync Historie, die aelter als zwei Tage
ist. Ohne diesen Job braucht es dafuer je einen Neustart innerhalb von
sieben Tagen nach dem Sync.

**Der Fix in zwei Teilen.**
1. Periodisch statt nur beim Start -- kein Restart mehr noetig.
2. Fenster dynamisch aus dem DB-Zustand statt fix sieben Tage. Zwischen
   zwei Syncs koennen bei diesen sechs Wochen liegen; ein 7-Tage-Fenster
   waere dann zu kurz, und ein pauschal grosses Fenster zoege bei jedem
   Lauf sinnlos Tage nach, an denen nichts fehlt.

**Kostenbild.** Der zugrundeliegende Backfill fragt pro Tag EINEN
Request fuer ALLE Pflanzen zugleich. Das Fenster kostet also Requests
proportional zu seiner Breite, nicht zur Zahl der Luecken -- ein Tag
Fenster = ein Request. Bei taeglichem Lauf und 30-Tage-Deckel ist der
Worst Case 30 Requests/Tag, gegen 288 Requests/Tag des normalen Polls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import structlog

from bewaesserung.modelle import DatenQuelle
from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

# Sicherheitspuffer auf das berechnete Fenster. Zwei Gruende:
# (1) Die FYTA-API bucketet `scanFromDate`/`scanToDate` nach UTC, die DB
#     speichert Berlin-naiv (+1/+2 h). Ohne Puffer fehlt an der unteren
#     Fenstergrenze ein 2-h-Band. Verifiziert 04.08.2026: der Vergleich
#     FYTA-Tagesbucket gegen DB-Tagesbucket wich pro Tag um exakt 8
#     Messungen ab (8 x 15 min = 2 h), Summen identisch.
# (2) Die letzte bekannte Messung ist der Beginn der Luecke, nicht ihr
#     Ende -- der Tag selbst ist typischerweise unvollstaendig.
PUFFER_TAGE = 1

# Untergrenze, die IMMER gefahren wird -- auch wenn jedes Geraet gerade
# frische Werte liefert.
#
# Der Grund ist der Kernfall des Tasks, und er ist subtil: ein Sensor war
# tagelang stumm, synct seinen Speicher hoch, und der 15-min-Poll holt
# davon die letzten zwei Tage. Ab diesem Moment ist seine LETZTE Messung
# wieder taufrisch -- das Loch davor bleibt offen. Ein Fenster, das nur
# "seit wann stumm?" misst, sieht es nie wieder. Genau so gingen am
# 02.08. rund 1.300 Messungen fast verloren (T-0499); gerettet hat sie
# ein zufaelliger Restart, nicht die Logik.
#
# 7 Tage, weil der Startup-Hook denselben Wert nutzt und die Praxis ihn
# deckt: zitrus_ii lieferte am 04.08. fuenf Tage Historie am Stueck nach.
# Der Lauf ist billig genug fuer taeglich -- gemessen 4,5 s fuer drei
# Tage bei 3.970 Dubletten, der Backfill dedupliziert selbst.
BASIS_FENSTER_TAGE = 7


class FytaLueckenJob:
    """Zieht periodisch die FYTA-Historie bis zur aeltesten Luecke nach."""

    def __init__(
        self,
        speicher: Speicher,
        fyta_client,  # FytaClient -- runtime-Hint vermeidet Zirkel-Import
        intervall_stunden: int = 24,
        deckel_tage: int = 30,
    ):
        self._speicher = speicher
        self._fyta_client = fyta_client
        self._intervall = timedelta(hours=intervall_stunden)
        self._deckel_tage = deckel_tage
        self._letzte_aktualisierung: datetime | None = None
        # Diagnose fuer /api/ml/status-artige Abfragen und Tests.
        self.letzter_fehler: str | None = None
        self.letztes_fenster_tage: int | None = None

    async def bestimme_fenster_tage(
        self, jetzt: datetime | None = None,
    ) -> int:
        """Breite des Nachhol-Fensters in Tagen. 0 = nichts zu tun.

        Zwei Anteile, und beide werden gebraucht:
        - `BASIS_FENSTER_TAGE` deckt Loecher IN DER MITTE ab, die ein
          spaet synchronisierender Sensor hinterlaesst (s. dort).
        - Die gemessene Stille am ENDE deckt den Fall ab, dass ein Geraet
          laenger weg war als das Basis-Fenster -- bei den Hub-fernen
          sechs koennen zwischen zwei Syncs Wochen liegen.

        Das Maximum aus beidem, gedeckelt. Die letzte bekannte Messung
        allein waere der falsche Massstab: sie beschreibt, seit wann ein
        Geraet stumm ist, nicht, wo Daten fehlen -- und nach einem
        Teil-Sync sagt sie gar nichts mehr
        ([[fehlerpattern_benachbartes_feld_als_messwert]]).

        Massgeblich ist das KONFIGURIERTE Pflanzen-Set, nicht was in der
        DB steht: ein aus der Konfig entferntes Geraet wuerde sonst mit
        seiner uralten letzten Messung das Fenster dauerhaft bis zum
        Deckel aufreissen (Klasse
        `fehlerpattern_fallback_an_messwert_statt_konfig` -- die Frage
        "welche Geraete zaehlen?" beantwortet die Konfig, die Frage "wie
        alt sind sie?" die DB).

        Ein konfiguriertes Geraet ganz OHNE Messung zaehlt bewusst nicht
        als Luecke: das ist Erstbefuellung, nicht Lueckenfuellung, und
        wuerde bei jedem neu angelegten Sensor das Fenster auf den Deckel
        ziehen. Es wird geloggt, damit der Fall sichtbar bleibt.
        """
        jetzt = jetzt or datetime.now()
        pflanzen_map = getattr(self._fyta_client, "_pflanzen_map", None)
        if not pflanzen_map:
            return 0

        konfigurierte = {f"fyta_{fyta_id}" for fyta_id in pflanzen_map}
        letzte = await self._speicher.letzte_zeitstempel_pro_geraet(
            DatenQuelle.FYTA
        )

        ohne_messung = sorted(konfigurierte - set(letzte))
        if ohne_messung:
            logger.info(
                "fyta_luecken.geraete_ohne_messung",
                geraete=ohne_messung,
                hinweis="Erstbefuellung, nicht Lueckenfuellung",
            )

        relevant = [ts for gid, ts in letzte.items() if gid in konfigurierte]
        if not relevant:
            return 0

        aelteste = min(relevant)
        luecke_tage = (jetzt - aelteste).days
        gewuenscht = max(BASIS_FENSTER_TAGE, luecke_tage + PUFFER_TAGE)
        return min(gewuenscht, self._deckel_tage)

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> tuple[int, int]:
        """Laeuft max. einmal pro `intervall_stunden`.

        Returns: (importiert, duplikate). (0, 0) wenn nicht faellig oder
        keine Luecke. Niemals werfen -- der Aufrufer ist der
        Entscheidungsloop.
        """
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and jetzt - self._letzte_aktualisierung < self._intervall
        ):
            return 0, 0

        # Faelligkeit VOR der Arbeit stempeln: ein Fehler darf den Job
        # nicht in eine Dauerschleife schicken (Klasse
        # `fehlerpattern_teure_vorarbeit_vor_dem_gate` -- das Gate
        # entscheidet, bevor Kosten anfallen).
        self._letzte_aktualisierung = jetzt
        self.letzter_fehler = None

        try:
            fenster_tage = await self.bestimme_fenster_tage(jetzt)
        except Exception as exc:
            self.letzter_fehler = str(exc)
            logger.exception("fyta_luecken.fenster_fehler")
            return 0, 0

        self.letztes_fenster_tage = fenster_tage
        if fenster_tage <= 0:
            logger.debug("fyta_luecken.keine_luecke")
            return 0, 0

        gedeckelt = fenster_tage >= self._deckel_tage
        logger.info(
            "fyta_luecken.start",
            fenster_tage=fenster_tage,
            gedeckelt=gedeckelt,
            ab=(date.today() - timedelta(days=fenster_tage)).isoformat(),
        )
        if gedeckelt:
            # Kein stiller Beschnitt: wer laenger als der Deckel stumm
            # war, verliert den Rest endgueltig, und das gehoert ins Log.
            logger.warning(
                "fyta_luecken.deckel_erreicht",
                deckel_tage=self._deckel_tage,
                hinweis="aeltere Messungen werden NICHT mehr geholt",
            )

        from bewaesserung.fyta_backfill import backfill_lueckenfuellung

        try:
            importiert, duplikate = await backfill_lueckenfuellung(
                self._speicher, self._fyta_client, tage_zurueck=fenster_tage,
            )
        except Exception as exc:
            self.letzter_fehler = str(exc)
            logger.exception("fyta_luecken.backfill_fehler")
            return 0, 0

        if importiert:
            # Das ist die Meldung, die es bisher nicht gab: T-0499 fiel
            # nur auf, weil jemand von Hand nachrechnete.
            logger.warning(
                "fyta_luecken.nachgeholt",
                importiert=importiert,
                duplikate=duplikate,
                fenster_tage=fenster_tage,
            )
        else:
            logger.info(
                "fyta_luecken.nichts_neu",
                duplikate=duplikate, fenster_tage=fenster_tage,
            )
        return importiert, duplikate
