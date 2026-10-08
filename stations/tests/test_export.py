import csv
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from stations.models import Station


class CoordinateExportTests(TestCase):
    def test_export_includes_unresolved_and_is_deterministic(self):
        Station.objects.create(source_truckstop_id="2", name="B", address="B", city="B", state="TX", rack_id="2", retail_price=Decimal("3"), geocoding_status="unresolved")
        Station.objects.create(source_truckstop_id="1", name="A", address="A", city="A", state="OK", rack_id="1", retail_price=Decimal("4"))
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(directory.rmdir)
        self.addCleanup(lambda: [path.unlink() for path in directory.iterdir()])
        output = directory / "coordinates.csv"
        call_command("export_station_coordinates", str(output))
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["source_truckstop_id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[1]["geocoding_status"], "unresolved")
