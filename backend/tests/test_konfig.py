"""Tests fuer Konfig-Loader.

Regression-Sicherung gegen das Whitelist-Antipattern in `_parse_zonen()`:
neue ZonenKonfig-Felder muessen explizit im Parser aufgefuehrt werden,
sonst kommen sie nie aus der YAML durch. Dieser Test stellt sicher, dass
die ausgelieferte config/default.example.yaml alle relevanten Felder befuellt.
(Die Live-config/default.yaml ist nicht im Repo -- getestet wird die
mitgelieferte Beispiel-Konfig, die dieselbe Struktur/Tuning traegt.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bewaesserung.konfig import lade_konfig


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "default.example.yaml"


def test_default_config_laedt_ohne_fehler():
    """Smoke-Test: ausgelieferte Default-Config ist valide."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    assert konfig.zonen, "mindestens eine Zone erwartet"


def test_tages_budget_liter_wird_nicht_als_sekunden_akzeptiert(tmp_path):
    """Alte Liter-Semantik darf nicht stumm als Sekundenbudget laufen."""
    cfg_path = tmp_path / "legacy_budget.yaml"
    cfg_path.write_text(
        """
gardena:
  client_id: x
  client_secret: y
zonen:
  - zone_id: z1
    name: Z1
    modus: automatik
    feuchte_schwelle_min: 30
    feuchte_schwelle_max: 60
    tages_budget_liter: 12
wetter:
  standorte:
    - id: berlin
      breite: 52.52
      laenge: 13.40
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tages_budget_liter"):
        lade_konfig(cfg_path)


def test_geraet_kanal_liter_pro_minute_wird_geladen():
    """Multi-DSWC-Raten muessen device-spezifisch aus YAML kommen."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    raten = konfig.bilanz.geraet_kanal_liter_pro_minute
    assert raten["11111111-1111-1111-1111-111111111111"][2] == 1.87
    assert raten["22222222-2222-2222-2222-222222222222"][2] == 1.4


def test_ventil_name_override_wird_geladen():
    """ventil_name aus YAML muss durch _parse_zonen() durchkommen.

    Hintergrund: ein Ventil-Kanal am Dual Water Control kann in der
    Gardena-App anders heissen als die Dashboard-Zone. Ohne ventil_name-
    Override matcht das Live-Callback-Routing nicht und Events landen mit
    valve_id als zone_id.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    assert nach_id["garten_mikrodrip"].ventil_name == "Mikrodrip"


def test_ventilkanal_belegung_konsistent():
    """Die dokumentierte Kanal-Belegung in der Config stimmt."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Kanal 1 = Wiese Sprinkler, Kanal 2 = Mikrodrip-Kette
    assert nach_id["wiese_sprinkler"].ventil_kanal == 1
    assert nach_id["garten_mikrodrip"].ventil_kanal == 2
    assert nach_id["garten_mikrodrip_b"].ventil_kanal == 2


def test_auto_loop_opt_in_wird_aus_yaml_durchgereicht():
    """T-0334: Pro-Strang-Opt-In fuer den Auto-Loop muss aus der YAML kommen
    UND die Opt-out-Garantie der anderen automatik-Zonen muss halten.

    Sicherheitsversprechen: globales Scharfschalten (ventilsteuerung_aktiv=true)
    darf NUR Bambus-Strang + Hecke feuern lassen, nicht waldblumenhain.
    Whitelist-Drift-Regression (Memory: fehlerpattern_config_whitelist) -- ohne
    Durchreichen faellt das Feld stumm auf False, dann wuerde der Bambus-Strang
    NIE giessen (anderer Fehler, gleiche Klasse).
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Opt-in: Mikrodrip-Strang + Tropf-Beet (beide autonom scharf).
    assert nach_id["garten_mikrodrip"].auto_loop_opt_in is True
    assert nach_id["garten_mikrodrip_b"].auto_loop_opt_in is True
    assert nach_id["beet_tropf"].auto_loop_opt_in is True
    # Opt-out-Garantie: nicht-scharfe automatik-Zonen bleiben Shadow, auch
    # nachdem das globale Flag scharf ist. Default False greift hier.
    assert nach_id["wiese_sprinkler"].auto_loop_opt_in is False
    # Pre-Soak-Policy-Realitaet -- alle scharfen Zonen giessen als Pre-Soak
    # (Vorbenetzung gg Hydrophobie); nicht-scharfe bleiben Pydantic-Default
    # "nie". Whitelist-Drift-Regression auch fuer pre_soak_modus/min.
    assert nach_id["garten_mikrodrip"].pre_soak_modus == "immer"
    assert nach_id["garten_mikrodrip"].pre_soak_min == 5
    assert nach_id["garten_mikrodrip_b"].pre_soak_modus == "immer"
    assert nach_id["beet_tropf"].pre_soak_modus == "immer"
    assert nach_id["wiese_sprinkler"].pre_soak_modus == "nie"
    # Empirisch kalibrierte Wirkungsrate (sonst Default 1.0 -> Auto-Dose
    # 4x zu kurz). Whitelist-Drift-Regression fuer delta_pp_pro_minute.
    assert nach_id["beet_tropf"].delta_pp_pro_minute == 0.33


def test_pre_soak_modus_wird_aus_yaml_durchgereicht(tmp_path):
    """T-0336: pre_soak_modus muss aus YAML durchkommen (Whitelist-Drift,
    Memory fehlerpattern_config_whitelist). Default 'nie' = Einzellauf."""
    cfg_path = tmp_path / "ps_modus.yaml"
    cfg_path.write_text(
        """
gardena:
  client_id: x
  client_secret: y
zonen:
  - zone_id: sprink
    name: Sprink
    modus: automatik
    pre_soak_modus: immer
    pre_soak_min: 5
  - zone_id: standard
    name: Standard
    modus: automatik
wetter:
  standorte:
    - id: berlin
      breite: 52.52
      laenge: 13.40
""",
        encoding="utf-8",
    )
    konfig = lade_konfig(cfg_path)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    assert nach_id["sprink"].pre_soak_modus == "immer"     # aus YAML
    assert nach_id["standard"].pre_soak_modus == "nie"     # Pydantic-Default


def test_optimum_feuchte_wird_aus_yaml_durchgereicht():
    """optimum_feuchte_min/max muss aus der YAML im ZonenKonfig landen.

    Regression fuer den Whitelist-Drift-Bug vom 2026-04-20: ZonenKonfig hatte
    das Feld, _parse_zonen() reichte es nicht durch, API lieferte None trotz
    gesetzter YAML-Werte — SchwellenRange-Widget zeigte deshalb kein
    Pflanzen-Optimum fuer Waldblumenhain und die beiden Bambusse.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # YAML-Werte: Mikrodrip 60/75, Wiese Sprinkler 40/60
    assert nach_id["wiese_sprinkler"].optimum_feuchte_min == 40
    assert nach_id["wiese_sprinkler"].optimum_feuchte_max == 60
    assert nach_id["garten_mikrodrip"].optimum_feuchte_min == 60
    assert nach_id["garten_mikrodrip"].optimum_feuchte_max == 75
    assert nach_id["garten_mikrodrip_b"].optimum_feuchte_min == 60
    assert nach_id["garten_mikrodrip_b"].optimum_feuchte_max == 75


def test_backup_konfig_wird_geladen():
    """Backup-Block muss durchs YAML-Parsing kommen (gleiches Whitelist-Risiko
    wie bei ml_ausschluss_fenster). Verzeichnis wird projekt-relativ aufgeloest.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    b = konfig.backup
    assert b.aktiv is True
    assert b.intervall_stunden == 24
    assert b.retention_taeglich_tage == 14
    assert b.monatlich_aktiv is True
    # verzeichnis wird absolut unter backend/daten/backup aufgeloest
    assert b.verzeichnis.endswith("/backend/daten/backup")
    assert Path(b.verzeichnis).is_absolute()


def test_ml_retrain_konfig_wird_geladen():
    """T-0048: ml.retrain-Block muss durchs YAML-Parsing kommen (gleiches
    Whitelist-Risiko). Seit 2026-04-19 scharfgeschaltet, bleibt Struktur-Test.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    r = konfig.ml_retrain
    assert isinstance(r.aktiv, bool)
    # B-Action 29.04. (Drift-Periode aktiv): 7 -> 3, gate 0.99 -> 1.5,
    # start_verzoegerung 30 -> 5. Zurueck auf 7/0.99/30 wenn 7 Tage stabil
    # gruen.
    assert r.intervall_tage == 3
    assert r.trainings_fenster_tage == 60
    assert r.gate_faktor == 1.5
    assert r.folds == 3
    assert r.quantile is True
    # ausgabe_pfad leer → Job nutzt konfig.ML_DATEN_PFAD zur Laufzeit
    assert r.ausgabe_pfad == ""


def test_bewaesserungs_response_konfig_wird_geladen():
    """T-0065: ml.bewaesserungs_response-Block muss durchs YAML-Parsing kommen.

    Regression gegen das Whitelist-Antipattern (siehe fehlerpattern_config_whitelist.md):
    neues Feld an GesamtKonfig wird stumm ignoriert, wenn der Parser es nicht
    explizit aus dem YAML in die Pydantic-Instanz durchreicht. Dann liegt
    _berechne_dauer unveraendert auf der Heuristik, obwohl die YAML
    aktiv/wirksam gesetzt hat — und niemand merkt es.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    r = konfig.ml_bewaesserungs_response
    # Werte aus default.yaml — seit T-0068 Shadow-Aktivierung:
    # aktiv=true (loggt Vorschlaege), wirksam=false (Heuristik bleibt produktiv),
    # min_events=3 (kleine Eventbasis bei 3-Zonen-Fleet, Initial-Training).
    assert r.aktiv is True
    assert r.wirksam is False
    assert r.min_events == 3
    assert r.retrain_intervall_tage == 14
    assert r.retrain_event_schwelle == 5
    assert r.gate_mae_faktor == 0.5
    # ausgabe_pfad leer → Service nutzt ML_DATEN_PFAD/response zur Laufzeit
    assert r.ausgabe_pfad == ""


def test_versickerungs_karenz_stunden_wird_aus_yaml_durchgereicht():
    """T-0071: versickerungs_karenz_stunden-Override muss durch _parse_zonen.

    Regression gegen das Whitelist-Antipattern (siehe
    fehlerpattern_config_whitelist.md): Ohne explizite Durchreiche im
    Parser wuerde der YAML-Waldblumenhain-Override=6 stumm auf Default=3
    fallen, und die Sensor-Heuristik schreibt weiter Phantom-Events.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Wiese Sprinkler hat Override=6 (Sandboden + Sprinkler-Nachlauf).
    assert nach_id["wiese_sprinkler"].versickerungs_karenz_stunden == 6
    # Mikrodrip-Zonen ohne Override -> Default 3.
    assert nach_id["garten_mikrodrip"].versickerungs_karenz_stunden == 3
    assert nach_id["garten_mikrodrip_b"].versickerungs_karenz_stunden == 3


def test_welkepunkt_und_sicherheits_tage_durchgereicht():
    """T-0075: welkepunkt + sicherheits_tage muessen aus YAML durch
    _parse_zonen kommen.

    Update 03.05.: Manuelle welkepunkt-Overrides fuer alle drei
    Outdoor-Zonen (Audit zeigte tagesmin_schaetzung-Drift mit
    falsch-akut-Triggern).
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Manuelle Welkepunkt-Overrides
    assert nach_id["garten_mikrodrip"].welkepunkt == 40.0
    assert nach_id["garten_mikrodrip_b"].welkepunkt == 40.0
    assert nach_id["wiese_sprinkler"].welkepunkt == 32.0
    # Wiese-Sprinkler-Override
    assert nach_id["wiese_sprinkler"].sicherheits_tage == 1.5
    # Mikrodrip-Defaults
    assert nach_id["garten_mikrodrip"].sicherheits_tage == 3.0
    assert nach_id["garten_mikrodrip_b"].sicherheits_tage == 3.0


def test_delta_pp_pro_minute_durchgereicht():
    """T-0089: pro-Zone-Wirkungsrate optionales Override-Feld in
    ZonenKonfig. Whitelist-Pattern wie fehlerpattern_config_whitelist.md.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Mikrodrip-Zonen: kein Override (Auto-Kalibrierung wuerde hier greifen).
    assert nach_id["garten_mikrodrip"].delta_pp_pro_minute is None
    assert nach_id["garten_mikrodrip_b"].delta_pp_pro_minute is None
    # Wiese Sprinkler: Konfig-Override beibehalten (Dauer-Abhaengigkeit).
    assert nach_id["wiese_sprinkler"].delta_pp_pro_minute == 0.17
    # FYTA-Zonen ohne Override -> None.
    for zid in ("topf_1", "topf_3", "topf_5", "topf_6"):
        assert nach_id[zid].delta_pp_pro_minute is None, (
            f"{zid}: kein YAML-Override -> sollte None sein"
        )


def test_bewaesserungs_strategie_durchgereicht():
    """T-0103: `bewaesserungs_strategie` muss aus YAML durch
    `_parse_zonen` kommen. Whitelist-Pattern.
    """
    from bewaesserung.modelle import BewaesserungsStrategie
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Mikrodrip: explizit HAEUFIG_KLEIN gesetzt
    assert nach_id["garten_mikrodrip"].bewaesserungs_strategie == \
        BewaesserungsStrategie.HAEUFIG_KLEIN
    assert nach_id["garten_mikrodrip_b"].bewaesserungs_strategie == \
        BewaesserungsStrategie.HAEUFIG_KLEIN
    # Wiese Sprinkler: explizit SELTEN_GROSS (Etablierungsphase,
    # Graeser-Konkurrenz).
    assert nach_id["wiese_sprinkler"].bewaesserungs_strategie == \
        BewaesserungsStrategie.SELTEN_GROSS
    # FYTA-Zonen: botanisch recherchierte Strategien.
    # Trocken-Nass-Tiefwurzler (Citrus): SELTEN_GROSS.
    for zid in ("topf_1", "topf_2"):
        assert nach_id[zid].bewaesserungs_strategie == \
            BewaesserungsStrategie.SELTEN_GROSS, zid
    # Kontinuierlich feucht (engerer Korridor, Schwankungen kritisch):
    # HAEUFIG_KLEIN.
    for zid in ("topf_7", "topf_6"):
        assert nach_id[zid].bewaesserungs_strategie == \
            BewaesserungsStrategie.HAEUFIG_KLEIN, zid
    # Mediterrane Topfpflanzen + Avocado (Phytophthora-Schutz) ->
    # SELTEN_GROSS (Trockenphasen erwuenscht).
    for zid in ("topf_3", "topf_4", "topf_5"):
        assert nach_id[zid].bewaesserungs_strategie == \
            BewaesserungsStrategie.SELTEN_GROSS, zid
    # Mischkasten: dominiert von einer verdunstungsstarken Art ->
    # HAEUFIG_KLEIN, die Mitbewohner tolerieren das.
    assert nach_id["topf_9"].bewaesserungs_strategie == \
        BewaesserungsStrategie.HAEUFIG_KLEIN
    # Pilea ist sukkulent-tendiert, Wurzelfaeule >> Welken.
    # KONSTANT_NIEDRIG triggert erst nahe Welkepunkt, statt
    # KORRIDOR-praeventiv-Logik.
    assert nach_id["topf_8"].bewaesserungs_strategie == \
        BewaesserungsStrategie.KONSTANT_NIEDRIG
    # Frisch gepflanztes Gehoelz in der Anwachsphase -> KORRIDOR
    # (Zielband halten, weder zu trocken noch zu nass).
    assert nach_id["beet_tropf"].bewaesserungs_strategie == \
        BewaesserungsStrategie.KORRIDOR


def test_cluster_id_und_ml_retrain_pro_zone_durchgereicht():
    """T-0082: `cluster_id` ist optional pro Zone (default None →
    cluster == zone_id). `ml.retrain.cluster_strategie` und
    `mindest_zeilen_pro_cluster` muessen via Whitelist durch.
    Whitelist-Pattern wie in fehlerpattern_config_whitelist.md.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    # Default: keine Zone hat cluster_id-Override gesetzt — alle None.
    fuer_zid = {z.zone_id: z for z in konfig.zonen}
    for zid in ("garten_mikrodrip", "wiese_sprinkler", "topf_1"):
        assert fuer_zid[zid].cluster_id is None, (
            f"{zid}: cluster_id sollte None sein (default = zone_id)"
        )
    # Seit 2026-04-27 (T-0082-Aktivierung) lebt `pro_zone` produktiv.
    # `global` bleibt fuer Tests + manuellen Rollback erreichbar.
    assert konfig.ml_retrain.cluster_strategie == "pro_zone"
    # T-0160 (07.05.2026): Schwelle von 1500 auf 800 gesenkt, weil bei
    # Gardena-Cadence ~1×/h ein 60-Tage-Fenster maximal 1440 Zeilen
    # liefert (real ~1150). 1500 war strukturell unerreichbar fuer
    # Outdoor-Zonen, Auto-Retrain hat sie still uebersprungen.
    assert konfig.ml_retrain.mindest_zeilen_pro_cluster == 800
    assert konfig.ml_retrain.gate_faktor_pro_cluster == {}


def test_bambus_schwellen_auf_optimum_gezogen():
    """Mikrodrip-Schwellen sollen deckungsgleich mit dem Pflanzen-Optimum
    bleiben. Wenn jemand das aus Versehen hochzieht, schlaegt dieser
    Test an.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    for zid in ("garten_mikrodrip", "garten_mikrodrip_b"):
        assert nach_id[zid].feuchte_schwelle_min == 60, (
            f"{zid}: Schwelle-min sollte 60 sein (Optimum-min), nicht "
            f"{nach_id[zid].feuchte_schwelle_min}"
        )
        assert nach_id[zid].feuchte_schwelle_max == 75


def test_ml_ausschluss_fenster_wird_geladen():
    """Sensor-Umzug-Marker muss durchs YAML-Parsing kommen.

    Gleiches Whitelist-Antipattern-Risiko wie bei ZonenKonfig: GesamtKonfig
    listet Felder einzeln im lade_konfig-Return auf. Dieser Test verhindert,
    dass ml_ausschluss_fenster stumm auf [] faellt.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    assert konfig.ml_ausschluss_fenster, "ml_ausschluss_fenster leer"
    fenster = konfig.ml_ausschluss_fenster[0]
    assert fenster.zone_id == "wiese_sprinkler"
    # Sensor-Umzug 2026-04-15 16:00-18:00
    assert fenster.von.year == 2026 and fenster.von.month == 4 and fenster.von.day == 15
    assert fenster.von.hour == 16
    assert fenster.bis.hour == 18


def test_t0304_effektiver_zweck_inferenz_und_explizit():
    """T-0304: ml_ausschluss_fenster.zweck unterscheidet Sensor-Kalibrierung
    (Werte unzuverlaessig) von Event-Ignore (Sensor ok, nur Kanal-Events).
    Default-Inferenz aus events_auto_ignorieren, explizit gewinnt."""
    from datetime import datetime

    from bewaesserung.modelle import MlAusschlussFenster

    von, bis = datetime(2026, 6, 13, 14), datetime(2026, 6, 21, 20)
    # Inferenz: kein events_auto_ignorieren -> sensor_kalibrierung.
    f_sensor = MlAusschlussFenster(zone_id="hecke", von=von, bis=bis)
    assert f_sensor.effektiver_zweck == "sensor_kalibrierung"
    # Inferenz: events_auto_ignorieren=True -> event_ignore.
    f_event = MlAusschlussFenster(
        zone_id="magerwiese", von=von, bis=bis, events_auto_ignorieren=True,
    )
    assert f_event.effektiver_zweck == "event_ignore"
    # Explizit gewinnt ueber Inferenz.
    f_explizit = MlAusschlussFenster(
        zone_id="x", von=von, bis=bis,
        events_auto_ignorieren=True, zweck="sensor_kalibrierung",
    )
    assert f_explizit.effektiver_zweck == "sensor_kalibrierung"
    # Ungueltiger zweck faellt auf Inferenz zurueck.
    f_muell = MlAusschlussFenster(zone_id="x", von=von, bis=bis, zweck="quatsch")
    assert f_muell.effektiver_zweck == "sensor_kalibrierung"


def test_t0304_magerwiese_fenster_ist_event_ignore():
    """Ein Fenster mit events_auto_ignorieren muss als event_ignore
    aufgeloest werden, damit das Dashboard 'Events ignoriert' statt
    'Kalibrierung' zeigt."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    mw = [
        f for f in konfig.ml_ausschluss_fenster
        if f.zone_id == "garten_sprinkler" and f.events_auto_ignorieren
    ]
    assert mw, "garten_sprinkler events_auto_ignorieren-Fenster fehlt"
    assert all(f.effektiver_zweck == "event_ignore" for f in mw)


