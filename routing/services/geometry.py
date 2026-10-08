from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from stations.models import Station
from stations.services.spatial_index import station_ids_in_boxes


EARTH_RADIUS_METERS = 6_371_008.8
METERS_PER_MILE = 1609.344


@dataclass(frozen=True)
class CorridorStation:
    station_id: int
    source_id: str
    name: str
    latitude: float
    longitude: float
    price: Decimal
    progress_miles: float
    distance_to_route_miles: float


def haversine_meters(latitude1, longitude1, latitude2, longitude2):
    phi1, phi2 = math.radians(latitude1), math.radians(latitude2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(((longitude2 - longitude1 + 180) % 360) - 180)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(min(1, math.sqrt(value)))


def _bearing(latitude1, longitude1, latitude2, longitude2):
    phi1, phi2 = math.radians(latitude1), math.radians(latitude2)
    delta = math.radians(((longitude2 - longitude1 + 180) % 360) - 180)
    return math.atan2(
        math.sin(delta) * math.cos(phi2),
        math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta),
    )


def _distance_and_fraction(point, start, finish):
    segment = haversine_meters(start[0], start[1], finish[0], finish[1])
    if segment < 0.01:
        return haversine_meters(point[0], point[1], start[0], start[1]), 0.0
    delta13 = haversine_meters(start[0], start[1], point[0], point[1]) / EARTH_RADIUS_METERS
    theta13 = _bearing(start[0], start[1], point[0], point[1])
    theta12 = _bearing(start[0], start[1], finish[0], finish[1])
    cross_angle = math.asin(max(-1, min(1, math.sin(delta13) * math.sin(theta13 - theta12))))
    along_angle = math.atan2(math.sin(delta13) * math.cos(theta13 - theta12), math.cos(delta13))
    fraction = along_angle * EARTH_RADIUS_METERS / segment
    if fraction <= 0:
        return haversine_meters(point[0], point[1], start[0], start[1]), 0.0
    if fraction >= 1:
        return haversine_meters(point[0], point[1], finish[0], finish[1]), 1.0
    return abs(cross_angle) * EARTH_RADIUS_METERS, fraction


def _segment_boxes(start, finish, corridor_miles):
    latitude_pad = corridor_miles / 69.0
    middle_latitude = (start[0] + finish[0]) / 2
    longitude_pad = corridor_miles / max(1.0, 69.172 * abs(math.cos(math.radians(middle_latitude))))
    longitude1, longitude2 = start[1], finish[1]
    latitude_min = max(-90, min(start[0], finish[0]) - latitude_pad)
    latitude_max = min(90, max(start[0], finish[0]) + latitude_pad)
    if abs(longitude1 - longitude2) <= 180:
        return [(max(-180, min(longitude1, longitude2) - longitude_pad), min(180, max(longitude1, longitude2) + longitude_pad), latitude_min, latitude_max)]
    western = max(longitude1, longitude2)
    eastern = min(longitude1, longitude2)
    return [
        (max(-180, western - longitude_pad), 180, latitude_min, latitude_max),
        (-180, min(180, eastern + longitude_pad), latitude_min, latitude_max),
    ]


def corridor_stations(route, corridor_miles):
    coordinates = [(float(lat_lon[1]), float(lat_lon[0])) for lat_lon in route.geometry]
    boxes = []
    for start, finish in zip(coordinates, coordinates[1:]):
        boxes.extend(_segment_boxes(start, finish, corridor_miles))
    candidate_ids = station_ids_in_boxes(boxes)
    stations = Station.objects.filter(
        id__in=candidate_ids,
        geocoding_status=Station.GeocodingStatus.MATCHED,
        latitude__isnull=False,
        longitude__isnull=False,
        retail_price__gt=0,
    )
    cumulative = 0.0
    segment_starts = []
    for distance in route.segment_distances_meters:
        segment_starts.append(cumulative)
        cumulative += distance
    selected = []
    corridor_meters = corridor_miles * METERS_PER_MILE
    for station in stations:
        point = (float(station.latitude), float(station.longitude))
        best = None
        for index, (start, finish) in enumerate(zip(coordinates, coordinates[1:])):
            distance, fraction = _distance_and_fraction(point, start, finish)
            if best is None or distance < best[0]:
                progress = segment_starts[index] + route.segment_distances_meters[index] * fraction
                best = (distance, progress)
        if best and best[0] <= corridor_meters:
            selected.append(CorridorStation(
                station_id=station.id,
                source_id=station.source_truckstop_id,
                name=station.name,
                latitude=point[0], longitude=point[1], price=station.retail_price,
                progress_miles=best[1] / METERS_PER_MILE,
                distance_to_route_miles=best[0] / METERS_PER_MILE,
            ))
    return sorted(selected, key=lambda item: (item.progress_miles, item.price, item.station_id))
