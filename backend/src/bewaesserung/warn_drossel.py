"""Frequenz-Drossel fuer Warnungen ueber dokumentierte Dauerzustaende (T-0540).

**Wozu.** Mehrere Detektoren melden korrekt, aber ununterbrochen: der geprueft
Zustand ist kein Ereignis, sondern eine Dauerlage, die dokumentiert und
akzeptiert ist. Gezaehlt ueber alle vorliegenden stdout-Logs:

    entscheidung.keine_messung_im_fallback_horizont   63.008
    entscheidung.sensor_festklemmend_blockiert        18.589
    entscheidung.aggregat_fenster_leer_nutze_fallback  8.578
    physik.k_basis_veraltet                            4.216   (seit T-0359 gedrosselt)

Eine Warnliste, in der zehntausende erwartete Meldungen stehen, wird nicht
gelesen -- und verdeckt damit genau die Faelle, fuer die sie gebaut wurde.

**Was die Drossel NICHT tut.** Sie unterdrueckt kein Signal, nur seine
Wiederholung. Der erste Eintritt eines Zustands wird immer gemeldet, und nach
`intervall` wieder. Wer den Zustand ganz abschalten will, braucht ein Opt-out
an der Quelle (so geloest fuer den Leck-Detektor, s. `wirkungs_alarm_aktiv`),
kein laengeres Intervall.

**Warum Prozess-Zustand und keine Persistenz.** Nach einem Neustart darf jeder
Zustand einmal melden. Das ist gewollt: ein frisch gestarteter Dienst soll
zeigen, was er vorfindet, und die Drossel soll nicht ueber Neustarts hinweg
Zustaende verschweigen, die sich in der Zwischenzeit geaendert haben.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Eine Meldung je Schluessel und Tag. 24 h, weil das bei T-0359 die Taktung des
# zugrunde liegenden Jobs war und sich als Regel bewaehrt hat.
DROSSEL_INTERVALL_DEFAULT = timedelta(hours=24)


class WarnDrossel:
    """Beantwortet: ist diese Warnung fuer diesen Schluessel jetzt wieder dran?

    Der Schluessel muss alles enthalten, was zwei Vorkommen unterscheidbar
    macht -- in aller Regel Meldungs-Key UND `zone_id`. Ein Schluessel ohne
    Zone drosselt die zweite Zone mit weg, das ist die Fehlerklasse aus
    `fehlerpattern_dedup_pro_zone_multisensor`.
    """

    def __init__(self, intervall: timedelta = DROSSEL_INTERVALL_DEFAULT) -> None:
        self._intervall = intervall
        self._letzte: dict[str, datetime] = {}

    @property
    def intervall(self) -> timedelta:
        return self._intervall

    @property
    def intervall_stunden(self) -> float:
        """Fuer das `drossel_stunden`-Feld im Log-Eintrag."""
        return self._intervall.total_seconds() / 3600

    def faellig(self, schluessel: str, jetzt: datetime) -> bool:
        """True = jetzt melden. Setzt bei True zugleich den Zeitstempel, damit
        der Aufrufer sich nicht merken muss, dass er das noch tun soll."""
        letzte = self._letzte.get(schluessel)
        if letzte is not None and (jetzt - letzte) < self._intervall:
            return False
        self._letzte[schluessel] = jetzt
        return True

    def zuruecksetzen(self) -> None:
        """Nur fuer Tests und Neustart-aehnliche Uebergaenge."""
        self._letzte.clear()