def test_kalibrierung_konfig_aus_yaml_durchgereicht():
    """T-0099: kalibrierung-Block muss durchs YAML-Parsing kommen.

    Vor dem Fix faellt `plateau_max_delta` stumm auf den Pydantic-Default
    5.0 zurueck, obwohl default.yaml 3.0 setzt — d.h. die KalibrationsJob
    arbeitet mit doppelt so weiter Toleranz wie konfiguriert. Klassisches
    Whitelist-Antipattern (siehe fehlerpattern_config_whitelist.md).
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    k = konfig.kalibrierung
    assert k.aktiv is True
    # T-0085-Folge 29.04.: 6 -> 1 (User-Praeferenz "lieber haeufiger triggern").
    assert k.intervall_stunden == 1
    assert k.rueckblick_tage == 60
    assert k.regen_min_mm == 10.0
    # Der eigentliche Regression-Wert: YAML 3.0 schlaegt Default 5.0.
    assert k.plateau_max_delta == 3.0
    assert k.welkepunkt_min_tage == 10
    assert k.saison_monate == [5, 6, 7, 8, 9]


def test_feuchte_regime_aus_yaml_durchgereicht(tmp_path):
    """T-0128 (H-4): feuchte_regime-Liste muss durchs YAML-Parsing kommen.

    Whitelist-Pattern (siehe fehlerpattern_config_whitelist.md). Wenn
    `feuchte_regime` im _parse_zonen-Block fehlt, kommt das Feld trotz
    YAML-Eintrag als [] an, und Magerwiese-Sommer-Trockenphase greift
    nie -- Pre-Mortem Akt 3.
    """
    yaml_text = """
