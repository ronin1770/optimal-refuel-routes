from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from integrations.exceptions import ProviderError, ProviderResponseError
from integrations.osrm import OSRMNoRoute, OSRMRoute
from optimization.services.fuel_optimizer import (
    InfeasibleFuelPlan,
    optimize_route,
    optimize_verified_route,
)
from routing.models import MapResult
from routing.services.endpoint_resolution import resolve_endpoint
from routing.services.geometry import METERS_PER_MILE, corridor_stations
from routing.services.osrm_routing import RoutingCallBudget, request_route
from stations.models import StationDatasetVersion


class JourneyPlanningError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _route_to_dict(route):
    return {
        "geometry": route.geometry,
        "segment_distances_meters": route.segment_distances_meters,
        "leg_distances_meters": route.leg_distances_meters,
        "distance_meters": route.distance_meters,
        "duration_seconds": route.duration_seconds,
    }


def _route_from_dict(value):
    return OSRMRoute(**value)


def _station_dict(station):
    return {
        "station_id": station.station_id,
        "source_truckstop_id": station.source_id,
        "name": station.name,
        "latitude": round(station.latitude, 7),
        "longitude": round(station.longitude, 7),
        "retail_price": float(round(station.price, 8)),
        "route_progress_miles": round(station.progress_miles, 3),
        "distance_to_route_miles": round(station.distance_to_route_miles, 3),
        "gallons_purchased": round(station.gallons_purchased, 3),
        "purchase_cost": float(round(station.purchase_cost, 2)),
        "purchase_cost_usd": float(round(station.purchase_cost, 2)),
        "fuel_after_purchase_gallons": round(station.fuel_after_purchase, 3),
    }


def _endpoint_dict(endpoint):
    return {
        "latitude": round(endpoint.latitude, 7),
        "longitude": round(endpoint.longitude, 7),
        "label": endpoint.label,
        "source": endpoint.source,
    }


def _get_base_route(start, finish, budget, deadline):
    points = [[round(start.latitude, 7), round(start.longitude, 7)], [round(finish.latitude, 7), round(finish.longitude, 7)]]
    key = "base-route:" + _digest({"points": points, "provider": settings.OSRM_BASE_URL})
    cached = cache.get(key)
    if cached:
        return _route_from_dict(cached), True
    route = request_route(points, budget=budget, deadline_monotonic=deadline, max_attempts=2)
    cache.set(key, _route_to_dict(route), settings.ROUTE_CACHE_SECONDS)
    return route, False


def _create_map(cache_key, response):
    now = timezone.now()
    map_result = MapResult.objects.create(
        cache_key=cache_key,
        calculated_at=now,
        expires_at=now + timedelta(seconds=settings.ROUTE_CACHE_SECONDS),
        result=response,
    )
    return f"/maps/{map_result.token}/", now


