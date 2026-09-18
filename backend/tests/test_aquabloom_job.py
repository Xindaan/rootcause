"""T-0177: Tests fuer AquabloomJob (Auto-Klassifikation aus Heuristik-Spruengen).

Deckt:
- Plausibles Fenster: UNBEKANNT-Sprung wird konvertiert
- Zu frueh (vor 0.5×Intervall): bleibt UNBEKANNT
- Zu spaet (nach 1.5×Intervall): bleibt UNBEKANNT
- Mehrere Spruenge: nur fruehester konvertiert
- Saison-Filter, Anker-Logik, Faellig-Gate
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bewaesserung.aquabloom_job import AquabloomJob, _in_saison, _ist_konfiguriert
from bewaesserung.modelle import (
    Ausloser,
    GardenaKonfig,
    GesamtKonfig,
    SpeicherKonfig,
    VentilAktion,
    VentilEreignis,
    WetterKonfig,
    WetterStandortKonfig,
    ZonenKonfig,
)
from bewaesserung.speicher import Speicher


def _run(coro):
    return asyncio.run(coro)


def _zone(
    zone_id: str = "zitrus",
    *,
    dauer_s: int | None = 600,
    intervall_h: float | None = 48.0,
    anker: datetime | None = None,
    tropfer_anzahl: int | None = 2,
    tropfer_l_h: float | None = 2.0,
    aktiv_ab: str | None = None,
    aktiv_bis: str | None = None,
) -> ZonenKonfig:
    return ZonenKonfig(
        zone_id=zone_id,
        name=zone_id,
        ventil_kanal=None,
        aquabloom_pumpen_dauer_sekunden=dauer_s,
        aquabloom_pumpen_intervall_stunden=intervall_h,
        aquabloom_anker_zeitstempel=anker,
        aquabloom_tropfer_anzahl=tropfer_anzahl,
        aquabloom_tropfer_liter_pro_stunde=tropfer_l_h,
        aquabloom_aktiv_ab=aktiv_ab,
        aquabloom_aktiv_bis=aktiv_bis,
    )


def _konfig(*zonen: ZonenKonfig) -> GesamtKonfig:
    return GesamtKonfig(
        gardena=GardenaKonfig(client_id="x", client_secret="y"),
        zonen=list(zonen),
        wetter=WetterKonfig(
            standorte=[WetterStandortKonfig(
                id="standort_a", breite=52.52, laenge=13.405,
            )],
        ),
        speicher=SpeicherKonfig(db_pfad=":memory:"),
    )


@pytest.fixture
def speicher(tmp_path):
    s = Speicher(str(tmp_path / "aqua.db"))
    _run(s.verbinden())
    yield s
    _run(s.schliessen())


def _schreibe_heuristik_paar(
    sp: Speicher, zone_id: str, t_oeffnen: datetime,
    dauer_s: int = 600,
) -> tuple[int, int]:
    """Simuliert was Sensor-Heuristik schreibt: OEFFNEN+SCHLIESSEN mit
    Ausloser=UNBEKANNT, ventil_id='sensor_heuristik'."""
    t_schliessen = t_oeffnen + timedelta(seconds=dauer_s)
    o = _run(sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=t_oeffnen, zone_id=zone_id,
        ventil_id="sensor_heuristik", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=Ausloser.UNBEKANNT, liter=None,
    )))
    s = _run(sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=t_schliessen, zone_id=zone_id,
        ventil_id="sensor_heuristik", aktion=VentilAktion.SCHLIESSEN,
        dauer_sekunden=dauer_s, ausloser=Ausloser.UNBEKANNT, liter=None,
    )))
    return o, s


def _schreibe_flanke(
    sp: Speicher, zone_id: str, t: datetime, ausloser: Ausloser,
) -> None:
    """Nur die OEFFNEN-Flanke (das ist die Basis der Takt-Inferenz)."""
    _run(sp.speichere_ventil_ereignis(VentilEreignis(
        zeitstempel=t, zone_id=zone_id,
        ventil_id="sensor_heuristik", aktion=VentilAktion.OEFFNEN,
        dauer_sekunden=0, ausloser=ausloser, liter=None,
    )))


def test_t0392_inferenz_zaehlt_konvertierte_aquabloom_flanken(speicher):
    """T-0392: Survivorship-Bias der Takt-Inferenz.

    Realer Takt 6h, Config sagt noch 12h. Die Flanken auf den 12h-Vielfachen
    wurden bereits zu AQUABLOOM konvertiert; nur die GESTRANDETEN bleiben
    UNBEKANNT. Sampelte die Inferenz (wie vorher) nur UNBEKANNT, betrug ihr
    Median-Abstand 12h -> sie bestaetigte die falsche Config dauerhaft, jeder
    zweite Puls blieb UNBEKANNT und die Wasser-Bilanz halbierte sich.
    Mit den konvertierten Flanken in der Basis kommt korrekt 6h heraus.
    """
    jetzt = datetime(2026, 6, 20, 12, 0)
    basis = jetzt - timedelta(hours=48)
    # Flanken alle 6h; die 12h-Vielfachen (k gerade) sind schon konvertiert.
    for k in range(8):
        t = basis + timedelta(hours=k * 6)
        ausloser = Ausloser.AQUABLOOM if k % 2 == 0 else Ausloser.UNBEKANNT
        _schreibe_flanke(speicher, "zitrus", t, ausloser)

    zone = _zone(intervall_h=12.0)
    job = AquabloomJob(speicher, _konfig(zone))

    gemessen = _run(job._inferiere_intervall_h(zone, jetzt, 12.0))
    assert gemessen == pytest.approx(6.0, abs=0.1), (
        f"Inferenz muss den realen 6h-Takt sehen, nicht die Config; war {gemessen}"
    )


def test_t0392_nur_unbekannte_flanken_liefern_weiter_korrekten_takt(speicher):
    """Gegenprobe: sind noch gar keine Flanken konvertiert, aendert T-0392
    nichts -- der Median der UNBEKANNT-Flanken ist weiterhin der Takt."""
    jetzt = datetime(2026, 6, 20, 12, 0)
    basis = jetzt - timedelta(hours=48)
    for k in range(8):
        _schreibe_flanke(
            speicher, "zitrus", basis + timedelta(hours=k * 6), Ausloser.UNBEKANNT,
        )

    zone = _zone(intervall_h=12.0)
    job = AquabloomJob(speicher, _konfig(zone))
    assert _run(job._inferiere_intervall_h(zone, jetzt, 12.0)) == pytest.approx(6.0, abs=0.1)


# --- Helper-Funktionen ----------------------------------------------------


def test_ist_konfiguriert_true_bei_allen_pflichtfeldern():
    assert _ist_konfiguriert(_zone()) is True


def test_ist_konfiguriert_false_bei_fehlendem_intervall():
    assert _ist_konfiguriert(_zone(intervall_h=None)) is False


def test_ist_konfiguriert_false_bei_null_tropfer():
    assert _ist_konfiguriert(_zone(tropfer_anzahl=0)) is False


def test_in_saison_ohne_grenzen_immer_true():
    z = _zone()
    assert _in_saison(z, datetime(2026, 1, 15)) is True
    assert _in_saison(z, datetime(2026, 7, 15)) is True


def test_in_saison_normale_grenzen():
    z = _zone(aktiv_ab="05-01", aktiv_bis="10-01")
    assert _in_saison(z, datetime(2026, 4, 30)) is False
    assert _in_saison(z, datetime(2026, 5, 1)) is True
    assert _in_saison(z, datetime(2026, 7, 15)) is True
    assert _in_saison(z, datetime(2026, 10, 1)) is True
    assert _in_saison(z, datetime(2026, 10, 2)) is False


# --- Plausibles Fenster: Konversion ---------------------------------------


def test_heuristik_im_plausiblen_fenster_wird_konvertiert(speicher):
    """Anker 07.05. 08:00, Intervall 48h. Plausibles Fenster:
    [09.05. 08:00, 10.05. 08:00]. Heuristik-Sprung am 09.05. 08:00 ->
    konvertiert zu AQUABLOOM mit korrekter Liter-Berechnung."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # Echter Pumpenpuls 09.05. 08:00 - Heuristik hat geschrieben
    t_puls = datetime(2026, 5, 9, 8, 0)
    o_id, s_id = _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    jetzt = datetime(2026, 5, 9, 9, 0)
    n = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert n == 1

    # Beide Events sind jetzt AQUABLOOM
    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert all(e.ausloser == Ausloser.AQUABLOOM for e in events)
    # SCHLIESSEN hat Tropfer-Liter (600/3600 * 2 * 2.0 = 0.667)
    schliessen = next(e for e in events if e.aktion == VentilAktion.SCHLIESSEN)
    assert schliessen.liter == pytest.approx(0.667, abs=0.01)
    assert schliessen.dauer_sekunden == 600