gardena:
  client_id: x
  client_secret: y
zonen:
  - zone_id: magerwiese
    name: Magerwiese
    modus: monitoring
    feuchte_schwelle_min: 30.0
    feuchte_schwelle_max: 50.0
    feuchte_kritisch: 15.0
    feuchte_regime:
      - name: anwachs
        von_mm_dd: "04-01"
        bis_mm_dd: "06-30"
        optimum_min: 40.0
        optimum_max: 60.0
        grund: "Anwachs-Phase"
      - name: sommer_trocken
        von_mm_dd: "07-01"
        bis_mm_dd: "09-30"
        feuchte_schwelle_min: 12.0
        feuchte_kritisch: 5.0
        optimum_min: 15.0
        optimum_max: 25.0
        grund: "Trockenphase gegen Konkurrenzgraeser"
wetter:
  standorte:
    - id: musterstadt
      breite: 52.52
      laenge: 13.40
"""
    cfg_path = tmp_path / "magerwiese.yaml"
    cfg_path.write_text(yaml_text)
    konfig = lade_konfig(cfg_path)
    zone = next(z for z in konfig.zonen if z.zone_id == "magerwiese")
    assert len(zone.feuchte_regime) == 2
    sommer = zone.feuchte_regime[1]
    assert sommer.name == "sommer_trocken"
    assert sommer.optimum_min == 15.0
    assert sommer.optimum_max == 25.0
    assert sommer.feuchte_kritisch == 5.0
    # Zone-Defaults bleiben unangetastet (sind der Fallback ausserhalb der Regimes)
    assert zone.feuchte_schwelle_min == 30.0


def test_wochen_report_konfig_aus_yaml_durchgereicht():
    """T-0099: wochen_report-Block muss durchs YAML-Parsing kommen.

    Vor dem Fix faellt `aktiv` stumm auf den Pydantic-Default False zurueck,
    obwohl default.yaml `aktiv: true` setzt — der Wochen-Report-Job lief
    nicht. Whitelist-Antipattern wie oben.
    """
    konfig = lade_konfig(DEFAULT_CONFIG)
    w = konfig.wochen_report
    assert w.aktiv is True
    assert w.tag_der_woche == 6
    assert w.stunde == 20


def test_aquabloom_felder_aus_yaml_durchgereicht():
    """T-0168: alle 7 AquaBloom-Felder muessen aus default.yaml im
    ZonenKonfig landen. Whitelist-Drift-Regression (Memory
    `fehlerpattern_config_whitelist.md`).

    topf_9 + topf_1 teilen Anker + Intervall + Dauer + Tropfer-Konfig
    (eine Pumpe, je 2 Tropfer mit 2 L/h). Whitelist-Coverage fuer beide
    aktiven Zonen.
    """
    from datetime import datetime as _dt

    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    # Aktive Pumpen-Zone (teilt die Pumpe mit topf_1).
    z = nach_id["topf_9"]
    assert z.aquabloom_pumpen_dauer_sekunden == 900
    assert z.aquabloom_pumpen_intervall_stunden == 12
    assert isinstance(z.aquabloom_anker_zeitstempel, _dt)
    assert z.aquabloom_aktiv_ab == "05-01"
    assert z.aquabloom_aktiv_bis == "10-01"
    assert z.aquabloom_tropfer_anzahl == 2
    assert z.aquabloom_tropfer_liter_pro_stunde == 2.0

    # topf_1: Werte identisch zu topf_9 (dauer 900, intervall 12, 2 Tropfer).
    z = nach_id["topf_1"]
    assert z.aquabloom_pumpen_dauer_sekunden == 900
    assert z.aquabloom_pumpen_intervall_stunden == 12
    assert z.aquabloom_tropfer_anzahl == 2
    assert isinstance(z.aquabloom_anker_zeitstempel, _dt)
    assert z.aquabloom_aktiv_ab == "05-01"
    assert z.aquabloom_aktiv_bis == "10-01"
    assert z.aquabloom_tropfer_liter_pro_stunde == 2.0


def test_logging_einheit_aus_yaml_durchgereicht():
    """T-0169: logging_einheit + logging_optionen_ml fuer FYTA-Topf-Zonen."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    nach_id = {z.zone_id: z for z in konfig.zonen}
    for zid in ("topf_1", "topf_9"):
        z = nach_id[zid]
        assert z.logging_einheit == "ml", zid
        assert z.logging_optionen_ml == [100, 250, 500, 1000], zid

    # Default fuer Zonen ohne Override: 'sekunden', leere Optionen
    assert nach_id["garten_mikrodrip"].logging_einheit == "sekunden"
    assert nach_id["garten_mikrodrip"].logging_optionen_ml == []


