from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import httpx

from integrations.exceptions import ProviderResponseError, ProviderTransientError
from integrations.geocode_maps import parse_retry_after


class OSRMNoRoute(ProviderResponseError):
    pass


@dataclass(frozen=True)
class OSRMRoute:
    geometry: list[list[float]]
    segment_distances_meters: list[float]
    leg_distances_meters: list[float]
    distance_meters: float
    duration_seconds: float


class OSRMClient:
    provider_name = "osrm"

    def __init__(self, *, base_url, transport=None):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"User-Agent": "optimal-refuel-routes/1.0"},
            timeout=httpx.Timeout(20.0, connect=5.0, write=5.0, pool=5.0),
            transport=transport,
        )

    def route_once(self, waypoints):
        waypoints = list(waypoints)
        if len(waypoints) < 2:
            raise ProviderResponseError("OSRM requires at least two waypoints")
        coordinates = ";".join(f"{longitude:.7f},{latitude:.7f}" for latitude, longitude in waypoints)
        try:
            response = self._client.get(
                f"/route/v1/driving/{coordinates}",
                params={
                    "overview": "full",
                    "geometries": "geojson",
                    "steps": "false",
                    "annotations": "distance",
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderTransientError(f"OSRM network error: {exc}") from exc
        if response.status_code in (429, 502, 503, 504):
            raise ProviderTransientError(
                f"Transient OSRM response: HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code >= 400:
            raise ProviderResponseError(f"OSRM returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError("OSRM returned invalid JSON") from exc
        if payload.get("code") == "NoRoute":
            raise OSRMNoRoute("OSRM could not find a connected driving route")
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise ProviderResponseError(f"OSRM returned an invalid route response: {payload.get('code', 'missing code')}")
        route = payload["routes"][0]
        geometry = route.get("geometry", {}).get("coordinates")
        legs = route.get("legs")
        if not isinstance(geometry, list) or len(geometry) < 2 or not isinstance(legs, list):
            raise ProviderResponseError("OSRM route is missing geometry or legs")
        if len(legs) != len(waypoints) - 1:
            raise ProviderResponseError("OSRM route leg count does not match requested waypoints")
        try:
            geometry = [[float(point[0]), float(point[1])] for point in geometry]
        except (TypeError, ValueError, IndexError) as exc:
            raise ProviderResponseError("OSRM route contains invalid geometry coordinates") from exc
        segment_distances = []
        leg_distances = []
        for leg in legs:
            distances = leg.get("annotation", {}).get("distance")
            if not isinstance(distances, list) or not distances:
                raise ProviderResponseError("OSRM route is missing road-distance annotations")
            try:
                parsed_distances = [float(distance) for distance in distances]
                segment_distances.extend(parsed_distances)
                leg_distances.append(float(leg.get("distance", sum(parsed_distances))))
            except (TypeError, ValueError) as exc:
                raise ProviderResponseError("OSRM route contains invalid road distances") from exc
        if len(segment_distances) != len(geometry) - 1:
            raise ProviderResponseError(
                "OSRM annotation distances do not correspond to route geometry coordinates"
            )
        try:
            distance = float(route["distance"])
            duration = float(route["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError("OSRM route has invalid distance or duration") from exc
        return OSRMRoute(
            geometry=geometry,
            segment_distances_meters=segment_distances,
            leg_distances_meters=leg_distances,
            distance_meters=distance,
            duration_seconds=duration,
        )

    def close(self):
        self._client.close()


@lru_cache(maxsize=4)
def get_osrm_client(base_url):
    return OSRMClient(base_url=base_url)
