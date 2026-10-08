from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction

from stations.models import SourceRecord, Station


COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)


@dataclass
class ImportSummary:
    imported: int = 0
    unchanged: int = 0
    invalid: int = 0
    conflicting: int = 0


def _clean_row(row):
    raw_values = {column: (row.get(column) or "") for column in COLUMNS}
    values = {column: value.strip() for column, value in raw_values.items()}
    missing = [column for column, value in values.items() if not value]
    if missing:
        raise ValueError(f"missing required values: {', '.join(missing)}")
    if len(values["State"]) != 2:
        raise ValueError("State must be a two-letter code")
    values["State"] = values["State"].upper()
    try:
        price = Decimal(values["Retail Price"])
    except InvalidOperation as exc:
        raise ValueError("Retail Price is not a decimal") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("Retail Price must be positive")
    values["Retail Price"] = price
    return values, raw_values


def _fingerprint(raw_values):
    serializable = [raw_values[column] for column in COLUMNS]
    return hashlib.sha256(json.dumps(serializable, separators=(",", ":")).encode()).hexdigest()


def _conflicts(station, values):
    return any(
        (
            station.name != values["Truckstop Name"],
            station.address != values["Address"],
            station.city != values["City"],
            station.state != values["State"],
            station.rack_id != values["Rack ID"],
            station.retail_price != values["Retail Price"],
        )
    )


def import_stations(path, *, error_callback=None):
    summary = ImportSummary()
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_headers = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
        if missing_headers:
            raise ValueError(f"CSV is missing columns: {', '.join(missing_headers)}")
        for line_number, row in enumerate(reader, start=2):
            try:
                values, raw_values = _clean_row(row)
                fingerprint = _fingerprint(raw_values)
            except ValueError as exc:
                summary.invalid += 1
                if error_callback:
                    error_callback(line_number, str(exc))
                continue

            with transaction.atomic():
                if SourceRecord.objects.filter(fingerprint=fingerprint).exists():
                    summary.unchanged += 1
                    continue
                station, created = Station.objects.get_or_create(
                    source_truckstop_id=values["OPIS Truckstop ID"],
                    defaults={
                        "name": values["Truckstop Name"],
                        "address": values["Address"],
                        "city": values["City"],
                        "state": values["State"],
                        "rack_id": values["Rack ID"],
                        "retail_price": values["Retail Price"],
                    },
                )
                conflicting = not created and _conflicts(station, values)
                SourceRecord.objects.create(
                    station=station,
                    fingerprint=fingerprint,
                    source_truckstop_id=values["OPIS Truckstop ID"],
                    truckstop_name=raw_values["Truckstop Name"],
                    address=raw_values["Address"],
                    city=raw_values["City"],
                    state=raw_values["State"],
                    rack_id=raw_values["Rack ID"],
                    retail_price=values["Retail Price"],
                    raw_values=raw_values,
                    is_conflicting=conflicting,
                )
                if conflicting:
                    Station.objects.filter(pk=station.pk).update(requires_review=True)
                    summary.conflicting += 1
                    if error_callback:
                        error_callback(
                            line_number,
                            f"conflicts with canonical OPIS Truckstop ID {station.source_truckstop_id}",
                        )
                else:
                    summary.imported += 1
    return summary
