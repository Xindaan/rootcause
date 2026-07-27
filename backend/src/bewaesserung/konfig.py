"""Konfiguration laden aus YAML-Datei + Umgebungsvariablen.

Laedt config/default.yaml und ersetzt ${ENV_VAR}-Platzhalter
durch Umgebungsvariablen. Validiert via Pydantic GesamtKonfig.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from bewaesserung.modelle import (
    BackupKonfig,
    MlBewaesserungsResponseKonfig,
    MlRetrainKonfig,
    BalkonKonfig,
    BenachrichtigungsKonfig,
    BilanzKonfig,
    EndpointHealthKonfig,
    FeuchteRegime,
    FytaKonfig,
    FytaPflanzenKonfig,
    GardenaKonfig,
    GesamtKonfig,
    HahnCluster,
    KalibrierungKonfig,
    MlAusschlussFenster,
    MlForecastRoutingKonfig,
    MlPhysikDiagnoseKonfig,
    MlSkalenMappingKonfig,
    MlStateSpaceKonfig,
    MlWirkungFitKonfig,
    SchwellenAdaptionKonfig,
    SpeicherKonfig,
    StandortKonfig,
    WatchdogKonfig,
    WetterKonfig,
    WetterStandortKonfig,
    WochenReportKonfig,
    ZeitFenster,
    ZonenKonfig,
    ZonenModus,
)

# Projekt-Root: 4 Ebenen hoch von diesem File (src/bewaesserung/konfig.py -> src -> backend -> Gardena)
PROJEKT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STANDARD_KONFIG_PFAD = PROJEKT_ROOT / "config" / "default.yaml"

# ML-Daten-Pfad (absolut, CWD-unabhaengig)
ML_DATEN_PFAD = str(PROJEKT_ROOT / "backend" / "daten" / "ml")

# Pattern fuer ${ENV_VAR} Platzhalter
ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _ersetze_env_variablen(wert: str) -> str:
    """Ersetzt ${VAR_NAME} durch den Wert der Umgebungsvariable."""
    def _ersetze(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")
    return ENV_PATTERN.sub(_ersetze, wert)


def _env_rekursiv(daten: dict | list | str) -> dict | list | str:
    """Ersetzt env-Platzhalter rekursiv in verschachtelten Strukturen."""
    if isinstance(daten, dict):
        return {k: _env_rekursiv(v) for k, v in daten.items()}
    if isinstance(daten, list):
        return [_env_rekursiv(item) for item in daten]
    if isinstance(daten, str):
        return _ersetze_env_variablen(daten)
    return daten


_konfig_logger = logging.getLogger(__name__)


def _parse_tages_budget(roh: dict) -> float:
    """Liest tages_budget_sekunden; alte Liter-Semantik ist nicht konvertierbar."""
    if "tages_budget_sekunden" in roh:
        return float(roh["tages_budget_sekunden"])
    if "tages_budget_liter" in roh:
        raise ValueError(
            "Zonenkonfig "
            f"{roh.get('zone_id', '?')}: 'tages_budget_liter' ist nicht "
            "mehr erlaubt. Bitte explizit 'tages_budget_sekunden' setzen; "
            "Liter duerfen nicht als Sekundenbudget interpretiert werden."
        )
    return 3600.0


def _parse_zeitfenster(text: str) -> ZeitFenster:
    """Parst '05:00-07:00' zu ZeitFenster."""
    teile = text.split("-")
    if len(teile) != 2:
        raise ValueError(f"Ungueliges Zeitfenster: {text!r} (erwartet 'HH:MM-HH:MM')")
    return ZeitFenster(von=teile[0].strip(), bis=teile[1].strip())


def _parse_zonen(roh_zonen: list[dict]) -> list[ZonenKonfig]:
    """Wandelt rohe YAML-Zonendaten in ZonenKonfig-Objekte."""
    zonen = []
    for roh in roh_zonen:
        zeitfenster = [_parse_zeitfenster(z) for z in roh.get("bevorzugte_zeiten", [])]

        benachrichtigung = None
        if "benachrichtigung" in roh:
            benachrichtigung = BenachrichtigungsKonfig(**roh["benachrichtigung"])

        zonen.append(ZonenKonfig(
            zone_id=roh["zone_id"],
            name=roh["name"],
            modus=ZonenModus(roh.get("modus", "automatik")),
            ventil_kanal=roh.get("ventil_kanal"),
            ventil_name=roh.get("ventil_name"),
            # T-0203: Multi-DSWC-Routing (siehe ZonenKonfig.ventil_geraet_id).
            ventil_geraet_id=roh.get("ventil_geraet_id"),
            # T-0334: Pro-Strang-Opt-In fuer den autonomen Auto-Loop.
            # Whitelist-Pattern (Memory: fehlerpattern_config_whitelist.md) --
            # ohne diese Zeile bleibt das Feld trotz YAML-Eintrag False.
            auto_loop_opt_in=roh.get("auto_loop_opt_in", False),
            feuchte_schwelle_min=roh.get("feuchte_schwelle_min", 35.0),
            feuchte_schwelle_max=roh.get("feuchte_schwelle_max", 65.0),
            feuchte_kritisch=roh.get("feuchte_kritisch", 20.0),
            max_dauer_sekunden=roh.get("max_dauer_sekunden", 1800),
            min_pause_minuten=roh.get("min_pause_minuten", 120),
            tages_budget_sekunden=_parse_tages_budget(roh),
            tages_budget_kritisch_faktor=float(roh.get("tages_budget_kritisch_faktor", 1.0)),
            ist_topf=roh.get("ist_topf", False),
            ist_indoor=roh.get("ist_indoor", False),
            bevorzugte_zeiten=zeitfenster,
            benachrichtigung=benachrichtigung,
            flaeche_m2=roh.get("flaeche_m2"),
            anteil_kanal=roh.get("anteil_kanal", 1.0),
            # T-0050a: Pflanzen-Optimum aus YAML durchreichen.
            # Config-Whitelist-Pattern beachten — ohne diese Zeilen kommt
            # das Feld trotz YAML-Eintrag als None ans API. (Memory:
            # fehlerpattern_config_whitelist.md)
            optimum_feuchte_min=roh.get("optimum_feuchte_min"),
            optimum_feuchte_max=roh.get("optimum_feuchte_max"),
            # T-0071: Versickerungs-Karenz pro Zone (Standard 3 h, Sandboden 6 h).
            versickerungs_karenz_stunden=roh.get(
                "versickerungs_karenz_stunden", 3,
            ),
            # T-0422: Tropferzahl fuer die benetzte Flaeche (Bilanz).
            tropfer_anzahl=roh.get("tropfer_anzahl"),
            # T-0414: eigene, kuerzere Karenz nur fuer den T-0378-Pause-Bypass
            # (Standard 1 h = dokumentierter Sensor-Nachlauf). Bewusst NICHT
            # aus versickerungs_karenz_stunden abgeleitet -- anderer Zweck,
            # s. Kommentar am Feld in modelle.py.
            pause_bypass_karenz_stunden=float(roh.get(
                "pause_bypass_karenz_stunden", 1.0,
            )),
            # T-0251: Detektor-Fenster-Ende fuer "Bewaesserung ohne Wirkung"
            # in Minuten (None = globaler Default 90, sonst per Zone z.B. 180
            # fuer Bambus/Magerwiese wegen langsamer Sensor-Antwort).
            detektor_fenster_ende_min=roh.get("detektor_fenster_ende_min"),
            # T-0075: Welkepunkt-Override (None = Auto-Aufloesung) und
            # Sicherheits-Reserve fuer die kausale Giess-Empfehlung.
            welkepunkt=roh.get("welkepunkt"),
            sicherheits_tage=float(roh.get("sicherheits_tage", 3.0)),
            # T-0082: Cluster-Override (None = Pro-Sensor-Modell, default).
            cluster_id=roh.get("cluster_id"),
            # T-0089: Pro-Zone-Wirkungsrate fuer Heuristik-Dauer.
            # None = Default 1.0 pp/min wird in _berechne_dauer angewendet.
            delta_pp_pro_minute=roh.get("delta_pp_pro_minute"),
            # T-0103: Bewaesserungs-Strategie. Default `korridor` =
            # Backward-Compat (= heutiges Verhalten + neue
            # `wohlfuehl_grenze`-Stufe). Whitelist-Pattern wie bei den
            # anderen Feldern (siehe fehlerpattern_config_whitelist.md).
            bewaesserungs_strategie=roh.get(
                "bewaesserungs_strategie", "korridor",
            ),
            # T-0091a: log-Decay-Alpha fuer Wirkungsrate bei langen Dosen.
            # Default 0 (Backward-Compat). Realdaten Bambus-Mikrodrip ~ -0.6.
            wirkungsrate_dauer_alpha=float(
                roh.get("wirkungsrate_dauer_alpha", 0.0),
            ),
            # T-0091b: Plateau-Modell-Parameter. Beide None (Default) =
            # Plateau aus. Wenn beide gesetzt, ueberschreibt es das
            # alpha-Modell.
            wirkung_max_pp=roh.get("wirkung_max_pp"),
            wirkungsrate_initial=roh.get("wirkungsrate_initial"),
            # T-0111: Pre-Soak-Defaults pro Zone (Frontend-Vorbelegung).
            pre_soak_min=roh.get("pre_soak_min"),
            pre_soak_pause_min=int(roh.get("pre_soak_pause_min", 30)),
            # T-0336: Pre-Soak-Policy (Whitelist-Pattern, Memory
            # fehlerpattern_config_whitelist). Default "nie" = Einzellauf.
            pre_soak_modus=roh.get("pre_soak_modus", "nie"),
            # T-0437: Cycle-and-Soak-Aufteilung der Hauptdose. Gleiches
            # Whitelist-Pattern; Defaults 1/0 = Bestandsverhalten.
            haupt_pulse=int(roh.get("haupt_pulse", 1)),
            haupt_puls_pause_min=int(roh.get("haupt_puls_pause_min", 0)),
            # T-0128 (H-4): Saisonale/phasen-abhaengige Feuchte-Regimes.
            # Whitelist-Pattern (Memory: fehlerpattern_config_whitelist.md).
            # Pydantic validiert die FeuchteRegime-Felder via **dict-Spread.
            feuchte_regime=[
                FeuchteRegime(**r) for r in roh.get("feuchte_regime", [])
            ],
            # T-0151: Hahn-Cluster + Verbrauch (Whitelist-Pattern).
            hahn_cluster=roh.get("hahn_cluster"),
            verbrauch_lpm=roh.get("verbrauch_lpm"),
            # T-0153: Druck-Exklusivitaet (Sprinkler etc.).
            exklusiv=bool(roh.get("exklusiv", False)),
            # T-0168: AquaBloom-Auto-Logging.
            aquabloom_pumpen_dauer_sekunden=roh.get("aquabloom_pumpen_dauer_sekunden"),
            aquabloom_pumpen_intervall_stunden=roh.get("aquabloom_pumpen_intervall_stunden"),
            aquabloom_anker_zeitstempel=_parse_aquabloom_anker(
                roh.get("aquabloom_anker_zeitstempel"),
            ),
            aquabloom_tropfer_anzahl=roh.get("aquabloom_tropfer_anzahl"),
            aquabloom_tropfer_liter_pro_stunde=roh.get("aquabloom_tropfer_liter_pro_stunde"),
            aquabloom_aktiv_ab=roh.get("aquabloom_aktiv_ab"),
            aquabloom_aktiv_bis=roh.get("aquabloom_aktiv_bis"),
            # T-0169: Logging-Einheit fuer den Manuell-Button.
            logging_einheit=str(roh.get("logging_einheit", "sekunden")),
            logging_optionen_ml=list(roh.get("logging_optionen_ml") or []),
            # T-0187: Sensor-Heuristik pro Zone (Override + Roll-Up).
            heuristik_min_delta_pp=roh.get("heuristik_min_delta_pp"),
            heuristik_rollup_fenster_min=roh.get(
                "heuristik_rollup_fenster_min",
            ),
            heuristik_rollup_schwelle_pp=roh.get(
                "heuristik_rollup_schwelle_pp",
            ),
            # T-0312: Cross-Spray-Quell-Zonen (Whitelist-Pattern,
            # fehlerpattern_config_whitelist.md).
            cross_spray_quell_zonen=roh.get("cross_spray_quell_zonen", []),
            # T-0332: Aggregat-Lead-Sensor (Whitelist-Pattern, Memory
            # fehlerpattern_config_whitelist.md). None = Median-Verhalten.
            aggregat_lead_geraet=roh.get("aggregat_lead_geraet"),
            # T-0337: Kanal-Trigger-Ausschluss (Whitelist-Pattern). Default
            # False = Zone treibt den Kanal-Min-Trigger wie bisher.
            kanal_trigger_ausschluss=roh.get("kanal_trigger_ausschluss", False),
            # T-0214: Pro-Zone-Override fuer SENSOR_AUSFALL-Schwelle.
            # Whitelist-Pattern (Memory: fehlerpattern_config_whitelist.md).
            ausfall_schwelle_stunden=roh.get("ausfall_schwelle_stunden"),
            # T-0181: Sensor-Quellen die gegen Gardena gefittet werden.
            # Default []: keine Fit-Teilnahme.
            calibration_pair_quellen=roh.get(
                "calibration_pair_quellen", [],
            ),
            # Hybrid Stufe 1: ZonenKonfig-Override fuer Trocknungs-
            # Konstante (Default None -> nutzt physik_k_basis-Tabelle).
            k_basis_pro_h=roh.get("k_basis_pro_h"),
            # T-0279 Phase 2: proaktiver Trigger-Schwellwert (Tage vor
            # optimum_min, Physik-basiert). Default None = altes Verhalten.
            proaktiv_tage_vor_optimum_min=roh.get(
                "proaktiv_tage_vor_optimum_min",
            ),
            # T-0279 Phase 2: proaktiver Trigger auf trockensten Sensor.
            proaktiv_min_sensor=roh.get("proaktiv_min_sensor", False),
        ))
    return zonen


def _parse_aquabloom_anker(roh_wert) -> "datetime | None":
    """T-0168: ISO8601-String -> datetime, None bleibt None.

    Beispiel: "2026-05-09T08:00:00" -> datetime(2026, 5, 9, 8, 0).
    """
    if roh_wert is None:
        return None
    if isinstance(roh_wert, datetime):
        return roh_wert
    return datetime.fromisoformat(str(roh_wert))


def lade_konfig(pfad: Path | None = None) -> GesamtKonfig:
    """Laedt und validiert die Konfiguration.

    Args:
        pfad: Pfad zur YAML-Datei. Standard: config/default.yaml

    Returns:
        Validierte GesamtKonfig.
    """
    if pfad is None:
        pfad = STANDARD_KONFIG_PFAD

    if not pfad.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {pfad}")

    with open(pfad, encoding="utf-8") as f:
        roh = yaml.safe_load(f)

    # Umgebungsvariablen ersetzen
    roh = _env_rekursiv(roh)

    # Wetter-Koordinaten koennen auch aus env kommen
    wetter_roh = roh.get("wetter", {})
    breite = float(os.environ.get("WETTER_BREITE", wetter_roh.get("breite", 0.0)))
    laenge = float(os.environ.get("WETTER_LAENGE", wetter_roh.get("laenge", 0.0)))

    # iMessage-Empfaenger aus env
    imessage_empfaenger = os.environ.get("IMESSAGE_EMPFAENGER", "")

    zonen = _parse_zonen(roh.get("zonen", []))

    # Empfaenger in Monitoring-Zonen eintragen falls aus env
    if imessage_empfaenger:
        for zone in zonen:
            if zone.benachrichtigung and not zone.benachrichtigung.empfaenger:
                zone.benachrichtigung.empfaenger = imessage_empfaenger

    gardena_roh = roh.get("gardena", {})
    speicher_roh = roh.get("speicher", {})

    # Multi-Standort-Wetter
    wetter_standorte = [
        WetterStandortKonfig(**s) for s in wetter_roh.get("standorte", [])
    ]

    # Standort-Gruppierung
    standorte = [
        StandortKonfig(**s) for s in roh.get("standorte", [])
    ]

    # FYTA
    fyta_konfig = None
    fyta_roh = roh.get("fyta")
    if fyta_roh:
        fyta_pflanzen = [
            FytaPflanzenKonfig(**p) for p in fyta_roh.get("pflanzen", [])
        ]
        fyta_konfig = FytaKonfig(
            api_url=fyta_roh.get("api_url", "https://web.fyta.de/api"),
            poll_intervall_sekunden=fyta_roh.get("poll_intervall_sekunden", 900),
            pflanzen=fyta_pflanzen,
        )

    # Balkon-Ausrichtung
    balkon_roh = roh.get("balkon_ausrichtung", {})
    balkon_ausrichtung = {
        k: BalkonKonfig(**v) for k, v in balkon_roh.items()
    }

    # T-0234: microdrip_solar-Parser entfernt (toter Konfig-Vertrag).
    # Alte Configs mit `microdrip_solar:`-Block werden vom YAML-Loader
    # eingelesen, aber von `_baue_gesamtkonfig` ignoriert. Pydantic
    # `GesamtKonfig` hat das Feld nicht mehr, kein Validierungs-Fehler
    # weil wir das Roh-Dict mit Whitelist konstruieren.

    # ML-Ausschluss-Fenster
    ml_ausschluss = [
        MlAusschlussFenster(**f) for f in roh.get("ml_ausschluss_fenster", [])
    ]

    # ET0-adaptive Schwellen + Wasser-Bilanz (Defaults greifen, falls Block fehlt)
    schwellen_adaption_roh = roh.get("schwellen_adaption")
    schwellen_adaption = (
        SchwellenAdaptionKonfig(**schwellen_adaption_roh)
        if schwellen_adaption_roh else SchwellenAdaptionKonfig()
    )
    bilanz_roh = roh.get("bilanz") or {}
    # YAML-Keys fuer Kanaele kommen als str an, BilanzKonfig erwartet int
    kanal_raten_roh = bilanz_roh.get("kanal_liter_pro_minute", {})
    kanal_raten = {int(k): float(v) for k, v in kanal_raten_roh.items()}
    geraet_kanal_raten_roh = bilanz_roh.get("geraet_kanal_liter_pro_minute", {})
    geraet_kanal_raten = {
        str(geraet_id): {int(k): float(v) for k, v in (raten or {}).items()}
        for geraet_id, raten in geraet_kanal_raten_roh.items()
    }
    bilanz_konfig = BilanzKonfig(
        kanal_liter_pro_minute=kanal_raten,
        geraet_kanal_liter_pro_minute=geraet_kanal_raten,
        manuell_liter_pro_minute=float(bilanz_roh.get("manuell_liter_pro_minute", 10.0)),
    )

    # Backup-Job (T-0043): Pfad relativ zum Projekt-Root, analog zu speicher.db_pfad
    backup_roh = roh.get("backup") or {}
    backup_defaults = BackupKonfig()
    backup_verzeichnis_roh = backup_roh.get("verzeichnis", backup_defaults.verzeichnis)
    backup_konfig = BackupKonfig(
        aktiv=backup_roh.get("aktiv", backup_defaults.aktiv),
        intervall_stunden=backup_roh.get(
            "intervall_stunden", backup_defaults.intervall_stunden,
        ),
        verzeichnis=str(PROJEKT_ROOT / "backend" / backup_verzeichnis_roh),
        retention_taeglich_tage=backup_roh.get(
            "retention_taeglich_tage", backup_defaults.retention_taeglich_tage,
        ),
        monatlich_aktiv=backup_roh.get(
            "monatlich_aktiv", backup_defaults.monatlich_aktiv,
        ),
        max_dateien=backup_roh.get("max_dateien", backup_defaults.max_dateien),
        # T-0131 (H-7): Spiegel-Verzeichnis (z. B. iCloud Drive). None = aus.
        # Whitelist-Pattern (Memory: fehlerpattern_config_whitelist.md).
        spiegel_verzeichnis=backup_roh.get("spiegel_verzeichnis"),
    )

    # T-0048 Auto-Retrain: woechentlicher In-Service-Retrain mit Deploy-Gate.
    # Pfad relativ zum Projekt-Root (analog backup/speicher); leer → ML_DATEN_PFAD.
    retrain_roh = (roh.get("ml") or {}).get("retrain") or {}
    retrain_defaults = MlRetrainKonfig()
    retrain_ausgabe_roh = retrain_roh.get("ausgabe_pfad", "")
    retrain_ausgabe_pfad = (
        str(PROJEKT_ROOT / "backend" / retrain_ausgabe_roh)
        if retrain_ausgabe_roh
        else ""  # leer → Job nutzt konfig.ML_DATEN_PFAD
    )
    ml_retrain_konfig = MlRetrainKonfig(
        aktiv=retrain_roh.get("aktiv", retrain_defaults.aktiv),
        intervall_tage=retrain_roh.get(
            "intervall_tage", retrain_defaults.intervall_tage,
        ),
        trainings_fenster_tage=retrain_roh.get(
            "trainings_fenster_tage", retrain_defaults.trainings_fenster_tage,
        ),
        gate_faktor=retrain_roh.get("gate_faktor", retrain_defaults.gate_faktor),
        folds=retrain_roh.get("folds", retrain_defaults.folds),
        quantile=retrain_roh.get("quantile", retrain_defaults.quantile),
        monotone_constraints=retrain_roh.get(
            "monotone_constraints",
            retrain_defaults.monotone_constraints,
        ),
        ausgabe_pfad=retrain_ausgabe_pfad,
        tmp_suffix=retrain_roh.get("tmp_suffix", retrain_defaults.tmp_suffix),
        archiv_suffix=retrain_roh.get("archiv_suffix", retrain_defaults.archiv_suffix),
        # T-0082: Cluster-Architektur — Default `global` haelt das alte
        # Verhalten unveraendert. Erst `pro_zone` aktiviert die Pro-Sensor-
        # Modelle (siehe MlRetrainJob.cluster_zonen).
        cluster_strategie=retrain_roh.get(
            "cluster_strategie", retrain_defaults.cluster_strategie,
        ),
        mindest_zeilen_pro_cluster=retrain_roh.get(
            "mindest_zeilen_pro_cluster",
            retrain_defaults.mindest_zeilen_pro_cluster,
        ),
        gate_faktor_pro_cluster=retrain_roh.get(
            "gate_faktor_pro_cluster",
            retrain_defaults.gate_faktor_pro_cluster,
        ),
    )

    # T-0065 Response-Modell (Config-Whitelist — Pflicht, sonst stiller Default.
    # Memory: fehlerpattern_config_whitelist.md)
    response_roh = (roh.get("ml") or {}).get("bewaesserungs_response") or {}
    response_defaults = MlBewaesserungsResponseKonfig()
    response_ausgabe_roh = response_roh.get("ausgabe_pfad", "")
    response_ausgabe_pfad = (
        str(PROJEKT_ROOT / "backend" / response_ausgabe_roh)
        if response_ausgabe_roh
        else ""  # leer → Service nutzt ML_DATEN_PFAD/response
    )
    ml_bewaesserungs_response_konfig = MlBewaesserungsResponseKonfig(
        aktiv=response_roh.get("aktiv", response_defaults.aktiv),
        wirksam=response_roh.get("wirksam", response_defaults.wirksam),
        min_events=response_roh.get("min_events", response_defaults.min_events),
        retrain_intervall_tage=response_roh.get(
            "retrain_intervall_tage", response_defaults.retrain_intervall_tage,
        ),
        retrain_event_schwelle=response_roh.get(
            "retrain_event_schwelle", response_defaults.retrain_event_schwelle,
        ),
        gate_mae_faktor=response_roh.get(
            "gate_mae_faktor", response_defaults.gate_mae_faktor,
        ),
        ausgabe_pfad=response_ausgabe_pfad,
        cluster_gap_min=response_roh.get(
            "cluster_gap_min", response_defaults.cluster_gap_min,
        ),
    )

    # T-0099: Whitelist-Lift fuer kalibrierung + wochen_report. Ohne diesen
    # Pass landet YAML-Config im Default-Fallback der Pydantic-Modelle —
    # gleicher Bug-Klasse wie ml_bewaesserungs_response (Memory:
    # fehlerpattern_config_whitelist.md).
    kalibrierung_roh = roh.get("kalibrierung") or {}
    kalibrierung_konfig = KalibrierungKonfig(**kalibrierung_roh)

    # T-0181: Skalen-Mapping-Fit-Job (analoge Whitelist-Disziplin).
    ml_skalen_mapping_roh = roh.get("ml_skalen_mapping") or {}
    ml_skalen_mapping_konfig = MlSkalenMappingKonfig(**ml_skalen_mapping_roh)

    # Hybrid Stufe 1: Physik-Diagnose-Job (analoge Whitelist-Disziplin).
    ml_physik_diagnose_roh = roh.get("ml_physik_diagnose") or {}
    ml_physik_diagnose_konfig = MlPhysikDiagnoseKonfig(**ml_physik_diagnose_roh)

    # T-0292 Stufe 2: Plateau-Wirkungs-Fit-Job (analoge Whitelist-Disziplin).
    ml_wirkung_fit_roh = roh.get("ml_wirkung_fit") or {}
    ml_wirkung_fit_konfig = MlWirkungFitKonfig(**ml_wirkung_fit_roh)

    # T-0353: State-Space-Shadow + Prognose-Routing (analoge
    # Whitelist-Disziplin, fehlerpattern_config_whitelist).
    ml_state_space_roh = roh.get("ml_state_space") or {}
    ml_state_space_konfig = MlStateSpaceKonfig(**ml_state_space_roh)
    ml_forecast_routing_roh = roh.get("ml_forecast_routing") or {}
    ml_forecast_routing_konfig = MlForecastRoutingKonfig(
        **ml_forecast_routing_roh,
    )

    wochen_report_roh = roh.get("wochen_report") or {}
    wochen_report_konfig = WochenReportKonfig(**wochen_report_roh)

    # H-2: Watchdog-Push-Konfig (Pre-Mortem Akt 1, 2, 4). Whitelist analog
    # zu kalibrierung/wochen_report (Memory: fehlerpattern_config_whitelist.md).
    watchdog_roh = roh.get("watchdog") or {}
    watchdog_konfig = WatchdogKonfig(**watchdog_roh)

    # H-8: Endpoint-Health-Check (Pre-Mortem Akt 4 + FYTA).
    endpoint_health_roh = roh.get("endpoint_health") or {}
    endpoint_health_konfig = EndpointHealthKonfig(**endpoint_health_roh)

    # T-0151: Hahn-Cluster (Durchfluss-Budget). Default leer = kein Lock.
    hahn_cluster_roh = roh.get("hahn_cluster") or []
    hahn_cluster_liste = [HahnCluster(**c) for c in hahn_cluster_roh]

    # T-0252: Multi-DSWC-Eindeutigkeit. Wenn mehrere Zonen denselben
    # `ventil_kanal` teilen, MUSS jede eine explizite `ventil_geraet_id`
    # haben — sonst kollidiert die Live-Event-Expansion mit anderen
    # DSWCs (Phantom-Events in der falschen Zone). Zonen am gleichen
    # `(kanal, geraet_id)` sind erlaubt: das sind echte Geschwister-
    # Cluster (z. B. bambuswald + bambuswald_yogaraum am Mikrodrip).
    kanal_gruppen: dict[int, list[ZonenKonfig]] = {}
    for z in zonen:
        if z.ventil_kanal is not None:
            kanal_gruppen.setdefault(z.ventil_kanal, []).append(z)
    for kanal, gruppe in kanal_gruppen.items():
        if len(gruppe) <= 1:
            continue
        ohne_geraet = [z.zone_id for z in gruppe if not z.ventil_geraet_id]
        if ohne_geraet:
            mit_geraet = [
                z.zone_id for z in gruppe if z.ventil_geraet_id
            ]
            raise ValueError(
                f"Konfig-Fehler T-0252: ventil_kanal={kanal} hat "
                f"mehrere Zonen ({[z.zone_id for z in gruppe]}), "
                f"aber {ohne_geraet} ohne `ventil_geraet_id`. "
                f"Multi-DSWC-Routing braucht explizite Geraete-"
                f"Zuordnung -- sonst expandieren Live-Events des "
                f"einen DSWC faelschlich auf Zonen des anderen "
                f"DSWC. Fix: `ventil_geraet_id` der jeweiligen "
                f"DSWC-UUID setzen. (Zonen mit gesetzter ID: "
                f"{mit_geraet or '[]'}.)"
            )

    # T-0204: Klartextnamen pro Sensor-Geraet-ID fuer die Diagnose-Liste.
    # FYTA-Namen aus `fyta.pflanzen[].name` automatisch vor-belegen
    # (Schluessel `fyta_<fyta_id>`), damit Andre sie nicht doppelt
    # pflegen muss. Manuelle Eintraege in `sensor_namen:` ueberschreiben.
    sensor_namen_roh = dict(roh.get("sensor_namen") or {})
    if fyta_konfig is not None:
        for p in fyta_konfig.pflanzen:
            schluessel = f"fyta_{p.fyta_id}"
            if schluessel not in sensor_namen_roh and p.name:
                sensor_namen_roh[schluessel] = p.name

    return GesamtKonfig(
        gardena=GardenaKonfig(
            client_id=gardena_roh.get("client_id", ""),
            client_secret=gardena_roh.get("client_secret", ""),
        ),
        zonen=zonen,
        wetter=WetterKonfig(
            standorte=wetter_standorte,
            breite=breite,
            laenge=laenge,
            cache_minuten=wetter_roh.get("cache_minuten", 30),
            regen_schwelle_mm=wetter_roh.get("regen_schwelle_mm", 2.0),
            regen_wahrscheinlichkeit_schwelle_prozent=wetter_roh.get(
                "regen_wahrscheinlichkeit_schwelle_prozent", 80.0,
            ),
        ),
        speicher=SpeicherKonfig(
            db_pfad=str(
                PROJEKT_ROOT / "backend" / speicher_roh.get("db_pfad", "./daten/bewaesserung.db")
            ),
        ),
        standorte=standorte,
        balkon_ausrichtung=balkon_ausrichtung,
        fyta=fyta_konfig,
        ventilsteuerung_aktiv=roh.get("ventilsteuerung_aktiv", False),
        schwellen_adaption=schwellen_adaption,
        bilanz=bilanz_konfig,
        backup=backup_konfig,
        ml_retrain=ml_retrain_konfig,
        ml_bewaesserungs_response=ml_bewaesserungs_response_konfig,
        ml_ausschluss_fenster=ml_ausschluss,
        kalibrierung=kalibrierung_konfig,
        ml_skalen_mapping=ml_skalen_mapping_konfig,
        ml_physik_diagnose=ml_physik_diagnose_konfig,
        ml_wirkung_fit=ml_wirkung_fit_konfig,
        ml_state_space=ml_state_space_konfig,
        ml_forecast_routing=ml_forecast_routing_konfig,
        wochen_report=wochen_report_konfig,
        watchdog=watchdog_konfig,
        endpoint_health=endpoint_health_konfig,
        hahn_cluster=hahn_cluster_liste,
        sensor_namen=sensor_namen_roh,
        log_level=roh.get("logging", {}).get("level", "INFO"),
    )
