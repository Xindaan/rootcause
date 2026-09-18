"""T-0132 (H-8): Endpoint-Health-Check fuer inoffizielle Schnittstellen.

Adressiert Pre-Mortem-Akt 4 (DHS-Schema-Drift kippt stillschweigend) +
FYTA-Schema-Drift (T-0072 Auto-Login funktioniert HEUTE -- kein
Versprechen fuer uebermorgen).

Strategie: 1x/Tag pro Endpoint einen kontrollierten Probe-Call gegen
die echte API. Schema validieren -- nicht nur HTTP 200 zaehlen, sondern
pruefen, dass die erwarteten Felder vorhanden sind. Status persistiert
in Tabelle `endpoint_health`. Watchdog (Trigger C) konsumiert den
Status und feuert iMessage bei laenger anhaltenden Fehlern.

Bewusst kein eigener Auth-Flow -- die existierenden Auth-Komponenten
(`gardena_customer_auth`, `fyta_client`) werden injiziert. Der Job
testet nur "kommt valides Schema?", nicht "kann ich mich neu
einloggen?".
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from bewaesserung.speicher import Speicher

logger = structlog.get_logger()

ENDPOINT_DHS = "gardena_dhs"
ENDPOINT_FYTA = "fyta"

# T-0290: eigener Status fuer DNS-/Offline-Probe-Fehler -- der Watchdog
# pusht darauf NICHT (host-seitig offline, nicht der Endpoint kaputt).
STATUS_HOST_OFFLINE = "host_offline"

DHS_PROBE_URL_TEMPLATE = "https://smart.gardena.com/v1/dhs/{sensor_uuid}"
HTTP_TIMEOUT_S = 15.0


def _ist_dns_fehler(exc: Exception) -> bool:
    """T-0290: DNS-/Namensaufloesungs-Fehler = der HOST ist offline
    (lokaler Resolver/Netz weg), NICHT der Endpoint kaputt.

    Auf einem mobilen Laptop (Schlaf/Pendeln) faellt der 24h-Single-Shot-
    Probe regelmaessig in so ein Offline-Fenster -> ohne diese
    Unterscheidung Falsch-Alarm 'connect_fehler' per Watchdog-iMessage.
    Deckt macOS + Linux (Pi, T-0057) ab.
    """
    text = str(exc).lower()
    return any(s in text for s in (
        "nodename nor servname",                  # macOS (Errno 8)
        "name or service not known",              # Linux (Errno -2)
        "temporary failure in name resolution",   # Linux (Errno -3)
        "getaddrinfo",
    ))


class EndpointHealthJob:
    """Periodischer Schema-Check pro Endpoint."""

    def __init__(
        self,
        speicher: Speicher,
        intervall_stunden: int = 24,
        gardena_auth: Any | None = None,
        gardena_location_id: str | None = None,
        gardena_probe_sensor_uuid: str | None = None,
        fyta_client: Any | None = None,
    ) -> None:
        self._speicher = speicher
        self._intervall = timedelta(hours=max(intervall_stunden, 1))
        self._gardena_auth = gardena_auth
        self._gardena_location_id = gardena_location_id
        self._gardena_probe_sensor_uuid = gardena_probe_sensor_uuid
        self._fyta_client = fyta_client
        self._letzte_aktualisierung: datetime | None = None

    async def aktualisiere_wenn_faellig(
        self, jetzt: datetime | None = None,
    ) -> dict:
        """Laeuft max. einmal pro `intervall_stunden`. Stat-Dict zurueck."""
        jetzt = jetzt or datetime.now()
        if (
            self._letzte_aktualisierung
            and (jetzt - self._letzte_aktualisierung) < self._intervall
        ):
            return {"geprueft": 0}
        self._letzte_aktualisierung = jetzt

        ergebnisse = {}
        if self._gardena_auth and self._gardena_probe_sensor_uuid:
            ergebnisse[ENDPOINT_DHS] = await self._pruefe_dhs(jetzt)
        if self._fyta_client:
            ergebnisse[ENDPOINT_FYTA] = await self._pruefe_fyta(jetzt)
        logger.info("endpoint_health.tick", **ergebnisse)
        return {"geprueft": len(ergebnisse), **ergebnisse}

    async def _pruefe_dhs(self, jetzt: datetime) -> str:
        """Probe-Call gegen den DHS-Sensor-Endpoint + Schema-Validierung.

        Erwartet: `data` ist eine Liste mit mindestens einem
        `dh-point-serie`-Eintrag, dessen `attributes['property-name']`
        in {humidity, temperature} liegt. Schema-Drift = wenn
        `property-name` umbenannt wird (z. B. zu `propertyName`).
        """
        try:
            token = await self._gardena_auth.hole_gueltigen_token()
        except Exception as exc:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_DHS, "auth_fehler", jetzt, details=str(exc)[:200],
            )
            return "auth_fehler"

        url = DHS_PROBE_URL_TEMPLATE.format(
            sensor_uuid=self._gardena_probe_sensor_uuid,
        )
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                antwort = await client.get(
                    url,
                    params={
                        "preset": "sensor2",
                        "location_id": self._gardena_location_id,
                    },
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.api+json",
                    },
                )
        except Exception as exc:
            status = STATUS_HOST_OFFLINE if _ist_dns_fehler(exc) else "connect_fehler"
            await self._speicher.setze_endpoint_health(
                ENDPOINT_DHS, status, jetzt, details=str(exc)[:200],
            )
            return status

        if antwort.status_code != 200:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_DHS, "auth_fehler" if antwort.status_code in (401, 403)
                else "connect_fehler",
                jetzt,
                details=f"HTTP {antwort.status_code}",
            )
            return f"http_{antwort.status_code}"

        try:
            daten = antwort.json()
        except Exception:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_DHS, "schema_fehler", jetzt,
                details="Antwort ist kein JSON",
            )
            return "schema_fehler"

        # Schema-Validierung: data[*].type=='dh-point-serie' mit
        # attributes['property-name'] in {humidity, temperature}
        properties = []
        for serie in (daten.get("data") or []):
            if serie.get("type") != "dh-point-serie":
                continue
            attr = serie.get("attributes") or {}
            prop = (attr.get("property-name") or attr.get("property") or "").lower()
            if prop:
                properties.append(prop)
        if not any(p in ("humidity", "temperature") for p in properties):
            await self._speicher.setze_endpoint_health(
                ENDPOINT_DHS, "schema_fehler", jetzt,
                details=f"Keine humidity/temperature-Serie -- gefunden: {properties}",
            )
            return "schema_fehler"

        await self._speicher.setze_endpoint_health(
            ENDPOINT_DHS, "ok", jetzt,
            details=f"properties={properties}",
        )
        return "ok"

    async def _pruefe_fyta(self, jetzt: datetime) -> str:
        """Validiert dass FYTA-API ein gueltiges Schema liefert.

        Zwei Stufen:
        1) Token-Check via `_stelle_token_sicher()` -- ohne Token kein
           Probe-Call. Liefert `auth_fehler` bei Misserfolg.
        2) T-0233 Schema-Probe: kontrollierter
           `POST /user-plant/list-measurements`-Call mit der ersten
           konfigurierten Plant-ID + Validierung dass der erste Eintrag
           im `measurements`-Array die erwarteten Felder hat
           (soil_moisture, temperature, light, soil_fertility). Erkennt
           stille FYTA-Schema-Drift (analog zu `_pruefe_dhs`).

        Vor T-0233 stoppte die Pruefung nach dem Token-Check; FYTA-
        Feldumbenennungen waeren erst beim naechsten echten Polling-Lauf
        sichtbar geworden (mit `fyta.abfrage_unerwarteter_fehler`-Log,
        aber ohne Health-Status-Update + ohne Watchdog-Trigger).
        """
        try:
            erfolg = await self._fyta_client._stelle_token_sicher()
        except Exception as exc:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "auth_fehler", jetzt, details=str(exc)[:200],
            )
            return "auth_fehler"

        if not erfolg:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "auth_fehler", jetzt,
                details="_stelle_token_sicher returnte False",
            )
            return "auth_fehler"

        token = getattr(self._fyta_client, "_token", None)
        if not isinstance(token, str) or not token:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "schema_fehler", jetzt,
                details=f"_token ist {type(token).__name__} statt nicht-leerer str",
            )
            return "schema_fehler"

        # T-0233: Schema-Probe-Call. Ohne konfigurierte Pflanzen geht's
        # nicht -- dann nur Token-Check ohne Schema-Validierung.
        pflanzen_map = getattr(self._fyta_client, "_pflanzen_map", None) or {}
        probe_plant_id = next(iter(pflanzen_map), None)
        if probe_plant_id is None:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "ok", jetzt,
                details=(
                    f"Token ok ({len(token)} Zeichen). Schema-Probe "
                    "uebersprungen: keine FYTA-Pflanze konfiguriert."
                ),
            )
            return "ok"

        konfig = getattr(self._fyta_client, "_konfig", None)
        api_url = getattr(konfig, "api_url", None) if konfig else None
        if not api_url:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "schema_fehler", jetzt,
                details="api_url nicht in fyta_client._konfig gefunden",
            )
            return "schema_fehler"

        heute = jetzt.date().isoformat()
        scan_from = (jetzt - timedelta(days=2)).date().isoformat()
        payload = {
            "userPlantIds": [probe_plant_id],
            "scanFromDate": scan_from,
            "scanToDate": heute,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                antwort = await client.post(
                    f"{api_url}/user-plant/list-measurements",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            status = STATUS_HOST_OFFLINE if _ist_dns_fehler(exc) else "connect_fehler"
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, status, jetzt, details=str(exc)[:200],
            )
            return status

        if antwort.status_code != 200:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA,
                "auth_fehler" if antwort.status_code in (401, 403)
                else "connect_fehler",
                jetzt,
                details=f"HTTP {antwort.status_code} auf list-measurements",
            )
            return f"http_{antwort.status_code}"

        try:
            daten = antwort.json()
        except Exception:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "schema_fehler", jetzt,
                details="Antwort ist kein JSON",
            )
            return "schema_fehler"

        # Schema-Validierung: user_plants[*].measurements[0] hat die
        # vier Pflicht-Felder. Memory `fyta_plant_detail_endpoint.md` +
        # README listen genau diese Felder als FYTA-Stabil-Vertrag.
        pflicht_felder = {
            "soil_moisture", "temperature", "light", "soil_fertility",
        }
        user_plants = daten.get("user_plants") or []
        if not user_plants:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "schema_fehler", jetzt,
                details="Antwort hat kein 'user_plants'-Array",
            )
            return "schema_fehler"

        measurements = user_plants[0].get("measurements") or []
        if not measurements:
            # 200 + leeres Array ist OK: Pflanze hat im 2-Tage-Fenster
            # einfach keine Werte gesendet (FYTA Beam-Cadence 3-4 h, ggf.
            # Bluetooth-Only-Pflanze). Schema-Probe NICHT als Fehler
            # werten -- Token + Endpoint-Form sind valide.
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "ok", jetzt,
                details=(
                    f"Token + Endpoint ok. Plant {probe_plant_id} hat "
                    "keine Messungen im Probe-Fenster (kein Schema-Drift-Beleg)."
                ),
            )
            return "ok"

        ist_felder = set(measurements[0].keys())
        fehlende = pflicht_felder - ist_felder
        if fehlende:
            await self._speicher.setze_endpoint_health(
                ENDPOINT_FYTA, "schema_fehler", jetzt,
                details=(
                    f"Fehlende Pflicht-Felder in measurement[0]: "
                    f"{sorted(fehlende)}; vorhanden: {sorted(ist_felder)[:10]}"
                ),
            )
            return "schema_fehler"

        await self._speicher.setze_endpoint_health(
            ENDPOINT_FYTA, "ok", jetzt,
            details=(
                f"Token + Schema ok (Plant {probe_plant_id}, "
                f"{len(measurements)} Messungen, alle Pflicht-Felder)."
            ),
        )
        return "ok"