def plan_journey(data):
    started = time.monotonic()
    start_result = resolve_endpoint(data["start"])
    finish_result = resolve_endpoint(data["finish"])
    geocoding_calls = start_result.geocoding_calls + finish_result.geocoding_calls
    start, finish = start_result.endpoint, finish_result.endpoint
    dataset, _ = StationDatasetVersion.objects.get_or_create(name="stations")
    verify = data.get("verify_detours", True)
    initial_fuel = float(data.get("initial_fuel_gallons", 50.0))
    journey_value = {
        "start": [round(start.latitude, 7), round(start.longitude, 7)],
        "finish": [round(finish.latitude, 7), round(finish.longitude, 7)],
        "initial_fuel": initial_fuel,
        "verify_detours": verify,
        "corridor_miles": settings.ROUTE_CORRIDOR_MILES,
        "vehicle": {"tank_gallons": 50, "mpg": 10},
        "optimizer_version": 1,
        "routing_provider": settings.OSRM_BASE_URL,
        "dataset_version": dataset.version,
    }
    optimized_key = "optimized-route:" + _digest(journey_value)
    cached = cache.get(optimized_key)
    if cached:
        response = copy.deepcopy(cached)
        response["call_counts"] = {"geocoding": geocoding_calls, "routing": 0}
        response["cache"] = {"optimized_response": True, "base_route": True}
        response["latency_seconds"] = round(time.monotonic() - started, 3)
        return response

    budget = RoutingCallBudget(3)
    deadline = time.monotonic() + settings.ROUTE_PROVIDER_DEADLINE_SECONDS
    base_route, base_cache_hit = _get_base_route(start, finish, budget, deadline)
    candidates = corridor_stations(base_route, settings.ROUTE_CORRIDOR_MILES)
    try:
        plan = optimize_route(
            candidates, base_route.distance_meters / METERS_PER_MILE, initial_fuel
        )
    except InfeasibleFuelPlan as exc:
        raise JourneyPlanningError(str(exc)) from exc

    result_route = base_route
    verification_status = "unverified"
    warnings = []
    if verify and plan.stops:
        selected = [
            next(item for item in candidates if item.station_id == stop.station_id)
            for stop in plan.stops
        ]
        waypoints = [(start.latitude, start.longitude)] + [
            (station.latitude, station.longitude) for station in selected
        ] + [(finish.latitude, finish.longitude)]
        try:
            result_route = request_route(
                waypoints,
                budget=budget,
                deadline_monotonic=deadline,
                max_attempts=min(2, budget.remaining),
            )
            plan = optimize_verified_route(
                selected,
                [distance / METERS_PER_MILE for distance in result_route.leg_distances_meters],
                initial_fuel,
            )
        except InfeasibleFuelPlan as exc:
            raise JourneyPlanningError(
                f"a feasible selected-stop itinerary could not be verified within the three-call routing budget: {exc}"
            ) from exc
        except OSRMNoRoute:
            raise
        except ProviderError as exc:
            raise ProviderResponseError(
                f"the selected-stop itinerary could not be verified within the three-call routing budget: {exc}"
            ) from exc
        verification_status = "verified"
    elif verify:
        verification_status = "not_required"
    else:
        warnings.append("Detour verification was disabled; route and fuel-stop costs are estimates.")

    response = {
        "start": _endpoint_dict(start),
        "finish": _endpoint_dict(finish),
        "route": {
            "geometry": result_route.geometry,
            "distance_miles": round(result_route.distance_meters / METERS_PER_MILE, 3),
            "duration_seconds": round(result_route.duration_seconds, 1),
        },
        "fuel_plan": {
            "vehicle": {"tank_capacity_gallons": 50, "miles_per_gallon": 10, "maximum_range_miles": 500},
            "initial_fuel_gallons": round(initial_fuel, 3),
            "initial_fuel_cost_included": False,
            "stops": [_station_dict(stop) for stop in plan.stops],
            "gallons_consumed": round(plan.gallons_consumed, 3),
            "gallons_purchased": round(plan.gallons_purchased, 3),
            "remaining_fuel_gallons": round(plan.remaining_fuel_gallons, 3),
            "total_purchase_cost": float(round(plan.total_cost, 2)),
            "total_purchase_cost_usd": float(round(plan.total_cost, 2)),
            "range_feasible": plan.feasible,
        },
        "total_fuel_cost_usd": float(round(plan.total_cost, 2)),
        "verification_status": verification_status,
        "warnings": warnings,
        "station_dataset_version": dataset.version,
        "call_counts": {"geocoding": geocoding_calls, "routing": budget.used},
        "cache": {"optimized_response": False, "base_route": base_cache_hit},
    }
    map_url, calculated_at = _create_map(optimized_key, response)
    response["map_url"] = map_url
    response["calculated_at"] = calculated_at.isoformat()
    response["latency_seconds"] = round(time.monotonic() - started, 3)
    # Save the finalized payload in both stores. The map row is updated because
    # it was created before its own URL was known.
    MapResult.objects.filter(token=map_url.rstrip("/").split("/")[-1]).update(result=response)
    cache.set(optimized_key, response, max(1, settings.ROUTE_CACHE_SECONDS - 1))
    return response
