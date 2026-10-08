from dataclasses import dataclass
from decimal import Decimal

from django.test import SimpleTestCase

from optimization.services.fuel_optimizer import InfeasibleFuelPlan, optimize_route


@dataclass(frozen=True)
class Candidate:
    station_id: int
    source_id: str
    name: str
    latitude: float
    longitude: float
    price: Decimal
    progress_miles: float
    distance_to_route_miles: float = 0


class FuelOptimizerTests(SimpleTestCase):
    def station(self, identifier, mile, price):
        return Candidate(identifier, str(identifier), f"Station {identifier}", 40, -100, Decimal(str(price)), mile)

    def reference_cost(self, stations, total_miles, initial_fuel):
        """Independent integer-gallon dynamic program for small fixtures."""
        points = [(0, None)] + [(item.progress_miles, item.price) for item in stations] + [(total_miles, None)]
        states = {int(initial_fuel): Decimal("0")}
        for index in range(1, len(points)):
            gallons_needed = int((points[index][0] - points[index - 1][0]) / 10)
            arrived = {
                fuel - gallons_needed: cost
                for fuel, cost in states.items()
                if fuel >= gallons_needed
            }
            if index == len(points) - 1:
                return min(arrived.values())
            states = {}
            for fuel, cost in arrived.items():
                for purchase in range(0, 51 - fuel):
                    new_cost = cost + points[index][1] * purchase
                    states[fuel + purchase] = min(states.get(fuel + purchase, new_cost), new_cost)
        raise AssertionError("reference solver did not reach destination")

    def test_initial_fuel_is_free_and_not_included_in_cost(self):
        plan = optimize_route([], 400, 50)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0)
        self.assertAlmostEqual(plan.remaining_fuel_gallons, 10)

    def test_buys_only_enough_to_reach_cheaper_station(self):
        stations = [self.station(1, 400, 4.00), self.station(2, 600, 3.00)]
        plan = optimize_route(stations, 900, 50)
        self.assertEqual([stop.station_id for stop in plan.stops], [1, 2])
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 10)
        self.assertAlmostEqual(plan.stops[1].gallons_purchased, 30)
        self.assertEqual(plan.total_cost, Decimal("130.0"))

    def test_reports_range_gap(self):
        with self.assertRaisesRegex(InfeasibleFuelPlan, "no reachable eligible station"):
            optimize_route([self.station(1, 501, 3.00)], 700, 50)

    def test_partial_initial_tank_must_reach_first_station(self):
        with self.assertRaises(InfeasibleFuelPlan):
            optimize_route([self.station(1, 100, 3.00)], 200, 5)

    def test_matches_independent_reference_solver(self):
        stations = [
            self.station(1, 300, 4.00),
            self.station(2, 500, 3.50),
            self.station(3, 700, 4.25),
        ]
        plan = optimize_route(stations, 900, 50)
        self.assertEqual(plan.total_cost, self.reference_cost(stations, 900, 50))