# --- T-0252: Multi-DSWC-Konfig-Validator ---

def _schreibe_yaml(tmp_path, inhalt: str):
    pfad = tmp_path / "test.yaml"
    pfad.write_text(inhalt, encoding="utf-8")
    return pfad


_BASIS_YAML = """
gardena:
  client_id: x
  client_secret: y
wetter:
  standorte:
    - id: berlin
      breite: 52.52
      laenge: 13.40
speicher:
  db_pfad: ":memory:"
standorte:
  - standort_id: garten
    name: Garten
    wetter_standort: berlin
    zonen: {zonen_liste}
zonen:
{zonen_yaml}
"""


def test_konfig_validator_lehnt_doppelten_kanal_ohne_geraet_ab(tmp_path):
    """T-0252: zwei Zonen am gleichen Kanal ohne `ventil_geraet_id`
    -> ValueError beim Laden (sonst silent Phantom-Events bei Multi-
    DSWC-Setups)."""
    pfad = _schreibe_yaml(tmp_path, _BASIS_YAML.format(
        zonen_liste='[zone_a, zone_b]',
        zonen_yaml=(
            "  - zone_id: zone_a\n"
            "    name: A\n"
            "    ventil_kanal: 1\n"
            "  - zone_id: zone_b\n"
            "    name: B\n"
            "    ventil_kanal: 1\n"
        ),
    ))
    with pytest.raises(ValueError, match=r"T-0252.*ventil_kanal=1"):
        lade_konfig(pfad)


