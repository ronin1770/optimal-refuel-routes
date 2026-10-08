from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from integrations.exceptions import (
    ProviderAccessError,
    ProviderResponseError,
    ProviderTransientError,
)
from integrations.geocode_maps import get_geocode_maps_client
from routing.models import AddressGeocodeCache, AddressGeocodingAttempt
from routing.services.usa_boundaries import is_in_usa
from stations.services.geocoding import (
    _is_usa,
    _locality,
    normalize_state,
    normalize_text,
    reserve_attempt,
    suspend_provider,
)


class EndpointValidationError(ValueError):
    def __init__(self, message, *, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


class EndpointProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedEndpoint:
    latitude: float
    longitude: float
    label: str
    source: str


@dataclass(frozen=True)
class EndpointResolution:
    endpoint: ResolvedEndpoint
    geocoding_calls: int
    cache_hit: bool


def _parse_free_text_constraints(address):
    parts = [part.strip() for part in str(address).split(",") if part.strip()]
    if parts and normalize_text(parts[-1]) in {"usa", "us", "united states", "united states of america"}:
        parts.pop()
    if len(parts) < 2:
        return "", ""
    state = normalize_state(parts[-1])
    if not state:
        return "", ""
    city = parts[-2] if len(parts) == 2 else parts[-2]
    return normalize_text(city), state


def _cache_key(address, city, state):
    normalized = {
        "address": normalize_text(address),
        "city": normalize_text(city),
        "state": normalize_state(state),
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), normalized


def _candidate_id(candidate):
    if candidate.get("osm_id") is not None:
        return f"osm:{candidate.get('osm_type', '')}:{candidate['osm_id']}"
    if candidate.get("place_id") is not None:
        return f"place:{candidate['place_id']}"
    return "coordinates:" + normalize_text(
        f"{candidate.get('lat')}:{candidate.get('lon')}:{candidate.get('display_name')}"
    )


def _safe_candidate(candidate):
    address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
    return {
        "display_name": str(candidate.get("display_name") or "")[:300],
        "locality": str(_locality(address) or "")[:128],
        "state": str(address.get("state") or "")[:128],
        "country": str(address.get("country") or "")[:128],
    }


def _valid_candidates(candidates, *, city, state):
    valid = {}
    rejections = []
    for candidate in candidates:
        address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
        reasons = []
        try:
            latitude = Decimal(str(candidate["lat"]))
            longitude = Decimal(str(candidate["lon"]))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise InvalidOperation
        except (KeyError, TypeError, ValueError, InvalidOperation):
            reasons.append("coordinates missing or invalid")
            latitude = longitude = None
        if not _is_usa(address):
            reasons.append("structured country is not USA")
        if latitude is not None and not is_in_usa(latitude, longitude):
            reasons.append("coordinates are outside the 50 states and DC")
        locality = normalize_text(_locality(address))
        candidate_state = normalize_state(address.get("state"))
        if city and (not locality or locality != city):
            reasons.append("locality is missing or conflicts with the supplied address")
        if state and (not candidate_state or candidate_state != state):
            reasons.append("state is missing or conflicts with the supplied address")
        if reasons:
            rejections.append({"candidate": _safe_candidate(candidate), "reasons": reasons})
            continue
        valid.setdefault(_candidate_id(candidate), candidate)
    return list(valid.values()), rejections


def _persist_cache(cache_key, normalized, candidates, valid, rejections):
    if len(valid) == 1:
        chosen = valid[0]
        outcome = "resolved"
        latitude, longitude = chosen["lat"], chosen["lon"]
        reason = "exactly one distinct candidate satisfied USA and locality/state constraints"
    elif valid:
        outcome, latitude, longitude = "ambiguous", None, None
        reason = "multiple distinct candidates satisfied the address constraints"
    else:
        outcome, latitude, longitude = "unresolved", None, None
        reason = "no candidate satisfied USA and locality/state constraints"
    decision = {
        "reason": reason,
        "valid_candidates": [_safe_candidate(item) for item in valid],
        "rejections": rejections,
    }
    entry, _ = AddressGeocodeCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={
            "normalized_address": normalized["address"],
            "city_constraint": normalized["city"],
            "state_constraint": normalized["state"],
            "provider_response": candidates,
            "decision": decision,
            "latitude": latitude,
            "longitude": longitude,
            "outcome": outcome,
        },
    )
    return entry