def test_f8_konversion_haelt_t0296_anker_paarbar(speicher):
    """F8: Die Konversion setzt dauer_s=600, darf aber den T-0296-Start-Anker
    (schliessen.ts - dauer ≈ oeffnen.ts) nicht brechen. Heuristik-Paar mit
    180s -> nach Konversion auf 600s muss finde_ventil_paar das Paar weiter
    finden (Zeitstempel mitgezogen). Ohne Fix driftet der Anker um 420s > 300s
    Toleranz -> Paar unauffindbar (halb-aquabloom/halb-heuristik)."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t_puls = datetime(2026, 5, 9, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls, dauer_s=180)

    n = _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 9, 9, 0)))
    assert n == 1

    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    oeffnen = next(e for e in events if e.aktion == VentilAktion.OEFFNEN)
    schliessen = next(e for e in events if e.aktion == VentilAktion.SCHLIESSEN)

    paar = _run(speicher.finde_ventil_paar(schliessen.id))
    assert set(paar) == {schliessen.id, oeffnen.id}, (
        f"Paar nach Konversion nicht mehr findbar (Anker gebrochen): {paar}"
    )


def test_heuristik_mit_drift_wird_konvertiert(speicher):
    """Real-Welt-Fall vom 11.05.2026: Pumpe driftet 46h statt 48h.
    Anker 09.05. 08:00 + Drift -> Puls 11.05. 06:00 (= 46h spaeter).
    Plausibles Fenster bei 48h-Intervall ist [+24h, +72h] = [10.05. 08:00,
    12.05. 08:00] -> 46h IST drin -> konvertiert.

    Damit toleriert die Konversion die echte AquaBloom-Pumpen-Drift.
    """
    anker = datetime(2026, 5, 9, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # 46h spaeter -> in Fenster [+24h, +72h]
    t_drift = datetime(2026, 5, 11, 6, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_drift)

    jetzt = datetime(2026, 5, 11, 12, 0)
    n = _run(job.aktualisiere_wenn_faellig(jetzt))
    assert n == 1, "46h-Drift muss im 0.5x-1.5x-Fenster konvertieren"

    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert all(e.ausloser == Ausloser.AQUABLOOM for e in events)


def test_heuristik_pumpe_pumpt_planmaessig_konvertiert(speicher):
    """48h-Intervall, Puls genau zur erwarteten Zeit."""
    anker = datetime(2026, 5, 7, 8, 22)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # Echter Puls genau 48h spaeter
    t_puls = anker + timedelta(hours=48)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    assert n == 1


# --- Zu frueh / zu spaet --------------------------------------------------


def test_heuristik_zu_frueh_bleibt_unbekannt(speicher):
    """Sprung weniger als 0.5×Intervall nach Anker -> nicht konvertieren."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # 12h nach Anker -> nur 0.25×Intervall, zu frueh
    t_puls = anker + timedelta(hours=12)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    assert n == 0
    events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert all(e.ausloser == Ausloser.UNBEKANNT for e in events)


