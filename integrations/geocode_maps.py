from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

import httpx

from .exceptions import ProviderAccessError, ProviderResponseError, ProviderTransientError


@dataclass(frozen=True)
class GeocodeResponse:
    candidates: list[dict]
    status_code: int


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class GeocodeMapsClient:
    provider_name = "geocode.maps.co"

    def __init__(self, *, api_key: str, base_url: str, transport=None):
        if not api_key:
            raise ValueError("GEOCODE_MAPS_API_KEY is required")
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"User-Agent": "optimal-refuel-routes/1.0"},
            timeout=httpx.Timeout(20.0, connect=5.0),
            transport=transport,
        )

    def close(self):
        self._client.close()

    def search_once(self, query: str) -> GeocodeResponse:
        try:
            response = self._client.get(
                "/search",
                params={
                    "q": query,
                    "api_key": self._api_key,
                    "format": "json",
                    "addressdetails": 1,
                    "countrycodes": "us",
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # httpx exception text can include the full request URL. Keep the
            # API key out of persisted attempt errors and application logs.
            raise ProviderTransientError(
                f"Geocoding network error ({type(exc).__name__})"
            ) from exc
        if response.status_code in (401, 403):
            raise ProviderAccessError(f"Geocode Maps rejected access with HTTP {response.status_code}")
        if response.status_code in (429, 502, 503, 504):
            raise ProviderTransientError(
                f"Transient Geocode Maps response: HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code >= 400:
            raise ProviderResponseError(f"Geocode Maps returned non-retryable HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError("Geocode Maps returned invalid JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ProviderResponseError("Geocode Maps response must be a JSON list of objects")
        return GeocodeResponse(candidates=payload, status_code=response.status_code)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@lru_cache(maxsize=4)
def get_geocode_maps_client(api_key: str, base_url: str):
    """Return one reusable synchronous client per provider configuration."""
    return GeocodeMapsClient(api_key=api_key, base_url=base_url)