def _result_from_cache(entry, calls, cache_hit):
    if entry.outcome == "ambiguous":
        raise EndpointValidationError(
            "address is ambiguous; provide explicit city and state constraints",
            candidates=entry.decision.get("valid_candidates", []),
        )
    if entry.outcome != "resolved" or entry.latitude is None:
        raise EndpointValidationError("address could not be resolved to a valid USA location")
    return EndpointResolution(
        endpoint=ResolvedEndpoint(
            latitude=float(entry.latitude),
            longitude=float(entry.longitude),
            label=entry.normalized_address,
            source="address",
        ),
        geocoding_calls=calls,
        cache_hit=cache_hit,
    )


def _lookup_address(address, city="", state="", *, client=None):
    inferred_city, inferred_state = _parse_free_text_constraints(address)
    explicit_city = normalize_text(city)
    explicit_state = normalize_state(state)
    if state and not explicit_state:
        raise EndpointValidationError("state must be a valid US state name or abbreviation")
    if explicit_city and inferred_city and explicit_city != inferred_city:
        raise EndpointValidationError("explicit city conflicts with the city in address")
    if explicit_state and inferred_state and explicit_state != inferred_state:
        raise EndpointValidationError("explicit state conflicts with the state in address")
    city_constraint = explicit_city or inferred_city
    state_constraint = explicit_state or inferred_state
    key, normalized = _cache_key(address, city_constraint, state_constraint)
    cached = AddressGeocodeCache.objects.filter(cache_key=key).first()
    if cached:
        return _result_from_cache(cached, 0, True)

    query_parts = [str(address).strip()]
    if explicit_city and explicit_city not in normalize_text(address):
        query_parts.append(str(city).strip())
    if explicit_state and explicit_state not in normalize_text(address).upper().split():
        query_parts.append(explicit_state)
    query_parts.append("USA")
    query = ", ".join(query_parts)
    client = client or get_geocode_maps_client(
        settings.GEOCODE_MAPS_API_KEY, settings.GEOCODE_MAPS_BASE_URL
    )
    calls = 0
    for attempt in range(1, 4):
        wait = reserve_attempt(
            configured_rate=settings.GEOCODE_REQUESTS_PER_SECOND,
            initial_remaining=settings.GEOCODE_INITIAL_REQUESTS_REMAINING,
        )
        if wait:
            time.sleep(wait)
        calls += 1
        try:
            response = client.search_once(query)
        except ProviderAccessError as exc:
            AddressGeocodingAttempt.objects.create(
                cache_key=key, normalized_query=normalize_text(query),
                outcome="access_error", error_message=str(exc),
            )
            raise EndpointProviderError("address provider rejected access") from exc
        except ProviderResponseError as exc:
            AddressGeocodingAttempt.objects.create(
                cache_key=key, normalized_query=normalize_text(query),
                outcome="malformed", error_message=str(exc),
            )
            raise EndpointProviderError("address provider returned an invalid response") from exc
        except ProviderTransientError as exc:
            AddressGeocodingAttempt.objects.create(
                cache_key=key, normalized_query=normalize_text(query),
                outcome="rate_limited" if exc.status_code == 429 else "transient_error",
                http_status=exc.status_code, error_message=str(exc),
            )
            if exc.retry_after is not None and exc.retry_after > 120:
                until = suspend_provider(exc.retry_after)
                raise EndpointProviderError(
                    f"address provider is temporarily unavailable until {until.isoformat()}"
                ) from exc
            if attempt == 3:
                raise EndpointProviderError("address provider is temporarily unavailable") from exc
            time.sleep(max(exc.retry_after or 0, min(30, 2 * (2 ** (attempt - 1))) + random.uniform(0, 1)))
            continue
        AddressGeocodingAttempt.objects.create(
            cache_key=key, normalized_query=normalize_text(query), outcome="response",
            http_status=response.status_code, candidate_count=len(response.candidates),
        )
        valid, rejections = _valid_candidates(
            response.candidates, city=city_constraint, state=state_constraint
        )
        entry = _persist_cache(key, normalized, response.candidates, valid, rejections)
        return _result_from_cache(entry, calls, False)
    raise AssertionError("address retry loop exited unexpectedly")


def resolve_endpoint(value, *, client=None):
    if "address" in value:
        return _lookup_address(
            value["address"], value.get("city", ""), value.get("state", ""), client=client
        )
    latitude = float(value["latitude"])
    longitude = float(value["longitude"])
    if not is_in_usa(latitude, longitude):
        raise EndpointValidationError("coordinates must be within the 50 states or DC")
    return EndpointResolution(
        endpoint=ResolvedEndpoint(latitude, longitude, f"{latitude:.6f}, {longitude:.6f}", "coordinates"),
        geocoding_calls=0,
        cache_hit=False,
    )
