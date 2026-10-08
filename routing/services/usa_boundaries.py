from __future__ import annotations

import json
from functools import lru_cache

from django.conf import settings


def _unwrap(longitude, reference):
    while longitude - reference > 180:
        longitude -= 360
    while longitude - reference < -180:
        longitude += 360
    return longitude


def _ring_contains(ring, longitude, latitude):
    inside = False
    epsilon = 1e-10
    for index in range(len(ring) - 1):
        x1 = _unwrap(float(ring[index][0]), longitude)
        y1 = float(ring[index][1])
        x2 = _unwrap(float(ring[index + 1][0]), longitude)
        y2 = float(ring[index + 1][1])
        dx, dy = x2 - x1, y2 - y1
        cross = (longitude - x1) * dy - (latitude - y1) * dx
        if abs(cross) <= epsilon and min(x1, x2) - epsilon <= longitude <= max(x1, x2) + epsilon and min(y1, y2) - epsilon <= latitude <= max(y1, y2) + epsilon:
            return True, True
        if (y1 > latitude) != (y2 > latitude):
            crossing = x1 + (latitude - y1) * dx / dy
            if longitude < crossing:
                inside = not inside
    return inside, False


def _polygon_contains(rings, longitude, latitude):
    inside, boundary = _ring_contains(rings[0], longitude, latitude)
    if boundary:
        return True
    if not inside:
        return False
    for hole in rings[1:]:
        in_hole, hole_boundary = _ring_contains(hole, longitude, latitude)
        if hole_boundary:
            return True
        if in_hole:
            return False
    return True


@lru_cache(maxsize=1)
def load_usa_boundaries():
    with open(settings.USA_BOUNDARY_GEOJSON, encoding="utf-8") as handle:
        payload = json.load(handle)
    polygons = []
    for feature in payload["features"]:
        geometry = feature["geometry"]
        coordinates = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            polygons.append(coordinates)
        elif geometry["type"] == "MultiPolygon":
            polygons.extend(coordinates)
    return tuple(polygons)


def is_in_usa(latitude, longitude):
    latitude = float(latitude)
    longitude = float(longitude)
    return any(
        _polygon_contains(polygon, longitude, latitude)
        for polygon in load_usa_boundaries()
    )