def test_heuristik_in_zyklus_2_wird_konvertiert(speicher):
    """T-0188: Sprung im 2. Zyklus-Fenster (verpasster vorheriger Puls)
    wird trotzdem konvertiert. 80h nach Anker = 1.67×Intervall liegt im
    Zyklus-2-Fenster [72h, 120h] (= 2×Intervall ±25%).
    """
    anker = datetime(2026, 5, 1, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t_puls = anker + timedelta(hours=80)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    assert n == 1


def test_t0339_inferiert_realen_takt_und_faengt_pulse_ausserhalb_config_fensters(speicher):
    """T-0339: Config sagt 24h, Pumpe laeuft real 12h. Ohne Inferenz faellt der
    12h-versetzte Puls (Anker+12h) aus den 24h-Fenstern ([18h,30h]) -> bliebe
    UNBEKANNT (der T-0294-Miss). Mit Inferenz erkennt der Median-Abstand der
    Flanken 12h -> die Fenster liegen bei 12h -> alle drei Pulse konvertiert."""
    anker = datetime(2026, 5, 1, 8, 0)
    konfig = _konfig(_zone(intervall_h=24.0, anker=anker))  # Config 24h (falsch)
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # Reale 12h-Cadence: 3 Flanken -> 2 Abstaende a 12h -> Median 12h.
    for h in (12, 24, 36):
        _schreibe_heuristik_paar(speicher, "zitrus", anker + timedelta(hours=h))

    n = _run(job.aktualisiere_wenn_faellig(anker + timedelta(hours=37)))
    # Alle 3 konvertiert (ohne Inferenz waeren es nur 2 -- Anker+12h faellt raus).
    assert n == 3


def test_t0339_fallback_auf_config_bei_zu_wenigen_flanken(speicher):
    """T-0339: < 3 Flanken -> kein belastbarer Median -> Config-Intervall (24h)
    als Fallback. Der Anker+12h-Puls bleibt dann UNBEKANNT (wie vor T-0339)."""
    anker = datetime(2026, 5, 1, 8, 0)
    konfig = _konfig(_zone(intervall_h=24.0, anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # Nur 2 Flanken -> Inferenz greift nicht.
    _schreibe_heuristik_paar(speicher, "zitrus", anker + timedelta(hours=12))
    _schreibe_heuristik_paar(speicher, "zitrus", anker + timedelta(hours=24))

    n = _run(job.aktualisiere_wenn_faellig(anker + timedelta(hours=25)))
    # Config 24h: nur der 24h-Puls faellt ins Fenster [18h,30h]; der 12h nicht.
    assert n == 1


def test_heuristik_nach_max_zyklen_bleibt_unbekannt(speicher):
    """T-0188: Sprung weit jenseits MAX_ZYKLEN (4 × 1.25 × 48h = 240h)
    -> nicht mehr konvertieren. Pumpe ist offensichtlich seit Tagen
    ausgefallen oder Anker veraltet, User soll manuell klassifizieren.
    """
    anker = datetime(2026, 5, 1, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # 300h nach Anker = 6.25×Intervall, jenseits MAX_ZYKLEN=4×1.25
    t_puls = anker + timedelta(hours=300)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    assert n == 0


# --- Mehrere Spruenge -----------------------------------------------------


def test_mehrere_spruenge_nur_fruehester_konvertiert(speicher):
    """Bei mehreren UNBEKANNT-Sprungen im Fenster: nur frueheste konvertiert.

    Andere Spruenge (z.B. Sensor-Cadence-Folge, wo Wasser noch in den
    naechsten Messungen sichtbar wird) bleiben UNBEKANNT — der naechste
    Job-Lauf wuerde sie nicht erneut konvertieren, weil dann gibt es
    schon einen AQUABLOOM-Event und der Anker liegt nach diesen Spruengen.
    """
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t1 = datetime(2026, 5, 9, 8, 0)
    t2 = datetime(2026, 5, 9, 8, 15)  # 15 min spaeter (Sensor-Folge)
    _schreibe_heuristik_paar(speicher, "zitrus", t1)
    _schreibe_heuristik_paar(speicher, "zitrus", t2)

    n = _run(job.aktualisiere_wenn_faellig(t2 + timedelta(hours=1)))
    assert n == 1

    events = sorted(
        _run(speicher.hole_ventil_ereignisse("zitrus")),
        key=lambda e: e.zeitstempel,
    )
    # Erstes Paar konvertiert
    assert events[0].ausloser == Ausloser.AQUABLOOM  # OEFFNEN t1
    assert events[1].ausloser == Ausloser.AQUABLOOM  # SCHLIESSEN t1+600s
    # Zweites Paar bleibt unbekannt (Sensor-Cadence-Folge)
    assert events[2].ausloser == Ausloser.UNBEKANNT
    assert events[3].ausloser == Ausloser.UNBEKANNT


def test_naechster_lauf_nutzt_bereits_konvertierten_anker(speicher):
    """Nach Konversion: neuer Anker = konvertiertes SCHLIESSEN.
    Naechster echter Puls 48h spaeter wird konvertiert.
    """
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t_puls_1 = datetime(2026, 5, 9, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls_1)
    _run(job.aktualisiere_wenn_faellig(t_puls_1 + timedelta(hours=1)))

    # Naechster Puls 48h spaeter
    t_puls_2 = datetime(2026, 5, 11, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls_2)
    n = _run(job.aktualisiere_wenn_faellig(t_puls_2 + timedelta(hours=1)))
    assert n == 1


# --- Saison-Filter --------------------------------------------------------


def test_saison_filter_blockt_ausserhalb(speicher):
    """Tag ausserhalb [aktiv_ab, aktiv_bis] -> nichts konvertieren."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker, aktiv_ab="05-01", aktiv_bis="10-01"))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # April -> ausserhalb
    t_puls = datetime(2026, 4, 15, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(datetime(2026, 4, 15, 12, 0)))
    assert n == 0


# --- Anker-Logik ----------------------------------------------------------


def test_kein_anker_keine_konversion_und_warning(speicher, capsys):
    """Leere DB + leerer Konfig-Anker -> Warnung, kein Update."""
    konfig = _konfig(_zone(anker=None))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)
    # Heuristik-Sprung existiert sogar — aber ohne Anker kein Plausibilitaets-Fenster
    _schreibe_heuristik_paar(
        speicher, "zitrus", datetime(2026, 5, 9, 8, 0),
    )

    n = _run(job.aktualisiere_wenn_faellig(datetime(2026, 5, 9, 9, 0)))
    assert n == 0
    captured = capsys.readouterr()
    assert "aquabloom.kein_anker" in captured.out


def test_db_aquabloom_hat_vorrang_vor_konfig_anker(speicher):
    """basis = max(letzter_db_aquabloom_event, konfig_anker).
    Ein konvertierter SCHLIESSEN-Event wirkt als neuer Anker."""
    konfig_anker = datetime(2026, 5, 1, 8, 0)
    konfig = _konfig(_zone(anker=konfig_anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    # Erster Puls konvertiert
    t1 = datetime(2026, 5, 3, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t1)
    _run(job.aktualisiere_wenn_faellig(t1 + timedelta(hours=1)))

    # Zweiter Puls ist nur 12h nach t1 (zu frueh) — basis ist jetzt t1+600s,
    # nicht der alte Konfig-Anker
    t2 = t1 + timedelta(hours=12)
    _schreibe_heuristik_paar(speicher, "zitrus", t2)
    n = _run(job.aktualisiere_wenn_faellig(t2 + timedelta(hours=1)))
    assert n == 0, "Zweiter Sprung nur 12h nach erstem AQUABLOOM, zu frueh"


# --- Faellig-Gate ---------------------------------------------------------


def test_faellig_gate_blockt_zweiten_lauf(speicher):
    """Standard-Intervall 60 min: zweiter Lauf in derselben Stunde -> 0."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=60)

    t_puls = datetime(2026, 5, 9, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)

    jetzt1 = t_puls + timedelta(hours=1)
    n1 = _run(job.aktualisiere_wenn_faellig(jetzt1))
    n2 = _run(job.aktualisiere_wenn_faellig(jetzt1 + timedelta(minutes=30)))

    assert n1 == 1
    assert n2 == 0


# --- Teilkonfig -----------------------------------------------------------


def test_teilkonfig_nur_konfigurierte_zone(speicher):
    """Zonen ohne AquaBloom-Konfig werden ignoriert."""
    anker = datetime(2026, 5, 7, 8, 0)
    zone_aktiv = _zone(zone_id="zitrus", anker=anker)
    zone_passiv = ZonenKonfig(
        zone_id="kroton", name="Kroton", ventil_kanal=None,
    )
    konfig = _konfig(zone_aktiv, zone_passiv)
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t_puls = datetime(2026, 5, 9, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)
    _schreibe_heuristik_paar(speicher, "kroton", t_puls)

    n = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    assert n == 1

    # Zitrus konvertiert
    z_events = _run(speicher.hole_ventil_ereignisse("zitrus"))
    assert all(e.ausloser == Ausloser.AQUABLOOM for e in z_events)
    # Kroton bleibt UNBEKANNT (keine AquaBloom-Konfig)
    k_events = _run(speicher.hole_ventil_ereignisse("kroton"))
    assert all(e.ausloser == Ausloser.UNBEKANNT for e in k_events)


# --- Idempotenz -----------------------------------------------------------


def test_idempotent_zweiter_lauf_keine_doppelung(speicher):
    """Zweiter Lauf nach Konversion: nichts mehr zu tun."""
    anker = datetime(2026, 5, 7, 8, 0)
    konfig = _konfig(_zone(anker=anker))
    job = AquabloomJob(speicher, konfig, intervall_minuten=0)

    t_puls = datetime(2026, 5, 9, 8, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", t_puls)
    n1 = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=1)))
    n2 = _run(job.aktualisiere_wenn_faellig(t_puls + timedelta(hours=2)))

    assert n1 == 1
    assert n2 == 0


# --- T-0294a: Cadence-Drift-Detektor --------------------------------------


def test_cadence_drift_warnung_bei_gestrandeten_events(speicher, capsys):
    """>=2 alte, nicht-konvertierte UNBEKANNT-Morgen-Spruenge -> Warnung
    (reale Cadence wahrscheinlich kuerzer als Konfig-Intervall)."""
    zone = _zone(intervall_h=48.0)
    job = AquabloomJob(speicher, _konfig(zone), intervall_minuten=0)
    jetzt = datetime(2026, 6, 7, 12, 0)
    # 3 gestrandete UNBEKANNT-Paare, alle aelter als catchable_bis (jetzt-24h)
    # und innerhalb des Lookbacks (8 Tage). Keine Konversion -> bleiben UNBEKANNT.
    for h in (72, 56, 40):
        _schreibe_heuristik_paar(
            speicher, "zitrus", jetzt - timedelta(hours=h),
        )
    _run(job._pruefe_cadence_drift(zone, jetzt, 48.0))
    assert "aquabloom.cadence_drift_verdacht" in capsys.readouterr().out


def test_cadence_drift_keine_warnung_bei_einem_event(speicher, capsys):
    """Nur ein gestrandetes Event -> kein Drift-Verdacht (Normalfall:
    der juengste Puls ist noch pending, kein Mismatch)."""
    zone = _zone(intervall_h=48.0)
    job = AquabloomJob(speicher, _konfig(zone), intervall_minuten=0)
    jetzt = datetime(2026, 6, 7, 12, 0)
    _schreibe_heuristik_paar(speicher, "zitrus", jetzt - timedelta(hours=40))
    _run(job._pruefe_cadence_drift(zone, jetzt, 48.0))
    assert "aquabloom.cadence_drift_verdacht" not in capsys.readouterr().out


def test_cadence_drift_throttle_1x_pro_24h(speicher, capsys):
    """Zweiter Aufruf binnen 24h -> keine erneute Warnung (Throttle)."""
    zone = _zone(intervall_h=48.0)
    job = AquabloomJob(speicher, _konfig(zone), intervall_minuten=0)
    jetzt = datetime(2026, 6, 7, 12, 0)
    for h in (72, 56, 40):
        _schreibe_heuristik_paar(
            speicher, "zitrus", jetzt - timedelta(hours=h),
        )
    _run(job._pruefe_cadence_drift(zone, jetzt, 48.0))
    assert "aquabloom.cadence_drift_verdacht" in capsys.readouterr().out
    # 1 h spaeter erneut: gedrosselt.
    _run(job._pruefe_cadence_drift(zone, jetzt + timedelta(hours=1), 48.0))
    assert "aquabloom.cadence_drift_verdacht" not in capsys.readouterr().out
