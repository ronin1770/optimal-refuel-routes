from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


TANK_CAPACITY_GALLONS = 50.0
MILES_PER_GALLON = 10.0
MAX_RANGE_MILES = TANK_CAPACITY_GALLONS * MILES_PER_GALLON
EPSILON = 1e-7
PRICE_EPSILON = Decimal("0.00000001")


class InfeasibleFuelPlan(ValueError):
    pass


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    source_id: str
    name: str
    latitude: float
    longitude: float
    price: Decimal
    progress_miles: float
    distance_to_route_miles: float
    gallons_purchased: float
    purchase_cost: Decimal
    fuel_after_purchase: float


@dataclass(frozen=True)
class FuelPlan:
    stops: list[FuelStop]
    distance_miles: float
    gallons_consumed: float
    gallons_purchased: float
    remaining_fuel_gallons: float
    total_cost: Decimal
    feasible: bool


def _plan(stations, total_miles, initial_fuel_gallons):
    if not 0 <= initial_fuel_gallons <= TANK_CAPACITY_GALLONS:
        raise InfeasibleFuelPlan("initial fuel must be between 0 and 50 gallons")
    ordered = [item for item in stations if EPSILON < item.progress_miles < total_miles - EPSILON]
    ordered.sort(key=lambda item: (item.progress_miles, item.price, item.station_id))
    fuel = float(initial_fuel_gallons)
    previous_progress = 0.0
    purchases = []
    total_cost = Decimal("0")
    total_purchased = 0.0

    for index, station in enumerate(ordered):
        travel = station.progress_miles - previous_progress
        fuel -= travel / MILES_PER_GALLON
        if fuel < -EPSILON:
            raise InfeasibleFuelPlan(
                f"route is infeasible: no reachable eligible station before mile {station.progress_miles:.1f}"
            )
        fuel = max(0.0, fuel)
        future = ordered[index + 1:]
        cheaper = next(
            (
                candidate for candidate in future
                if candidate.progress_miles - station.progress_miles <= MAX_RANGE_MILES + EPSILON
                and candidate.price < station.price - PRICE_EPSILON
            ),
            None,
        )
        destination_distance = total_miles - station.progress_miles
        if cheaper is not None:
            target_fuel = (cheaper.progress_miles - station.progress_miles) / MILES_PER_GALLON
        elif destination_distance <= MAX_RANGE_MILES + EPSILON:
            target_fuel = destination_distance / MILES_PER_GALLON
        else:
            any_reachable = any(
                candidate.progress_miles - station.progress_miles <= MAX_RANGE_MILES + EPSILON
                for candidate in future
            )
            if not any_reachable:
                raise InfeasibleFuelPlan(
                    f"route is infeasible: no eligible station within 500 miles after {station.name}"
                )
            target_fuel = TANK_CAPACITY_GALLONS
        gallons = max(0.0, min(TANK_CAPACITY_GALLONS, target_fuel) - fuel)
        if gallons > EPSILON:
            cost = Decimal(str(gallons)) * station.price
            fuel += gallons
            total_purchased += gallons
            total_cost += cost
            purchases.append(FuelStop(
                station_id=station.station_id,
                source_id=station.source_id,
                name=station.name,
                latitude=station.latitude,
                longitude=station.longitude,
                price=station.price,
                progress_miles=station.progress_miles,
                distance_to_route_miles=station.distance_to_route_miles,
                gallons_purchased=gallons,
                purchase_cost=cost,
                fuel_after_purchase=fuel,
            ))
        previous_progress = station.progress_miles

    fuel -= (total_miles - previous_progress) / MILES_PER_GALLON
    if fuel < -EPSILON:
        raise InfeasibleFuelPlan("route is infeasible: the destination is beyond the available fuel range")
    return FuelPlan(
        stops=purchases,
        distance_miles=total_miles,
        gallons_consumed=total_miles / MILES_PER_GALLON,
        gallons_purchased=total_purchased,
        remaining_fuel_gallons=max(0.0, fuel),
        total_cost=total_cost,
        feasible=True,
    )


def optimize_route(stations, total_miles, initial_fuel_gallons):
    return _plan(stations, float(total_miles), float(initial_fuel_gallons))


def optimize_verified_route(selected_stations, leg_distances_miles, initial_fuel_gallons):
    if len(leg_distances_miles) != len(selected_stations) + 1:
        raise InfeasibleFuelPlan("verified routing legs do not match the selected stops")
    progress = 0.0
    rebuilt = []
    for station, leg_distance in zip(selected_stations, leg_distances_miles):
        progress += float(leg_distance)
        rebuilt.append(type(station)(
            station_id=station.station_id,
            source_id=station.source_id,
            name=station.name,
            latitude=station.latitude,
            longitude=station.longitude,
            price=station.price,
            progress_miles=progress,
            distance_to_route_miles=station.distance_to_route_miles,
        ))
    return _plan(rebuilt, sum(float(value) for value in leg_distances_miles), initial_fuel_gallons)
