from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase

from integrations.exceptions import ProviderTransientError
from integrations.geocode_maps import GeocodeMapsClient, GeocodeResponse
from stations.models import GeocodingAttempt, GeocodingQueryCache, ProviderState, Station
from stations.services.geocoding import (
    AttemptBudget,
    Lease,
    ProviderAttemptBudgetExceeded,
    classify_candidates,
    diagnostic_primary_query,
    geocode_station,
    reconcile_allowance,
    request_with_retries,
    reevaluate_cached_station,
    station_identity_fingerprint,
)


def make_station(**overrides):
    values = {
        "source_truckstop_id": "7", "name": "WOODED FUEL #12", "address": "10 Main St",
        "city": "Tulsa", "state": "OK", "rack_id": "307", "retail_price": Decimal("3.00733333"),
    }
    values.update(overrides)
    return Station.objects.create(**values)


def candidate(**overrides):
    value = {
        "place_id": 100, "lat": "36.1000000", "lon": "-95.9000000", "type": "fuel",
        "name": "WOODED FUEL #12",
        "address": {"country_code": "us", "state": "Oklahoma", "city": "Tulsa", "road": "Main St", "house_number": "10"},
    }
    value.update(overrides)
    return value


class GeocodeMapsClientTests(TestCase):
    def test_api_key_is_sent_as_query_parameter_not_bearer_header(self):
        def handler(request):
            self.assertEqual(request.url.params["api_key"], "secret-key")
            self.assertEqual(request.url.params["addressdetails"], "1")
            self.assertEqual(request.url.params["countrycodes"], "us")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json=[])

        with GeocodeMapsClient(
            api_key="secret-key",
            base_url="https://geocode.maps.co",
            transport=httpx.MockTransport(handler),
        ) as client:
            client.search_once("Tulsa, OK, USA")


class CandidateMatchingTests(TestCase):
    def test_unique_exact_candidate_matches(self):
        decision = classify_candidates(make_station(), [candidate()])
        self.assertEqual(decision.status, "matched")

    def test_duplicate_provider_object_is_not_ambiguous(self):
        decision = classify_candidates(make_station(), [candidate(), candidate()])
        self.assertEqual(decision.status, "matched")

    def test_multiple_distinct_matches_are_ambiguous(self):
        decision = classify_candidates(make_station(), [candidate(), candidate(place_id=101)])
        self.assertEqual(decision.status, "ambiguous")

    def test_missing_station_number_is_plausible_not_contradictory(self):
        decision = classify_candidates(make_station(), [candidate(name="WOODED FUEL")])
        self.assertEqual(decision.status, "ambiguous")
        self.assertIn("source station number is not confirmed", decision.candidate_decisions[0]["reasons"])

    def test_explicitly_different_station_number_is_contradictory(self):
        decision = classify_candidates(make_station(), [candidate(name="WOODED FUEL #99")])
        self.assertEqual(decision.status, "unresolved")
        self.assertTrue(any(reason.startswith("station number contradicts source") for reason in decision.candidate_decisions[0]["reasons"]))

    def test_namedetails_ref_confirms_number_with_leading_zero_normalization(self):
        result = candidate(name="WOODED FUEL", namedetails={"ref": "0012"})
        decision = classify_candidates(make_station(), [result])
        self.assertEqual(decision.status, "matched")
        self.assertIn("station number confirmed by namedetails.ref", decision.candidate_decisions[0]["reasons"])

    def test_namedetails_ref_does_not_accept_partial_text(self):
        result = candidate(name="WOODED FUEL", namedetails={"ref": "store 12"})
        decision = classify_candidates(make_station(), [result])
        self.assertEqual(decision.status, "ambiguous")
        self.assertIn("source station number is not confirmed", decision.candidate_decisions[0]["reasons"])

    def test_number_confirmation_does_not_establish_fuel_classification(self):
        result = candidate(
            name="WOODED FUEL", type="services", **{"class": "highway"},
            namedetails={"ref": "12"},
        )
        decision = classify_candidates(make_station(), [result])
        self.assertEqual(decision.status, "ambiguous")
        self.assertIn("fuel-station classification missing", decision.candidate_decisions[0]["reasons"])

    def test_different_official_store_url_number_blocks_confirmation(self):
        result = candidate(
            name="WOODED FUEL", namedetails={"ref": "12"},
            extratags={"website": "https://example.test/locations/99"},
        )
        decision = classify_candidates(make_station(), [result])
        self.assertEqual(decision.status, "unresolved")
        self.assertTrue(any("official store URL" in reason for reason in decision.candidate_decisions[0]["reasons"]))

    def test_credible_brand_and_locality_without_fuel_class_is_ambiguous(self):
        result = candidate(name="WOODED FUEL", type="convenience", **{"class": "shop"})
        decision = classify_candidates(make_station(), [result])
        self.assertEqual(decision.status, "ambiguous")
        self.assertIn("fuel-station classification missing", decision.candidate_decisions[0]["reasons"])

    def test_diagnostic_query_removes_number_but_preserves_full_state(self):
        station = make_station()
        self.assertEqual(diagnostic_primary_query(station), "Wooded Fuel, Tulsa, Oklahoma, USA")

    def test_city_only_and_empty_results_are_unresolved(self):
        station = make_station()
        city = candidate(type="city", name="Tulsa")
        self.assertEqual(classify_candidates(station, [city]).status, "unresolved")
        self.assertEqual(classify_candidates(station, []).status, "unresolved")