def test_konfig_validator_akzeptiert_gleichen_kanal_gleiches_geraet(tmp_path):
    """Echter Geschwister-Cluster: zwei Zonen am selben (kanal, geraet) —
    z. B. bambuswald + bambuswald_yogaraum an einem Mikrodrip — ist
    erlaubt."""
    pfad = _schreibe_yaml(tmp_path, _BASIS_YAML.format(
        zonen_liste='[zone_a, zone_b]',
        zonen_yaml=(
            "  - zone_id: zone_a\n"
            "    name: A\n"
            "    ventil_kanal: 2\n"
            "    ventil_geraet_id: dswc-uuid-1\n"
            "  - zone_id: zone_b\n"
            "    name: B\n"
            "    ventil_kanal: 2\n"
            "    ventil_geraet_id: dswc-uuid-1\n"
        ),
    ))
    konfig = lade_konfig(pfad)
    assert {z.zone_id for z in konfig.zonen} == {"zone_a", "zone_b"}


def test_konfig_validator_akzeptiert_gleichen_kanal_verschiedene_geraete(tmp_path):
    """Multi-DSWC-Setup: zwei Zonen am Kanal 1 unterschiedlicher DSWCs —
    jede mit expliziter `ventil_geraet_id` — ist erlaubt."""
    pfad = _schreibe_yaml(tmp_path, _BASIS_YAML.format(
        zonen_liste='[zone_a, zone_b]',
        zonen_yaml=(
            "  - zone_id: zone_a\n"
            "    name: A\n"
            "    ventil_kanal: 1\n"
            "    ventil_geraet_id: dswc-uuid-1\n"
            "  - zone_id: zone_b\n"
            "    name: B\n"
            "    ventil_kanal: 1\n"
            "    ventil_geraet_id: dswc-uuid-2\n"
        ),
    ))
    konfig = lade_konfig(pfad)
    assert len(konfig.zonen) == 2


