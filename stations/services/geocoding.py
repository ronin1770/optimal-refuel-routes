from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.db import connection, transaction
from django.utils import timezone

from integrations.exceptions import (
    ProviderAccessError,
    ProviderResponseError,
    ProviderSuspendedError,
    ProviderTransientError,
)
from stations.models import (
    GeocodingAttempt,
    GeocodingQueryCache,
    JobLease,
    ProviderState,
    Station,
)


PROVIDER = "geocode.maps.co"
JOB_NAME = "station-geocoding"
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


class JobLeaseError(RuntimeError):
    pass


class ProviderAttemptBudgetExceeded(RuntimeError):
    pass


class AttemptBudget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.consumed = 0

    def consume(self):
        if self.consumed >= self.maximum:
            raise ProviderAttemptBudgetExceeded(
                f"diagnostic provider-attempt budget of {self.maximum} is exhausted"
            )
        self.consumed += 1


@dataclass(frozen=True)
class MatchDecision:
    status: str
    candidate: dict | None
    reasons: list[str]
    candidate_decisions: list[dict]


def normalize_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", str(value or "").casefold())).strip()


def normalize_state(value):
    normalized = normalize_text(value)
    return US_STATES.get(normalized, normalized.upper() if len(normalized) == 2 else "")


STATE_NAMES = {code: name.title() for name, code in US_STATES.items()}
STATION_NUMBER_SUFFIX = re.compile(r"\s*(?:#|\bno\.?\s*)\s*([A-Za-z0-9-]+)\s*$", re.IGNORECASE)


def station_name_parts(value):
    text = str(value or "").strip()
    match = STATION_NUMBER_SUFFIX.search(text)
    if not match:
        return normalize_text(text), None
    return normalize_text(text[: match.start()]), normalize_text(match.group(1))


def normalize_station_identifier(value):
    identifier = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", identifier):
        return None
    if identifier.isdigit():
        return identifier.lstrip("0") or "0"
    return identifier.casefold()


def _candidate_station_number_evidence(candidate, candidate_name_number):
    evidence = []
    name_identifier = normalize_station_identifier(candidate_name_number)
    if name_identifier:
        evidence.append(("candidate name", name_identifier))

    namedetails = candidate.get("namedetails")
    if isinstance(namedetails, dict):
        reference = normalize_station_identifier(namedetails.get("ref"))
        if reference:
            evidence.append(("namedetails.ref", reference))

    extratags = candidate.get("extratags")
    if isinstance(extratags, dict) and extratags.get("website"):
        try:
            segments = [segment for segment in urlparse(str(extratags["website"])).path.split("/") if segment]
            final_segment = segments[-1] if segments else ""
        except ValueError:
            final_segment = ""
        if final_segment.isdigit():
            evidence.append(("official store URL", normalize_station_identifier(final_segment)))
    return evidence


def diagnostic_primary_query(station):
    brand, _number = station_name_parts(station.name)
    display_brand = " ".join(word.capitalize() for word in brand.split())
    state = STATE_NAMES.get(normalize_state(station.state), station.state)
    return f"{display_brand}, {station.city}, {state}, USA"


def normalized_query(value):
    return normalize_text(value)


