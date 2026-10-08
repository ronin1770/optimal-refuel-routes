import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from stations.models import SourceRecord, Station
from stations.services.csv_import import import_stations


HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


class CsvImportTests(TestCase):
    def write_csv(self, body):
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", delete=False)
        handle.write(HEADER + body)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_preserves_precision_and_is_idempotent(self):
        path = self.write_csv('20,PILOT #1243,"I-8, EXIT 119",Gila Bend,AZ,930,3.89912345\n')
        first = import_stations(path)
        second = import_stations(path)
        self.assertEqual((first.imported, second.unchanged), (1, 1))
        self.assertEqual(Station.objects.get().retail_price, Decimal("3.89912345"))
        self.assertEqual(SourceRecord.objects.count(), 1)

    def test_first_valid_record_remains_canonical_and_conflict_is_retained(self):
        path = self.write_csv(
            '20,PILOT #1243,"I-8, EXIT 119",Gila Bend,AZ,930,3.89900000\n'
            '20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119",Gila Bend,AZ,930,3.99900000\n'
        )
        summary = import_stations(path)
        station = Station.objects.get()
        self.assertEqual(station.name, "PILOT #1243")
        self.assertEqual(station.retail_price, Decimal("3.89900000"))
        self.assertTrue(station.requires_review)
        self.assertEqual(summary.conflicting, 1)
        self.assertEqual(SourceRecord.objects.filter(is_conflicting=True).count(), 1)

    def test_invalid_rows_are_counted(self):
        path = self.write_csv("1,Name,Address,City,XX,Rack,-1\n2,,Address,City,TX,Rack,3.2\n")
        summary = import_stations(path)
        self.assertEqual(summary.invalid, 2)
        self.assertFalse(Station.objects.exists())

    def test_csv_conflict_preserves_verified_coordinates(self):
        first = self.write_csv("1,Name,1 Main St,Austin,TX,Rack,3.10000000\n")
        import_stations(first)
        Station.objects.update(
            geocoding_status="matched", latitude=Decimal("30.1000000"),
            longitude=Decimal("-97.1000000"), coordinate_source="test",
        )
        conflict = self.write_csv("1,Other Name,2 Main St,Austin,TX,Rack,3.20000000\n")
        import_stations(conflict)
        station = Station.objects.get()
        self.assertEqual(station.geocoding_status, "matched")
        self.assertEqual(station.latitude, Decimal("30.1000000"))