def test_konfig_validator_einzelne_zone_ohne_geraet_ist_ok(tmp_path):
    """Single-DSWC-Setup ohne explizite `ventil_geraet_id` bleibt
    erlaubt (Backward-Compat). Validator schlaegt nur bei MEHREREN
    Zonen am selben Kanal ohne Geraet zu."""
    pfad = _schreibe_yaml(tmp_path, _BASIS_YAML.format(
        zonen_liste='[zone_a]',
        zonen_yaml=(
            "  - zone_id: zone_a\n"
            "    name: A\n"
            "    ventil_kanal: 1\n"
        ),
    ))
    konfig = lade_konfig(pfad)
    assert konfig.zonen[0].zone_id == "zone_a"


def test_t0353_state_space_und_routing_aus_yaml_durchgereicht():
    """T-0353: ml_state_space + ml_forecast_routing muessen durchs
    YAML-Parsing kommen. Whitelist-Drift-Regression (Memory
    `fehlerpattern_config_whitelist.md`): ohne explizite Durchreiche in
    konfig.py fielen beide Sektionen stumm auf die Pydantic-Defaults
    (aktiv=False) zurueck und der Shadow-Logger liefe nie."""
    konfig = lade_konfig(DEFAULT_CONFIG)
    ss = konfig.ml_state_space
    assert ss.aktiv is True
    assert ss.ramp_stunden == 1.5
    assert ss.puls_lookback_stunden == 3.0
    assert ss.regen_faktor_pp_pro_mm == 4.0
    routing = konfig.ml_forecast_routing
    assert routing.aktiv is True
    assert routing.default_quelle == "ml"
    assert routing.zonen.get("wiese_sprinkler") == "statespace"
    assert routing.zonen.get("garten_mikrodrip") == "ml"
