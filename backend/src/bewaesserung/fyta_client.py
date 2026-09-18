"""Async FYTA-API-Client mit Polling-Loop.

Pollt regelmaessig aktuelle Sensorwerte und wandelt sie in SensorMessung um.
Nutzt httpx (async) statt urllib wie der Export-Client.

T-0072 (2026-04-23): Login-Flow + Auto-Refresh.
Bisher wurde ein vom User manuell extrahiertes `FYTA_ACCESS_TOKEN` aus der
Web-LocalStorage genutzt — das veraltete nach ~2 Wochen und musste manuell
erneuert werden. Jetzt: Email+Passwort aus `.env`, der Client holt sich
beim Start selbst ein Token, persistiert es in
`backend/daten/fyta_customer_token.json`, und refresht bei 401/403
automatisch. Login-Pattern (aus `fyta_cli` v-check 2026-04-23 verifiziert):
  POST https://web.fyta.de/api/auth/login
  Authorization: Basic <base64(email:password)>
  Content-Type: application/json
  Body: {"email": ..., "password": ...}
Response erfolg: `{access_token, expires_in}` (+ optional refresh_token).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import httpx
import structlog

from bewaesserung.modelle import (
    ist_mehrdeutige_lokalzeit,
    DatenQuelle,
    FytaGeraeteStatus,
    FytaKonfig,
    FytaPflanzenKonfig,
    SensorMessung,
)

logger = structlog.get_logger()

# Default-Pfad fuer den Token-Cache (wird in Tests via Parameter ueberschrieben).
# Absolut unter backend/daten, damit Start-CWD keinen zweiten Token-Cache
# unter Projektroot/daten erzeugt.
FYTA_TOKEN_PFAD_DEFAULT = (
    Path(__file__).resolve().parents[2] / "daten" / "fyta_customer_token.json"
)
# Sicherheits-Puffer: Login-Refresh schon 5 min vor Ablauf triggern.
LOGIN_PUFFER = timedelta(minutes=5)


def parse_fyta_zeitstempel(roh: str) -> datetime:
    """T-0176: FYTA liefert Zeitstempel im Feld `date_utc` als ISO-String
    OHNE Zonen-Suffix (z. B. "2026-05-10T09:16:50"). Naive `fromisoformat`
    interpretiert das als lokal -> 2 h zu frueh im Sommer (CEST).

    Korrekt: als UTC interpretieren, in lokale Zone konvertieren, dann
    `tzinfo=None` (alle DB-Zeitstempel sind im Projekt naive lokal).
    """
    if "+" not in roh and "Z" not in roh:
        roh = roh + "+00:00"
    elif roh.endswith("Z"):
        roh = roh[:-1] + "+00:00"
    # T-0566: gleiche Mehrdeutigkeit wie im DHS-Parser. Am 25.10.2026 gibt
    # es 02:00-03:00 lokal zweimal; zwei FYTA-Messungen eine Stunde
    # auseinander bekommen denselben naiven Stempel, und der UNIQUE-Index
    # `(zone_id, geraet_id, zeitstempel)` verwirft die zweite still.
    # Ausgefuehrt reproduziert: zwei geschriebene Messungen, eine in der DB,
    # der zweite Wert weg. Das verletzt
    # [[feedback_messdaten_vollstaendig_behalten]] -- deshalb wenigstens
    # sichtbar machen, statt es lautlos passieren zu lassen.
    lokal = datetime.fromisoformat(roh).astimezone()
    if ist_mehrdeutige_lokalzeit(lokal):
        logger.warning(
            "fyta.zeitstempel_mehrdeutig",
            roh=str(roh),
            lokal=lokal.replace(tzinfo=None).isoformat(),
            hinweis="Wiederholungsstunde der Zeitumstellung -- eine der "
                    "beiden Messungen faellt aus dem UNIQUE-Index",
        )
    return lokal.replace(tzinfo=None)


class FytaClient:
    """Async FYTA-Client fuer Live-Polling von Pflanzensensoren."""

    def __init__(
        self,
        konfig: FytaKonfig,
        access_token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        token_pfad: Path | None = None,
    ):
        self._konfig = konfig
        # Prioritaet: (1) expliziter Parameter, (2) persistierter Cache,
        # (3) alter .env-Override FYTA_ACCESS_TOKEN (Backward-Compat, faellt
        # ab T-0072 Default weg). (2) und (3) werden im ersten Login-Check
        # ausgewertet.
        self._token = access_token or os.environ.get("FYTA_ACCESS_TOKEN", "")
        self._email = email or os.environ.get("FYTA_EMAIL", "").strip()
        self._password = password or os.environ.get("FYTA_PASSWORD", "")
        self._token_ablauf: datetime | None = None
        self._token_pfad = token_pfad or FYTA_TOKEN_PFAD_DEFAULT
        self._lade_token_cache()
        self._pflanzen_map: dict[int, FytaPflanzenKonfig] = {
            p.fyta_id: p for p in konfig.pflanzen
        }
        self._callback = None
        self._aktiv = False
        self._login_lock = asyncio.Lock()
        # T-0166: In-Memory-Dedup. FYTA returnt pro Pflanze ein ARRAY von
        # Messungen im scanFromDate..scanToDate-Range. Vor T-0166 nahm der
        # Code nur `eintraege[-1]` -> alle anderen 5-15 Messungen pro
        # Polling-Tick gingen verloren (besonders sichtbar bei Pilea, die
        # nach FYTA-internem "veraltete Daten"-Status einen Burst liefert).
        # Tracking des letzten Zeitstempels, um Doppelungen zu vermeiden
        # (sensor_messung hat kein UNIQUE-Constraint).
        # T-0224: Schluessel ist die `geraet_id` (fyta_<plant_id>), NICHT
        # die zone_id. Eine Zone kann mehrere FYTA-Sensoren haben
        # (waldblumenhain: 2x) -- eine zonenweite Schwelle laesst den
        # Sensor mit den spaeteren Zeitstempeln den anderen aushungern
        # (dessen Messungen gelten faelschlich als "nicht neu").
        self._letzter_zeitstempel: dict[str, datetime] = {}

    def registriere_callback(self, callback) -> None:
        """Registriert eine Callback-Funktion fuer neue Messungen."""
        self._callback = callback

    # ---------- Token-Verwaltung (T-0072) ----------

    def _lade_token_cache(self) -> None:
        """Laedt persistiertes Token wenn vorhanden + noch gueltig."""
        if self._token:
            return  # explizites Token (Env/Param) hat Vorrang
        try:
            roh = self._token_pfad.read_text()
        except FileNotFoundError:
            return
        except Exception:
            logger.exception("fyta.token_cache_lesefehler", pfad=str(self._token_pfad))
            return
        try:
            daten = json.loads(roh)
            self._token = str(daten.get("access_token", ""))
            ablauf = daten.get("expires_at")
            if ablauf:
                self._token_ablauf = datetime.fromisoformat(ablauf)
        except Exception:
            logger.warning("fyta.token_cache_parse_fehler", pfad=str(self._token_pfad))
            self._token = ""
            self._token_ablauf = None

    def _speichere_token_cache(self) -> None:
        """Persistiert Token+Ablauf fuer Restart-Reuse."""
        try:
            self._token_pfad.parent.mkdir(parents=True, exist_ok=True)
            # T-0557: atomar + 0600, wie `gardena_customer_auth._speichere_cache`.
            # Vorher `write_text` ohne chmod -> die Datei stand mit 0644 auf der
            # Platte (nachgesehen: 0644 hier, 0600 beim Gardena-Token daneben),
            # der Bearer-Token war also fuer jeden lokalen Nutzer lesbar. Das
            # `os.chmod` VOR dem `replace`, damit die Zieldatei nie kurz mit
            # zu weiten Rechten existiert.
            tmp = self._token_pfad.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "access_token": self._token,
                "expires_at": self._token_ablauf.isoformat() if self._token_ablauf else None,
            }))
            os.chmod(tmp, 0o600)
            tmp.replace(self._token_pfad)
        except Exception:
            logger.exception("fyta.token_cache_schreibfehler", pfad=str(self._token_pfad))

    def _token_noch_gueltig(self) -> bool:
        if not self._token:
            return False
        if self._token_ablauf is None:
            # Altes .env-Token ohne bekanntes Ablaufdatum — fuer die
            # Backward-Compat akzeptieren wir es, refreshen es aber auf
            # 401/403 via Login.
            return True
        return datetime.now() + LOGIN_PUFFER < self._token_ablauf

    async def _login(self) -> bool:
        """Holt frisches Access-Token via Email+Passwort.

        Pattern verifiziert via `fyta_cli`: Basic-Auth im Header
        zusaetzlich zur JSON-Body-Copy. Rueckgabe True bei Erfolg.
        """
        if not self._email or not self._password:
            logger.warning(
                "fyta.login_keine_credentials",
                hinweis="FYTA_EMAIL/FYTA_PASSWORD in .env setzen",
            )
            return False
        payload = {"email": self._email, "password": self._password}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                antwort = await client.post(
                    f"{self._konfig.api_url}/auth/login",
                    auth=(self._email, self._password),
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            logger.warning("fyta.login_netzwerk_fehler", fehler=str(exc))
            return False

        if antwort.status_code == 404:
            logger.error("fyta.login_auth_fehlgeschlagen",
                         hint="Email/Passwort falsch oder Endpoint geaendert")
            return False
        if antwort.status_code == 401:
            logger.error("fyta.login_passwort_falsch")
            return False
        if antwort.status_code >= 400:
            logger.error(
                "fyta.login_fehler",
                status=antwort.status_code,
                body=antwort.text[:200],
            )
            return False

        try:
            daten = antwort.json()
            self._token = str(daten["access_token"])
            expires_in = int(daten.get("expires_in", 0))
        except (KeyError, ValueError, TypeError) as exc:
            logger.exception("fyta.login_antwort_ungueltig", fehler=str(exc))
            return False

        if expires_in > 0:
            self._token_ablauf = datetime.now() + timedelta(seconds=expires_in)
        else:
            # Konservativer Fallback: 12 h, wenn FYTA keinen expires_in liefert.
            self._token_ablauf = datetime.now() + timedelta(hours=12)
        self._speichere_token_cache()
        logger.info("fyta.login_erfolg",
                    ablauf=self._token_ablauf.isoformat(timespec="seconds"))
        return True

    async def _stelle_token_sicher(self) -> bool:
        """Login wenn Token fehlt oder abgelaufen. Lock-serialisiert."""
        if self._token_noch_gueltig():
            return True
        async with self._login_lock:
            if self._token_noch_gueltig():  # Double-check nach Lock
                return True
            return await self._login()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _mit_auth_retry(self, ruf):
        """Fuehrt `ruf()` aus; bei 401/403 einmal neu einloggen + retry.

        `ruf` ist ein Callable, das einen httpx.AsyncClient bekommt und ein
        httpx.Response liefert. Der Caller kuemmert sich um Parsing.
        """
        await self._stelle_token_sicher()
        async with httpx.AsyncClient(timeout=20.0) as client:
            antwort = await ruf(client)
            if antwort.status_code in (401, 403):
                logger.info("fyta.token_abgelaufen_reauth")
                # Cache invalidieren und frisch einloggen.
                self._token_ablauf = datetime.now() - timedelta(seconds=1)
                if await self._stelle_token_sicher():
                    antwort = await ruf(client)
            return antwort

    async def hole_pflanzen(self) -> list[dict]:
        """Holt alle Pflanzen des Users (fuer Discovery)."""
        async def _ruf(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                f"{self._konfig.api_url}/user-plant",
                headers=self._headers(),
            )
        antwort = await self._mit_auth_retry(_ruf)
        antwort.raise_for_status()
        daten = antwort.json()
        pflanzen = daten.get("plants", [])
        logger.info("fyta.pflanzen_geladen", anzahl=len(pflanzen))
        return pflanzen

    async def hole_geraete_status(self) -> list[FytaGeraeteStatus]:
        """T-0527: Geraete-Zustand aller konfigurierten Pflanzen.

        Zwei Quellen, weil FYTA die Felder aufteilt:

        - `GET /user-plant` liefert `wifi_status`, `isOutdated` und das
          `sensor`-Objekt (`status`, `version`, `is_battery_low`).
        - `GET /user-plant/<id>` liefert zusaetzlich
          `plant.sensors[0].battery_level` (0-100) -- **nur dort**, die
          Liste kennt den Wert nicht. Kostet einen Call pro Pflanze.

        Deshalb ist dieser Aufruf teuer (1 + N Requests) und gehoert in
        einen eigenen, langsam getakteten Job, nicht in den 15-min-Poll.

        Nicht beschaffbar und deshalb auch nicht modelliert: RSSI bzw.
        Signalstaerke, Batteriespannung, Fehlercodes, Reboot-Zaehler.
        Geprueft 08.08.2026 gegen elf weitere Endpoint-Pfade, alle 404/422.

        Fehlerverhalten: leere Liste statt Exception. Der Aufrufer ist ein
        Diagnose-Job -- er darf den Entscheidungs-Loop nicht mitreissen.
        """
        if not self._pflanzen_map:
            return []

        async def _liste(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                f"{self._konfig.api_url}/user-plant", headers=self._headers(),
            )

        try:
            antwort = await self._mit_auth_retry(_liste)
            antwort.raise_for_status()
            pflanzen = antwort.json().get("plants", [])
        except httpx.HTTPError as exc:
            logger.warning("fyta.status_liste_fehlgeschlagen", fehler=str(exc))
            return []
        except Exception:
            logger.exception("fyta.status_liste_unerwarteter_fehler")
            return []

        jetzt = datetime.now()
        ergebnis: list[FytaGeraeteStatus] = []

        for pflanze in pflanzen:
            plant_id = pflanze.get("id") or pflanze.get("user_plant_id")
            konfig = self._pflanzen_map.get(plant_id)
            if konfig is None:
                continue  # nicht konfiguriert -> geht uns nichts an

            sensor = pflanze.get("sensor") or {}

            # battery_level nur im Detail-Endpoint. Schlaegt der Call fehl,
            # wird der Rest trotzdem geschrieben -- ein fehlender Akkuwert
            # ist kein Grund, Status und Zeitstempel zu verlieren.
            battery = None
            try:
                async def _detail(client: httpx.AsyncClient) -> httpx.Response:
                    return await client.get(
                        f"{self._konfig.api_url}/user-plant/{plant_id}",
                        headers=self._headers(),
                    )
                d = await self._mit_auth_retry(_detail)
                d.raise_for_status()
                sensoren = (d.json().get("plant") or {}).get("sensors") or []
                if sensoren:
                    battery = _safe_float(sensoren[0].get("battery_level"))
            except Exception as exc:
                logger.warning(
                    "fyta.status_detail_fehlgeschlagen",
                    plant_id=plant_id, fehler=str(exc),
                )

            empfangen = sensor.get("received_data_at") or pflanze.get("received_data_at")
            try:
                letzte = parse_fyta_zeitstempel(empfangen) if empfangen else None
            except (ValueError, TypeError):
                letzte = None

            ergebnis.append(FytaGeraeteStatus(
                zeitstempel=jetzt,
                geraet_id=f"fyta_{plant_id}",
                plant_id=int(plant_id),
                sensor_id=str(sensor.get("id") or ""),
                zone_id=konfig.zone_id,
                battery_level=battery,
                is_battery_low=sensor.get("is_battery_low"),
                sensor_status=sensor.get("status"),
                wifi_status=pflanze.get("wifi_status"),
                hub_status=(pflanze.get("hub") or {}).get("status"),
                is_outdated=pflanze.get("isOutdated"),
                firmware=str(sensor.get("version") or ""),
                last_data_received_at=letzte,
            ))

        logger.info("fyta.geraete_status_geholt", geraete=len(ergebnis))
        return ergebnis

    async def hole_plant_optimum(
        self, fyta_id: int,
    ) -> dict[str, float] | None:
        """T-0050b: Holt die Feuchte-Optimum-Range einer Pflanze aus der
        Detail-API. Rueckgabe: `{min, max, min_akzeptabel, max_akzeptabel}`
        in Prozent, oder None bei Fehler/Missing-Feldern.

        FYTA-Response-Schema (verifiziert 2026-04-20):
            plant.measurements.moisture.values.min_good    → min
            plant.measurements.moisture.values.max_good    → max
            plant.measurements.moisture.values.min_acceptable / max_acceptable
        """
        async def _ruf(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                f"{self._konfig.api_url}/user-plant/{fyta_id}",
                headers=self._headers(),
            )
        try:
            antwort = await self._mit_auth_retry(_ruf)
            antwort.raise_for_status()
            daten = antwort.json()
        except httpx.HTTPError as exc:
            logger.warning(
                "fyta.plant_detail_fehler", fyta_id=fyta_id, fehler=str(exc),
            )
            return None
        except Exception:
            logger.exception("fyta.plant_detail_unerwartet", fyta_id=fyta_id)
            return None

        try:
            werte = daten["plant"]["measurements"]["moisture"]["values"]
            # FYTA liefert Strings — defensiv konvertieren
            return {
                "min": float(werte["min_good"]),
                "max": float(werte["max_good"]),
                "min_akzeptabel": float(werte.get("min_acceptable", werte["min_good"])),
                "max_akzeptabel": float(werte.get("max_acceptable", werte["max_good"])),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "fyta.plant_optimum_parsing_fehler",
                fyta_id=fyta_id, fehler=str(exc),
            )
            return None

    async def hole_plant_optima_alle_achsen(
        self, fyta_id: int,
    ) -> dict[str, dict] | None:
        """T-0196: Optimum-Schwellen ALLER messbaren Achsen pro Pflanze.

        FYTA Plant-Detail-API (`GET /user-plant/{id}`) liefert pro Achse
        einen `measurements.{achse}`-Knoten mit `values.{min_good,
        max_good, min_acceptable, max_acceptable, current}` plus
        `dli_values` fuer Licht (PPFD vs. Daily Light Integral) und
        `unit` als Einheits-String.

        Returns: dict[achse, dict[bound, value]] oder None bei Fehler.

        Achsen (deutsche Identifier, intern fuer plant_optimum_achse):
        - `feuchte` (FYTA moisture, Einheit %/h)
        - `licht_ppfd` (FYTA light.values, μmol/h)
        - `licht_dli` (FYTA light.dli_values, mol/day)
        - `temperatur` (FYTA temperature, °C/h)
        - `salinitaet` (FYTA salinity, mS/cm/h)

        Felder pro Achse: min_good, max_good, min_akzeptabel,
        max_akzeptabel (alle float|None), einheit (str), current
        (float|None — fuer Polling).

        Ignoriert: air_humidity und ph (current=None bei allen
        gesichteten Pflanzen 16.05.), nutrients (nur Status-int, keine
        Schwellen), battery (kein pflanzenbezogenes Optimum).
        """
        async def _ruf(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                f"{self._konfig.api_url}/user-plant/{fyta_id}",
                headers=self._headers(),
            )
        try:
            antwort = await self._mit_auth_retry(_ruf)
            antwort.raise_for_status()
            daten = antwort.json()
        except httpx.HTTPError as exc:
            logger.warning(
                "fyta.plant_detail_fehler", fyta_id=fyta_id, fehler=str(exc),
            )
            return None
        except Exception:
            logger.exception("fyta.plant_detail_unerwartet", fyta_id=fyta_id)
            return None

        try:
            m = daten["plant"]["measurements"]
        except (KeyError, TypeError) as exc:
            logger.warning(
                "fyta.plant_detail_struktur_ungueltig",
                fyta_id=fyta_id, fehler=str(exc),
            )
            return None

        def _parse_values(node: dict, schluessel: str) -> dict | None:
            """Pickt min/max-Schwellen + current aus values/dli_values.

            FYTA-Werte sind Strings, defensiv konvertieren. Bei Parse-
            Fehler wird das einzelne Feld None, der Eintrag insgesamt
            bleibt erhalten (sonst verlieren wir die anderen Schwellen).
            """
            v = node.get(schluessel)
            if not isinstance(v, dict):
                return None
            eintrag: dict = {}
            for src, dst in (
                ("min_good", "min_good"),
                ("max_good", "max_good"),
                ("min_acceptable", "min_akzeptabel"),
                ("max_acceptable", "max_akzeptabel"),
            ):
                roh = v.get(src)
                if roh is None:
                    eintrag[dst] = None
                else:
                    try:
                        eintrag[dst] = float(roh)
                    except (TypeError, ValueError):
                        eintrag[dst] = None
            cur = v.get("current")
            if cur is None:
                eintrag["current"] = None
            else:
                try:
                    eintrag["current"] = float(cur)
                except (TypeError, ValueError):
                    eintrag["current"] = None
            return eintrag

        ergebnis: dict[str, dict] = {}

        # Achsen-Mapping FYTA-Knotenname -> intern (deutscher Identifier)
        # plus optionaler Default fuer Einheit (FYTA liefert sie meist
        # im node.unit-Feld, Fallback wenn Feld fehlt).
        achsen_mapping = [
            ("moisture", "feuchte", "values", "%/h"),
            ("light", "licht_ppfd", "values", "μmol/h"),
            ("light", "licht_dli", "dli_values", "mol/day"),
            ("temperature", "temperatur", "values", "°C/h"),
            ("salinity", "salinitaet", "values", "mS/cm/h"),
        ]

        for fyta_name, intern, value_key, einheit_default in achsen_mapping:
            node = m.get(fyta_name)
            if not isinstance(node, dict):
                continue
            werte = _parse_values(node, value_key)
            if werte is None:
                continue
            # Einheit: bei dli_values aus dli_unit, sonst aus unit
            unit_field = "dli_unit" if value_key == "dli_values" else "unit"
            werte["einheit"] = node.get(unit_field, einheit_default)
            ergebnis[intern] = werte

        logger.debug(
            "fyta.plant_optima_alle_achsen",
            fyta_id=fyta_id,
            achsen=list(ergebnis.keys()),
        )
        return ergebnis or None

    async def hole_aktuelle_werte(self) -> list[SensorMessung]:
        """Holt aktuelle Messwerte fuer alle konfigurierten Pflanzen."""
        if not self._pflanzen_map:
            logger.warning("fyta.keine_pflanzen_konfiguriert")
            return []

        scan_to = date.today()
        scan_from = scan_to - timedelta(days=1)
        heute = scan_to.isoformat()
        plant_ids = list(self._pflanzen_map.keys())

        payload = {
            "userPlantIds": plant_ids,
            "scanFromDate": scan_from.isoformat(),
            "scanToDate": heute,
        }
        async def _ruf(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                f"{self._konfig.api_url}/user-plant/list-measurements",
                headers=self._headers(),
                json=payload,
            )
        try:
            antwort = await self._mit_auth_retry(_ruf)
            antwort.raise_for_status()
            daten = antwort.json()
        except httpx.HTTPError as exc:
            logger.warning("fyta.abfrage_fehlgeschlagen", fehler=str(exc))
            return []
        except Exception:
            logger.exception("fyta.abfrage_unerwarteter_fehler")
            return []

        messungen = []
        user_plants = daten.get("user_plants", [])

        for pflanze in user_plants:
            plant_id = pflanze.get("user_plant_id") or pflanze.get("id")
            if plant_id not in self._pflanzen_map:
                continue

            konfig = self._pflanzen_map[plant_id]
            geraet_id = f"fyta_{plant_id}"
            eintraege = pflanze.get("measurements", [])

            if not eintraege:
                continue

            # T-0166: ALLE Eintraege im Range uebernehmen, nicht nur den
            # letzten. FYTA returnt pro Pflanze typisch 5-15 Datenpunkte
            # im scanFromDate..scanToDate-Range (Beam-Sensor sendet alle
            # 3-4 h). Der bisherige Code `eintraege[-1]` warf alles bis
            # auf einen weg. In-Memory-Dedup gegen `_letzter_zeitstempel`
            # vermeidet Doppelungen, da sensor_messung kein UNIQUE hat.
            # T-0224: Dedup-Schwelle pro geraet_id -- bei Multi-FYTA-Zonen
            # (waldblumenhain, 2 Sensoren) darf sie nicht zonenweit
            # geteilt werden, sonst hungern sich die Sensoren aus.
            schwellen_zeit = self._letzter_zeitstempel.get(geraet_id)
            neue_pro_sensor = 0
            for eintrag in eintraege:
                try:
                    # T-0176: UTC-konvertierter Zeitstempel statt naiv lesen.
                    zeitstempel = parse_fyta_zeitstempel(
                        eintrag.get("date_utc", heute + "T00:00:00")
                    )
                except (ValueError, TypeError):
                    zeitstempel = datetime.now()

                # Dedup: nur Messungen NEUER als die zuletzt gesehene.
                if schwellen_zeit is not None and zeitstempel <= schwellen_zeit:
                    continue

                messungen.append(SensorMessung(
                    zeitstempel=zeitstempel,
                    zone_id=konfig.zone_id,
                    geraet_id=geraet_id,
                    boden_feuchte=_safe_float(eintrag.get("soil_moisture")),
                    boden_temperatur=_safe_float(eintrag.get("temperature")),
                    licht=_safe_float(eintrag.get("light")),
                    boden_fruchtbarkeit=_safe_float(eintrag.get("soil_fertility")),
                    quelle=DatenQuelle.FYTA,
                ))
                neue_pro_sensor += 1
                # Schwelle nachfuehren — jeder Eintrag muss neuer sein
                # als der hoechste bisher gesehene dieser Zone.
                if (
                    schwellen_zeit is None
                    or zeitstempel > schwellen_zeit
                ):
                    schwellen_zeit = zeitstempel

            # Schwelle persistieren fuer naechsten Polling-Tick.
            if schwellen_zeit is not None:
                self._letzter_zeitstempel[geraet_id] = schwellen_zeit

            if neue_pro_sensor > 0:
                logger.debug(
                    "fyta.zone_neue_messungen",
                    zone=konfig.zone_id,
                    geraet=geraet_id,
                    neue=neue_pro_sensor,
                    eintraege_total=len(eintraege),
                )

        logger.info(
            "fyta.werte_geholt",
            pflanzen=len(messungen),
            gesamt_konfiguriert=len(self._pflanzen_map),
        )
        return messungen

    async def initialisiere_dedup_aus_db(self, speicher) -> None:
        """T-0166: Dedup-Schwellen aus der DB laden (beim Service-Start).

        Verhindert, dass nach einem Restart die letzten 1-2 Tage erneut
        komplett geschrieben werden. Liest pro konfiguriertem FYTA-Sensor
        (geraet_id) die letzte vorhandene Messung und setzt damit die
        In-Memory-Schwelle.

        T-0224: pro `geraet_id`, nicht pro `zone_id` -- bei Zonen mit
        mehreren FYTA-Sensoren (waldblumenhain: 2x) braucht jeder Sensor
        seine eigene Schwelle, sonst setzt der Init eine gemeinsame
        (die juengste der Zone) und der langsamere Sensor wird beim
        ersten Poll faelschlich komplett wegdedupt.
        """
        for plant_id in self._pflanzen_map:
            geraet_id = f"fyta_{plant_id}"
            try:
                letzte = await speicher.letzte_messung_geraet(geraet_id)
            except Exception:
                logger.exception(
                    "fyta.dedup_init_fehler", geraet=geraet_id,
                )
                continue
            if letzte and letzte.zeitstempel is not None:
                self._letzter_zeitstempel[geraet_id] = letzte.zeitstempel
        logger.info(
            "fyta.dedup_init",
            sensoren_mit_schwelle=len(self._letzter_zeitstempel),
        )

    async def starte_polling(self) -> None:
        """Polling-Loop — laeuft als asyncio-Task im Hintergrund."""
        self._aktiv = True
        intervall = self._konfig.poll_intervall_sekunden
        logger.info("fyta.polling_gestartet", intervall_s=intervall)

        while self._aktiv:
            try:
                messungen = await self.hole_aktuelle_werte()
                if self._callback and messungen:
                    for messung in messungen:
                        await self._callback(messung)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fyta.polling_fehler")

            await asyncio.sleep(intervall)

    def stoppe(self) -> None:
        """Stoppt den Polling-Loop."""
        self._aktiv = False
        logger.info("fyta.polling_gestoppt")


def _safe_float(wert) -> float | None:
    """Wandelt einen Wert sicher in float um, None bei Fehler."""
    if wert is None:
        return None
    try:
        return float(wert)
    except (ValueError, TypeError):
        return None
