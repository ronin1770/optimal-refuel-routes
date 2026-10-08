import csv
import json
import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from stations.models import Station


class Command(BaseCommand):
    help = "Atomically export station coordinates and review statuses"

    def add_arguments(self, parser):
        parser.add_argument("output_path")

    def handle(self, *args, **options):
        destination = Path(options["output_path"]).resolve()
        if not destination.parent.is_dir():
            raise CommandError(f"output directory does not exist: {destination.parent}")
        fields = [
            "id", "source_truckstop_id", "name", "address", "city", "state", "rack_id", "retail_price",
            "latitude", "longitude", "geocoding_status", "coordinate_source",
            "geocoded_at", "geocoding_decision_summary", "requires_review", "created_at", "updated_at",
        ]
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=destination.parent,
                prefix=f".{destination.name}.", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for station in Station.objects.order_by("source_truckstop_id", "id").iterator():
                    row = {field: getattr(station, field) for field in fields}
                    for timestamp in ("geocoded_at", "created_at", "updated_at"):
                        value = getattr(station, timestamp)
                        row[timestamp] = value.isoformat() if value else ""
                    row["geocoding_decision_summary"] = json.dumps(
                        station.geocoding_decision_summary, separators=(",", ":")
                    )
                    writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        except OSError as exc:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"exported coordinates to {destination}")