class GeocodingWorkflowTests(TestCase):
    def setUp(self):
        self.station = make_station()
        self.lease = Lease()
        self.lease.acquire()
        self.addCleanup(self.lease.release)
        reconcile_allowance(10)

    @patch.object(Lease, "wait", autospec=True)
    def test_success_persists_coordinates_and_cache(self, wait):
        client = Mock()
        client.search_once.return_value = GeocodeResponse([candidate()], 200)
        decision, calls = geocode_station(
            station=self.station, client=client, lease=self.lease,
            configured_rate=4, initial_remaining=10,
        )
        self.station.refresh_from_db()
        self.assertEqual((decision.status, calls), ("matched", 1))
        self.assertEqual(self.station.latitude, Decimal("36.1000000"))
        self.assertEqual(GeocodingAttempt.objects.count(), 1)
        self.assertEqual(self.station.geocoding_decision_summary["status"], "matched")
        attempt = GeocodingAttempt.objects.get()
        self.assertEqual(attempt.candidate_decisions[0]["disposition"], "matched")

    @patch.object(Lease, "wait", autospec=True)
    def test_cached_query_avoids_second_provider_call(self, wait):
        client = Mock()
        client.search_once.return_value = GeocodeResponse([candidate()], 200)
        kwargs = dict(station=self.station, query="WOODED FUEL #12, Tulsa, OK, USA", client=client, lease=self.lease, configured_rate=4, initial_remaining=10)
        request_with_retries(**kwargs)
        _, calls = request_with_retries(**kwargs)
        self.assertEqual(calls, 0)
        self.assertEqual(client.search_once.call_count, 1)

    def test_cached_reevaluation_persists_candidate_and_station_decisions(self):
        identity = station_identity_fingerprint(self.station)
        primary_query = "wooded fuel 12 tulsa ok usa"
        fallback_query = "10 main st tulsa ok usa"
        plausible = candidate(name="WOODED FUEL")
        GeocodingQueryCache.objects.create(
            station=self.station, provider="geocode.maps.co",
            identity_fingerprint=identity, normalized_query=primary_query,
            response=[plausible],
        )
        GeocodingQueryCache.objects.create(
            station=self.station, provider="geocode.maps.co",
            identity_fingerprint=identity, normalized_query=fallback_query,
            response=[],
        )
        first = GeocodingAttempt.objects.create(
            station=self.station, provider="geocode.maps.co",
            normalized_query=primary_query, outcome="response",
            candidate_details=[plausible],
        )
        second = GeocodingAttempt.objects.create(
            station=self.station, provider="geocode.maps.co",
            normalized_query=fallback_query, outcome="response",
            candidate_details=[],
        )
        decision = reevaluate_cached_station(self.station)
        self.station.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(self.station.geocoding_decision_summary["status"], "ambiguous")
        self.assertEqual(first.candidate_decisions[0]["disposition"], "plausible")
        self.assertEqual(second.decision_reasons, ["provider returned no candidates"])

    def test_attempt_budget_is_hard_capped(self):
        budget = AttemptBudget(2)
        budget.consume()
        budget.consume()
        with self.assertRaises(ProviderAttemptBudgetExceeded):
            budget.consume()

    @patch.object(Lease, "wait", autospec=True)
    def test_multiple_matches_persist_ambiguous_without_coordinates(self, wait):
        client = Mock()
        client.search_once.side_effect = [
            GeocodeResponse([candidate(), candidate(place_id=101)], 200),
            GeocodeResponse([], 200),
        ]
        decision, calls = geocode_station(
            station=self.station, client=client, lease=self.lease,
            configured_rate=4, initial_remaining=10,
        )
        self.station.refresh_from_db()
        self.assertEqual((decision.status, calls), ("ambiguous", 2))
        self.assertIsNone(self.station.latitude)

    @patch.object(Lease, "wait", autospec=True)
    def test_empty_primary_and_fallback_persist_unresolved(self, wait):
        client = Mock()
        client.search_once.return_value = GeocodeResponse([], 200)
        decision, calls = geocode_station(
            station=self.station, client=client, lease=self.lease,
            configured_rate=4, initial_remaining=10,
        )
        self.station.refresh_from_db()
        self.assertEqual((decision.status, calls), ("unresolved", 2))
        self.assertIsNone(self.station.longitude)

    @patch.object(Lease, "wait", autospec=True)
    @patch("stations.services.geocoding.random.uniform", return_value=0)
    def test_transient_failures_retry_and_count_allowance(self, jitter, wait):
        client = Mock()
        client.search_once.side_effect = [
            ProviderTransientError("timeout"), ProviderTransientError("timeout"),
            GeocodeResponse([], 200),
        ]
        _, calls = request_with_retries(
            station=self.station, query="query", client=client, lease=self.lease,
            configured_rate=4, initial_remaining=10,
        )
        self.assertEqual(calls, 3)
        self.assertEqual(ProviderState.objects.get().remaining_initial_allowance, 7)

    @patch.object(Lease, "wait", autospec=True)
    @patch("stations.services.geocoding.random.uniform", return_value=0)
    def test_transient_failure_stops_after_three_attempts(self, jitter, wait):
        client = Mock()
        client.search_once.side_effect = ProviderTransientError("timeout")
        with self.assertRaises(ProviderTransientError):
            request_with_retries(
                station=self.station, query="query", client=client, lease=self.lease,
                configured_rate=4, initial_remaining=10,
            )
        self.assertEqual(client.search_once.call_count, 3)
        self.assertEqual(GeocodingAttempt.objects.count(), 3)
