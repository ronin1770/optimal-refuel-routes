from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from integrations.exceptions import ProviderTransientError
from integrations.geocode_maps import GeocodeResponse
from integrations.osrm import OSRMRoute
from routing.models import MapResult
from routing.serializers import RouteRequestSerializer
from routing.services.endpoint_resolution import EndpointValidationError, resolve_endpoint
from routing.services.geometry import METERS_PER_MILE, corridor_stations
from routing.services.osrm_routing import RoutingCallBudget, RoutingCallBudgetExceeded, request_route
from routing.services.usa_boundaries import is_in_usa
from stations.models import ProviderState, Station, StationDatasetVersion


class RouteSerializerTests(TestCase):
    def test_accepts_mixed_endpoint_representations(self):
        serializer = RouteRequestSerializer(data={
            "start": {"address": "New York, NY"},
            "finish": {"latitude": 42.3601, "longitude": -71.0589},
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_combined_address_and_coordinates(self):
        serializer = RouteRequestSerializer(data={
            "start": {"address": "Boston, MA", "latitude": 42, "longitude": -71},
            "finish": {"latitude": 40.7, "longitude": -74},
        })
        self.assertFalse(serializer.is_valid())


class UsaBoundaryTests(TestCase):
    def test_50_states_and_dc_membership_excludes_territory(self):
        self.assertTrue(is_in_usa(40.7128, -74.0060))
        self.assertTrue(is_in_usa(61.2181, -149.9003))
        self.assertFalse(is_in_usa(18.4655, -66.1057))


class EndpointResolutionTests(TestCase):
    def setUp(self):
        ProviderState.objects.create(provider="geocode.maps.co", remaining_initial_allowance=10)

    @patch("routing.services.endpoint_resolution.time.sleep")
    @patch("routing.services.endpoint_resolution.is_in_usa", return_value=True)
    def test_address_resolution_is_cached_and_counts_only_outbound_call(self, _inside, _sleep):
        client = Mock()
        client.search_once.return_value = GeocodeResponse(candidates=[{
            "place_id": 1,
            "lat": "40.7128",
            "lon": "-74.0060",
            "display_name": "New York, New York, United States",
            "address": {"city": "New York", "state": "New York", "country": "United States", "country_code": "us"},
        }], status_code=200)
        first = resolve_endpoint({"address": "New York, NY"}, client=client)
        second = resolve_endpoint({"address": "New York, NY"}, client=client)
        self.assertEqual(first.geocoding_calls, 1)
        self.assertEqual(second.geocoding_calls, 0)
        self.assertTrue(second.cache_hit)
        client.search_once.assert_called_once()

    @patch("routing.services.endpoint_resolution.is_in_usa", return_value=True)
    def test_multiple_valid_address_candidates_are_ambiguous(self, _inside):
        client = Mock()
        client.search_once.return_value = GeocodeResponse(candidates=[
            {"place_id": 1, "lat": "42.1", "lon": "-71.1", "display_name": "One", "address": {"city": "Boston", "state": "MA", "country_code": "us"}},
            {"place_id": 2, "lat": "42.2", "lon": "-71.2", "display_name": "Two", "address": {"city": "Boston", "state": "MA", "country_code": "us"}},
        ], status_code=200)
        with self.assertRaises(EndpointValidationError) as context:
            resolve_endpoint({"address": "Boston, MA"}, client=client)
        self.assertEqual(len(context.exception.candidates), 2)

    @patch("routing.services.endpoint_resolution.is_in_usa", return_value=True)
    def test_coordinates_do_not_call_geocoder(self, _inside):
        client = Mock()
        result = resolve_endpoint({"latitude": 40.7, "longitude": -74.0}, client=client)
        self.assertEqual(result.geocoding_calls, 0)
        client.search_once.assert_not_called()


class CorridorTests(TestCase):
    def test_rtree_shortlist_is_precisely_filtered_and_progressed(self):
        near = Station.objects.create(
            source_truckstop_id="near", name="Near", address="1 Road", city="Town", state="KS",
            rack_id="1", retail_price="3.00000000", latitude="38.0100000", longitude="-99.0000000",
            geocoding_status=Station.GeocodingStatus.MATCHED,
        )
        Station.objects.create(
            source_truckstop_id="far", name="Far", address="2 Road", city="Town", state="KS",
            rack_id="2", retail_price="2.00000000", latitude="38.2000000", longitude="-99.0000000",
            geocoding_status=Station.GeocodingStatus.MATCHED,
        )
        route = OSRMRoute(
            geometry=[[-100, 38], [-98, 38]], segment_distances_meters=[100 * METERS_PER_MILE],
            leg_distances_meters=[100 * METERS_PER_MILE], distance_meters=100 * METERS_PER_MILE,
            duration_seconds=7200,
        )
        result = corridor_stations(route, 5)
        self.assertEqual([item.station_id for item in result], [near.id])
        self.assertAlmostEqual(result[0].progress_miles, 50, delta=2)


class DatasetVersionTests(TestCase):
    def test_routing_relevant_changes_increment_version(self):
        version = StationDatasetVersion.objects.get(name="stations")
        original = version.version
        station = Station.objects.create(
            source_truckstop_id="versioned", name="Versioned", address="1 Road", city="Town", state="KS",
            rack_id="1", retail_price="3.00000000",
        )
        version.refresh_from_db()
        self.assertEqual(version.version, original + 1)
        Station.objects.filter(pk=station.pk).update(retail_price="3.10000000")
        version.refresh_from_db()
        self.assertEqual(version.version, original + 2)
        Station.objects.filter(pk=station.pk).update(requires_review=True)
        version.refresh_from_db()
        self.assertEqual(version.version, original + 2)


class RoutingProviderTests(TestCase):
    @patch("routing.services.osrm_routing.time.sleep")
    @patch("routing.services.osrm_routing._reserve_slot", return_value=0)
    def test_transient_failure_retries_within_shared_budget(self, _reserve, _sleep):
        client = Mock()
        expected = OSRMRoute([[-100, 38], [-99, 38]], [1000], [1000], 1000, 60)
        client.route_once.side_effect = [ProviderTransientError("temporary"), expected]
        budget = RoutingCallBudget(3)
        result = request_route(
            [(38, -100), (38, -99)], budget=budget,
            deadline_monotonic=float("inf"),
            client=client,
        )
        self.assertEqual(result, expected)
        self.assertEqual(budget.used, 2)

    def test_budget_never_allows_fourth_call(self):
        budget = RoutingCallBudget(3)
        for _index in range(3):
            budget.consume()
        with self.assertRaises(RoutingCallBudgetExceeded):
            budget.consume()


class RouteApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.station = Station.objects.create(
            source_truckstop_id="route-stop", name="Route Stop", address="1 Road", city="Town", state="KS",
            rack_id="1", retail_price="3.00000000", latitude="38.5000000", longitude="-98.5000000",
            geocoding_status=Station.GeocodingStatus.MATCHED,
        )

    @patch("routing.services.planner.resolve_endpoint")
    @patch("routing.services.planner.request_route")
    def test_verified_route_uses_two_logical_osrm_calls(self, request_route, resolve):
        from routing.services.endpoint_resolution import EndpointResolution, ResolvedEndpoint
        resolve.side_effect = [
            EndpointResolution(ResolvedEndpoint(38, -100, "Start", "coordinates"), 0, False),
            EndpointResolution(ResolvedEndpoint(39, -97, "Finish", "coordinates"), 0, False),
        ]
        base = OSRMRoute(
            geometry=[[-100, 38], [-98.5, 38.5], [-97, 39]],
            segment_distances_meters=[300 * METERS_PER_MILE, 300 * METERS_PER_MILE],
            leg_distances_meters=[600 * METERS_PER_MILE], distance_meters=600 * METERS_PER_MILE,
            duration_seconds=36000,
        )
        verified = OSRMRoute(
            geometry=base.geometry, segment_distances_meters=base.segment_distances_meters,
            leg_distances_meters=[305 * METERS_PER_MILE, 305 * METERS_PER_MILE],
            distance_meters=610 * METERS_PER_MILE, duration_seconds=37000,
        )
        routes = iter([base, verified])
        def mocked_route(_waypoints, *, budget, **_kwargs):
            budget.consume()
            return next(routes)
        request_route.side_effect = mocked_route
        response = self.client.post(reverse("route"), {
            "start": {"latitude": 38, "longitude": -100},
            "finish": {"latitude": 39, "longitude": -97},
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["verification_status"], "verified")
        self.assertEqual(response.json()["call_counts"]["routing"], 2)
        self.assertEqual(request_route.call_count, 2)

    def test_expired_map_returns_410(self):
        result = MapResult.objects.create(
            cache_key="expired", result={}, calculated_at=timezone.now() - timedelta(days=2),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.get(reverse("saved-map", args=[result.token]))
        self.assertEqual(response.status_code, 410)

    def test_saved_map_allows_cross_origin_referrer_for_tiles(self):
        result = MapResult.objects.create(
            cache_key="active-map", result={}, calculated_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        response = self.client.get(reverse("saved-map", args=[result.token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_non_usa_coordinates_are_rejected_without_provider_calls(self):
        response = self.client.post(reverse("route"), {
            "start": {"latitude": 18.4655, "longitude": -66.1057},
            "finish": {"latitude": 40.7128, "longitude": -74.0060},
        }, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    @patch("routing.services.planner.resolve_endpoint")
    @patch("routing.services.planner.request_route")
    def test_optimized_cache_reuses_route_and_station_change_invalidates_it(self, request_route, resolve):
        from routing.services.endpoint_resolution import EndpointResolution, ResolvedEndpoint
        def resolved(value):
            endpoint = ResolvedEndpoint(value["latitude"], value["longitude"], "Point", "coordinates")
            return EndpointResolution(endpoint, 0, False)
        resolve.side_effect = resolved
        route = OSRMRoute(
            geometry=[[-100, 38], [-99, 38]], segment_distances_meters=[100 * METERS_PER_MILE],
            leg_distances_meters=[100 * METERS_PER_MILE], distance_meters=100 * METERS_PER_MILE,
            duration_seconds=6000,
        )
        def mocked_route(_waypoints, *, budget, **_kwargs):
            budget.consume()
            return route
        request_route.side_effect = mocked_route
        payload = {
            "start": {"latitude": 38, "longitude": -100},
            "finish": {"latitude": 38, "longitude": -99},
        }
        first = self.client.post(reverse("route"), payload, content_type="application/json")
        second = self.client.post(reverse("route"), payload, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()["cache"]["optimized_response"])
        self.assertEqual(second.json()["call_counts"]["routing"], 0)
        Station.objects.filter(pk=self.station.pk).update(retail_price="3.20000000")
        third = self.client.post(reverse("route"), payload, content_type="application/json")
        self.assertFalse(third.json()["cache"]["optimized_response"])
        self.assertTrue(third.json()["cache"]["base_route"])
        self.assertEqual(request_route.call_count, 1)

    @patch("routing.services.planner.resolve_endpoint")
    @patch("routing.services.planner.request_route")
    def test_infeasible_verified_detour_returns_error(self, request_route, resolve):
        from routing.services.endpoint_resolution import EndpointResolution, ResolvedEndpoint
        resolve.side_effect = [
            EndpointResolution(ResolvedEndpoint(38, -100, "Start", "coordinates"), 0, False),
            EndpointResolution(ResolvedEndpoint(39, -97, "Finish", "coordinates"), 0, False),
        ]
        base = OSRMRoute(
            geometry=[[-100, 38], [-98.5, 38.5], [-97, 39]],
            segment_distances_meters=[300 * METERS_PER_MILE, 300 * METERS_PER_MILE],
            leg_distances_meters=[600 * METERS_PER_MILE], distance_meters=600 * METERS_PER_MILE,
            duration_seconds=36000,
        )
        impossible = OSRMRoute(
            geometry=base.geometry, segment_distances_meters=base.segment_distances_meters,
            leg_distances_meters=[501 * METERS_PER_MILE, 200 * METERS_PER_MILE],
            distance_meters=701 * METERS_PER_MILE, duration_seconds=40000,
        )
        routes = iter([base, impossible])
        def mocked_route(_waypoints, *, budget, **_kwargs):
            budget.consume()
            return next(routes)
        request_route.side_effect = mocked_route
        response = self.client.post(reverse("route"), {
            "start": {"latitude": 38, "longitude": -100},
            "finish": {"latitude": 39, "longitude": -97},
        }, content_type="application/json")
        self.assertEqual(response.status_code, 422)
        self.assertIn("could not be verified", response.json()["error"])
