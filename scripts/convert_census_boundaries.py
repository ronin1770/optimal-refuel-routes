#!/usr/bin/env python3
"""Convert the Census 2025 state KML to the bundled 50-state/DC GeoJSON.

Usage:
    python scripts/convert_census_boundaries.py input.zip routing/data/us_states_dc_2025_500k.geojson

The input may be the downloaded Census ZIP archive or an extracted KML file.
"""

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


INCLUDED = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
NS = {"k": "http://www.opengis.net/kml/2.2"}


def coordinates(element):
    text = element.findtext(".//k:coordinates", namespaces=NS) or ""
    return [[float(parts[0]), float(parts[1])] for token in text.split() if len(parts := token.split(",")) >= 2]


def main(source, destination):
    source = Path(source)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            member = next((name for name in archive.namelist() if name.lower().endswith(".kml")), None)
            if member is None:
                raise SystemExit("input archive contains no KML file")
            with archive.open(member) as handle:
                root = ET.parse(handle).getroot()
    else:
        root = ET.parse(source).getroot()
    features = []
    for placemark in root.findall(".//k:Placemark", NS):
        properties = {
            node.attrib["name"]: (node.text or "")
            for node in placemark.findall(".//k:SimpleData", NS)
        }
        if properties.get("STUSPS") not in INCLUDED:
            continue
        polygons = []
        for polygon in placemark.findall(".//k:Polygon", NS):
            outer = polygon.find("k:outerBoundaryIs", NS)
            if outer is None:
                continue
            rings = [coordinates(outer)]
            rings.extend(coordinates(inner) for inner in polygon.findall("k:innerBoundaryIs", NS))
            polygons.append(rings)
        geometry = {
            "type": "Polygon" if len(polygons) == 1 else "MultiPolygon",
            "coordinates": polygons[0] if len(polygons) == 1 else polygons,
        }
        features.append({
            "type": "Feature",
            "properties": {key: properties.get(key) for key in ("STATEFP", "STUSPS", "NAME")},
            "geometry": geometry,
        })
    if len(features) != 51:
        raise SystemExit(f"expected 51 state/DC features, found {len(features)}")
    payload = {
        "type": "FeatureCollection",
        "name": "cb_2025_us_state_500k_50_states_dc",
        "source": "https://www2.census.gov/geo/tiger/GENZ2025/kml/cb_2025_us_state_500k.zip",
        "features": sorted(features, key=lambda feature: feature["properties"]["STUSPS"]),
    }
    Path(destination).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