def station_identity_fingerprint(station):
    values = [station.name, station.address, station.city, station.state]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _candidate_id(candidate):
    if candidate.get("osm_id") is not None:
        return f"osm:{candidate.get('osm_type', '')}:{candidate['osm_id']}"
    if candidate.get("place_id") is not None:
        return f"place:{candidate['place_id']}"
    return "fallback:" + hashlib.sha256(
        json.dumps(
            [candidate.get("lat"), candidate.get("lon"), candidate.get("display_name")],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def deduplicate_candidates(candidates):
    unique = {}
    for candidate in candidates:
        unique.setdefault(_candidate_id(candidate), candidate)
    return list(unique.values())


def _candidate_name(candidate, address):
    for value in (candidate.get("name"), address.get("amenity"), address.get("shop")):
        if value:
            return value
    return ""


def _is_usa(address):
    code = normalize_text(address.get("country_code"))
    country = normalize_text(address.get("country"))
    return code == "us" or country in {"united states", "united states of america", "usa"}


def _is_fuel_station(candidate, address):
    kind = normalize_text(candidate.get("type"))
    category = normalize_text(candidate.get("class") or candidate.get("category"))
    name = normalize_text(_candidate_name(candidate, address))
    return kind in {"fuel", "gas station", "petrol station"} or (
        bool(name) and ("truckstop" in name.replace(" ", "") or "truck stop" in name)
    ) or (category == "amenity" and kind == "fuel")


def _locality(address):
    return next((address.get(key) for key in ("city", "town", "village", "municipality") if address.get(key)), "")


def _street(address):
    return next((address.get(key) for key in ("road", "street", "pedestrian") if address.get(key)), "")


def _source_street_and_number(source_address):
    match = re.match(r"^\s*(\d+[A-Za-z-]*)\s+(.+?)\s*$", source_address or "")
    return (match.group(2), match.group(1)) if match else ("", "")


def classify_candidates(station, candidates):
    candidates = deduplicate_candidates(candidates)
    matches = []
    plausible = []
    contradictory = 0
    source_name = normalize_text(station.name)
    source_brand, source_station_number = station_name_parts(station.name)
    source_station_number = normalize_station_identifier(source_station_number)
    source_city = normalize_text(station.city)
    source_state = normalize_state(station.state)
    source_street, source_house_number = _source_street_and_number(station.address)

    candidate_decisions = []
    for candidate in candidates:
        address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
        country_present = bool(address.get("country_code") or address.get("country"))
        state_present = bool(address.get("state"))
        locality_present = bool(_locality(address))
        country_ok = _is_usa(address)
        state_ok = normalize_state(address.get("state")) == source_state
        city_ok = normalize_text(_locality(address)) == source_city
        fuel_ok = _is_fuel_station(candidate, address)
        name = normalize_text(_candidate_name(candidate, address))
        candidate_brand, candidate_number = station_name_parts(_candidate_name(candidate, address))
        brand_ok = bool(candidate_brand) and candidate_brand == source_brand
        number_evidence = _candidate_station_number_evidence(candidate, candidate_number)
        matching_number_sources = [source for source, value in number_evidence if value == source_station_number]
        conflicting_number_sources = [source for source, value in number_evidence if source_station_number and value != source_station_number]
        number_confirmed = bool(source_station_number and matching_number_sources)
        number_missing = bool(source_station_number) and brand_ok and not number_evidence
        number_contradiction = bool(conflicting_number_sources)
        name_ok = bool(name) and brand_ok and (
            number_confirmed if source_station_number else name == source_name
        ) and not number_contradiction
        street_ok = bool(source_street) and normalize_text(_street(address)) == normalize_text(source_street)
        number_ok = bool(source_house_number) and normalize_text(address.get("house_number")) == normalize_text(source_house_number)
        returned_name_does_not_conflict = not name or (
            brand_ok and not number_contradiction
        )
        identity_match = country_ok and state_ok and city_ok and fuel_ok and name_ok
        address_match = (
            country_ok
            and state_ok
            and city_ok
            and fuel_ok
            and street_ok
            and number_ok
            and returned_name_does_not_conflict
            and not number_missing
        )
        reasons = []
        if not country_present:
            reasons.append("country missing")
        elif not country_ok:
            reasons.append("country contradicts source")
        if not state_present:
            reasons.append("state missing")
        elif not state_ok:
            reasons.append("state contradicts source")
        if not locality_present:
            reasons.append("locality missing")
        elif not city_ok:
            reasons.append("locality contradicts source")
        if not name:
            reasons.append("station name missing")
        elif not brand_ok:
            reasons.append("station brand contradicts source")
        elif number_contradiction:
            reasons.append(
                "station number contradicts source in "
                + ", ".join(conflicting_number_sources)
            )
        elif number_confirmed:
            reasons.append(
                "station number confirmed by " + ", ".join(matching_number_sources)
            )
        elif number_missing:
            reasons.append("source station number is not confirmed")
        if not fuel_ok:
            reasons.append("fuel-station classification missing")
        if identity_match or address_match:
            try:
                latitude = Decimal(str(candidate["lat"]))
                longitude = Decimal(str(candidate["lon"]))
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise InvalidOperation
            except (KeyError, InvalidOperation, ValueError):
                plausible.append(candidate)
                reasons.append("coordinates missing or invalid")
                candidate_decisions.append({"provider_object_id": _candidate_id(candidate), "disposition": "plausible", "reasons": reasons})
                continue
            matches.append(candidate)
            candidate_decisions.append({
                "provider_object_id": _candidate_id(candidate),
                "disposition": "matched",
                "reasons": ["all exact matching requirements satisfied"]
                + [reason for reason in reasons if "confirmed by" in reason],
            })
        elif not (
            (country_present and not country_ok)
            or (state_present and not state_ok)
            or (locality_present and not city_ok)
            or number_contradiction
            or (bool(name) and not brand_ok)
        ) and (fuel_ok or (brand_ok and country_ok and state_ok and city_ok)):
            plausible.append(candidate)
            candidate_decisions.append({"provider_object_id": _candidate_id(candidate), "disposition": "plausible", "reasons": reasons})
        else:
            contradictory += 1
            candidate_decisions.append({"provider_object_id": _candidate_id(candidate), "disposition": "rejected", "reasons": reasons or ["candidate did not provide confirming station evidence"]})

    if len(matches) == 1 and not plausible:
        return MatchDecision(Station.GeocodingStatus.MATCHED, matches[0], ["one candidate satisfied every exact matching rule"], candidate_decisions)
    if len(matches) > 1:
        return MatchDecision(Station.GeocodingStatus.AMBIGUOUS, None, ["multiple distinct candidates satisfied the matching rules"], candidate_decisions)
    if plausible or (matches and plausible):
        return MatchDecision(Station.GeocodingStatus.AMBIGUOUS, None, ["a plausible station exists but identity, station number, classification, or locality could not be confirmed"], candidate_decisions)
    if not candidates:
        return MatchDecision(Station.GeocodingStatus.UNRESOLVED, None, ["the provider returned no candidates"], [])
    return MatchDecision(Station.GeocodingStatus.UNRESOLVED, None, [f"all {contradictory} candidates had contradictory identities or were not credible stations"], candidate_decisions)


class Lease:
    def __init__(self, *, duration_seconds=120, renewal_interval=30):
        self.duration = timedelta(seconds=duration_seconds)
        self.renewal_interval = renewal_interval
        self.token = uuid.uuid4().hex
        self.last_renewed = None

    def acquire(self):
        now = timezone.now()
        with transaction.atomic():
            lease, created = JobLease.objects.select_for_update().get_or_create(
                name=JOB_NAME,
                defaults={"owner_token": self.token, "expires_at": now + self.duration},
            )
            if not created:
                if lease.expires_at > now:
                    raise JobLeaseError("another station-geocoding job owns the active lease")
                lease.owner_token = self.token
                lease.expires_at = now + self.duration
                lease.save(update_fields=["owner_token", "expires_at", "updated_at"])
        self.last_renewed = now

    def confirm(self):
        now = timezone.now()
        if self.last_renewed and (now - self.last_renewed).total_seconds() >= self.renewal_interval:
            self.renew()
            return
        if not JobLease.objects.filter(name=JOB_NAME, owner_token=self.token, expires_at__gt=now).exists():
            raise JobLeaseError("station-geocoding lease ownership was lost or expired")

    def renew(self):
        now = timezone.now()
        updated = JobLease.objects.filter(name=JOB_NAME, owner_token=self.token, expires_at__gt=now).update(expires_at=now + self.duration)
        if updated != 1:
            raise JobLeaseError("station-geocoding lease could not be renewed")
        self.last_renewed = now

    def release(self):
        JobLease.objects.filter(name=JOB_NAME, owner_token=self.token).delete()

    def wait(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            elapsed = (timezone.now() - self.last_renewed).total_seconds()
            until_renewal = max(0.01, self.renewal_interval - elapsed)
            time.sleep(min(remaining, until_renewal))
            if (timezone.now() - self.last_renewed).total_seconds() >= self.renewal_interval:
                self.renew()
        self.confirm()


def force_or_release_lease(*, force=False):
    now = timezone.now()
    with transaction.atomic():
        lease = JobLease.objects.select_for_update().filter(name=JOB_NAME).first()
        if not lease:
            return False
        if lease.expires_at > now and not force:
            raise JobLeaseError("the geocoding lease is active; use --force-release to override")
        lease.delete()
        return True


def initialize_provider_state(initial_remaining):
    state, _ = ProviderState.objects.get_or_create(
        provider=PROVIDER,
        defaults={"remaining_initial_allowance": max(0, initial_remaining)},
    )
    return state


def reconcile_allowance(value):
    state, _ = ProviderState.objects.update_or_create(
        provider=PROVIDER,
        defaults={"remaining_initial_allowance": max(0, value)},
    )
    return state


def reserve_attempt(*, configured_rate, initial_remaining):
    now = timezone.now()
    with transaction.atomic():
        # Write first: SQLite has no SELECT FOR UPDATE, so this serializes all
        # workers before any process reads/decrements the shared allowance.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO stations_providerstate
                  (provider, remaining_initial_allowance, next_request_at, not_before, updated_at)
                VALUES (%s, %s, NULL, NULL, %s)
                """,
                [PROVIDER, max(0, initial_remaining), now],
            )
            cursor.execute(
                "UPDATE stations_providerstate SET updated_at = updated_at WHERE provider = %s",
                [PROVIDER],
            )
        state = ProviderState.objects.get(provider=PROVIDER)
        if state.not_before and state.not_before > now:
            raise ProviderSuspendedError(f"provider is suspended until {state.not_before.isoformat()}")
        using_initial_rate = state.remaining_initial_allowance > 0
        slot = max(now, state.next_request_at or now)
        if using_initial_rate:
            state.remaining_initial_allowance -= 1
        next_rate = (
            min(max(float(configured_rate), 0.01), 5.0)
            if state.remaining_initial_allowance > 0
            else 1.0
        )
        state.next_request_at = slot + timedelta(seconds=1.0 / next_rate)
        state.save(update_fields=["remaining_initial_allowance", "next_request_at", "updated_at"])
    return max(0.0, (slot - now).total_seconds())


def suspend_provider(seconds):
    until = timezone.now() + timedelta(seconds=seconds)
    with transaction.atomic():
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO stations_providerstate
                  (provider, remaining_initial_allowance, next_request_at, not_before, updated_at)
                VALUES (%s, 0, NULL, NULL, %s)
                """,
                [PROVIDER, now],
            )
            cursor.execute(
                "UPDATE stations_providerstate SET updated_at = updated_at WHERE provider = %s",
                [PROVIDER],
            )
        state = ProviderState.objects.get(provider=PROVIDER)
        if not state.not_before or state.not_before < until:
            state.not_before = until
            state.save(update_fields=["not_before", "updated_at"])
    return until


def request_with_retries(*, station, query, client, lease, configured_rate, initial_remaining, attempt_budget=None):
    normalized = normalized_query(query)
    identity = station_identity_fingerprint(station)
    cached = GeocodingQueryCache.objects.filter(
        station=station, provider=PROVIDER, identity_fingerprint=identity, normalized_query=normalized
    ).first()
    if cached:
        return cached.response, 0

    calls = 0
    for attempt_number in range(1, 4):
        lease.confirm()
        if attempt_budget:
            attempt_budget.consume()
        lease.wait(reserve_attempt(configured_rate=configured_rate, initial_remaining=initial_remaining))
        calls += 1
        try:
            response = client.search_once(query)
        except ProviderAccessError as exc:
            GeocodingAttempt.objects.create(station=station, provider=PROVIDER, normalized_query=normalized, outcome=GeocodingAttempt.Outcome.ACCESS_ERROR, error_message=str(exc))
            raise
        except ProviderResponseError as exc:
            GeocodingAttempt.objects.create(station=station, provider=PROVIDER, normalized_query=normalized, outcome=GeocodingAttempt.Outcome.MALFORMED, error_message=str(exc))
            raise
        except ProviderTransientError as exc:
            outcome = GeocodingAttempt.Outcome.RATE_LIMITED if exc.status_code == 429 else GeocodingAttempt.Outcome.TRANSIENT_ERROR
            GeocodingAttempt.objects.create(station=station, provider=PROVIDER, normalized_query=normalized, outcome=outcome, http_status=exc.status_code, error_message=str(exc))
            if exc.retry_after is not None and exc.retry_after > 120:
                until = suspend_provider(exc.retry_after)
                raise ProviderSuspendedError(f"provider Retry-After persisted until {until.isoformat()}") from exc
            if attempt_number == 3:
                raise
            backoff = min(30, 2 * (2 ** (attempt_number - 1))) + random.uniform(0, 1)
            delay = max(backoff, exc.retry_after or 0)
            lease.wait(delay)
            continue
        GeocodingAttempt.objects.create(
            station=station,
            provider=PROVIDER,
            normalized_query=normalized,
            outcome=GeocodingAttempt.Outcome.RESPONSE,
            http_status=response.status_code,
            candidate_details=response.candidates,
        )
        GeocodingQueryCache.objects.update_or_create(
            station=station,
            provider=PROVIDER,
            identity_fingerprint=identity,
            normalized_query=normalized,
            defaults={"response": response.candidates},
        )
        return response.candidates, calls
    raise AssertionError("retry loop exited unexpectedly")


def _persist_decision(station, decision, evaluated_queries):
    summary = {
        "status": decision.status,
        "reasons": decision.reasons,
        "candidate_count": len(decision.candidate_decisions),
        "matched_candidate_id": _candidate_id(decision.candidate) if decision.candidate else None,
        "evaluated_queries": [query for query, _candidates in evaluated_queries],
        "evaluated_at": timezone.now().isoformat(),
    }
    update = {
        "geocoding_status": decision.status,
        "latitude": None,
        "longitude": None,
        "coordinate_source": "",
        "geocoded_at": timezone.now(),
        "updated_at": timezone.now(),
        "geocoding_decision_summary": summary,
    }
    if decision.status == Station.GeocodingStatus.MATCHED:
        update.update(
            latitude=Decimal(str(decision.candidate["lat"])),
            longitude=Decimal(str(decision.candidate["lon"])),
            coordinate_source=PROVIDER,
        )
    Station.objects.filter(pk=station.pk).update(**update)

    decisions_by_id = {
        item["provider_object_id"]: item for item in decision.candidate_decisions
    }
    for query, candidates in evaluated_queries:
        candidate_ids = {_candidate_id(candidate) for candidate in candidates}
        query_decisions = [
            decisions_by_id[candidate_id]
            for candidate_id in candidate_ids
            if candidate_id in decisions_by_id
        ]
        query_reasons = (
            ["provider returned no candidates"]
            if not candidates
            else ["candidate-level decisions recorded for this response"]
        )
        attempt = station.geocoding_attempts.filter(
            provider=PROVIDER, normalized_query=query
        ).order_by("-attempted_at").first()
        if attempt:
            attempt.candidate_decisions = query_decisions
            attempt.decision_reasons = query_reasons
            attempt.save(update_fields=["candidate_decisions", "decision_reasons"])


def reevaluate_cached_station(station):
    identity = station_identity_fingerprint(station)
    entries = list(
        station.geocoding_cache_entries.filter(
            provider=PROVIDER, identity_fingerprint=identity
        ).order_by("id")
    )
    if not entries:
        return None
    evaluated_queries = [
        (entry.normalized_query, entry.response) for entry in entries
    ]
    candidates = deduplicate_candidates(
        [candidate for entry in entries for candidate in entry.response]
    )
    decision = classify_candidates(station, candidates)
    _persist_decision(station, decision, evaluated_queries)
    return decision


def geocode_station(*, station, client, lease, configured_rate, initial_remaining, primary_query=None, primary_only=False, attempt_budget=None):
    primary = primary_query or f"{station.name}, {station.city}, {station.state}, USA"
    all_candidates, calls = request_with_retries(
        station=station, query=primary, client=client, lease=lease,
        configured_rate=configured_rate, initial_remaining=initial_remaining,
        attempt_budget=attempt_budget,
    )
    evaluated_queries = [(normalized_query(primary), all_candidates)]
    decision = classify_candidates(station, all_candidates)
    if not primary_only and decision.status != Station.GeocodingStatus.MATCHED and station.address:
        fallback = f"{station.address}, {station.city}, {station.state}, USA"
        if normalized_query(fallback) != normalized_query(primary):
            fallback_candidates, fallback_calls = request_with_retries(
                station=station, query=fallback, client=client, lease=lease,
                configured_rate=configured_rate, initial_remaining=initial_remaining,
                attempt_budget=attempt_budget,
            )
            calls += fallback_calls
            evaluated_queries.append((normalized_query(fallback), fallback_candidates))
            all_candidates = deduplicate_candidates(all_candidates + fallback_candidates)
            decision = classify_candidates(station, all_candidates)
    _persist_decision(station, decision, evaluated_queries)
    return decision, calls
