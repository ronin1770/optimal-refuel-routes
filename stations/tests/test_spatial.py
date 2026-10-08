from decimal import Decimal

from django.db import IntegrityError, connection
from django.test import TransactionTestCase

from stations.models import Station
from stations.services.spatial_index import station_ids_in_bounds, verify_rtree_support


def station_values(**overrides):
    values = {
        "source_truckstop_id": "1", "name": "Fuel One", "address": "1 Main St",
        "city": "Austin", "state": "TX", "rack_id": "R1",
        "retail_price": Decimal("3.12345678"),
    }
    values.update(overrides)
    return values


class SpatialIndexTests(TransactionTestCase):
    reset_sequences = True

    def test_rtree_insert_update_delete_and_bounds_query(self):
        verify_rtree_support()
        station = Station.objects.create(
            **station_values(), geocoding_status="matched",
            latitude=Decimal("30.0000000"), longitude=Decimal("-97.0000000"),
        )
        self.assertEqual(
            station_ids_in_bounds(min_longitude=-98, max_longitude=-96, min_latitude=29, max_latitude=31),
            [station.id],
        )
        Station.objects.filter(pk=station.pk).update(longitude=Decimal("-110.0000000"))
        self.assertEqual(station_ids_in_bounds(min_longitude=-98, max_longitude=-96, min_latitude=29, max_latitude=31), [])
        station.delete()
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM station_location_rtree")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_bulk_identity_edit_invalidates_coordinates_and_index(self):
        station = Station.objects.create(
            **station_values(), geocoding_status="matched",
            latitude=Decimal("30.0000000"), longitude=Decimal("-97.0000000"),
            coordinate_source="test",
        )
        Station.objects.filter(pk=station.pk).update(address="2 Main St")
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, "pending")
        self.assertIsNone(station.latitude)
        self.assertEqual(station_ids_in_bounds(min_longitude=-180, max_longitude=180, min_latitude=-90, max_latitude=90), [])

    def test_price_only_bulk_edit_preserves_coordinates(self):
        station = Station.objects.create(
            **station_values(), geocoding_status="matched",
            latitude=Decimal("30.0000000"), longitude=Decimal("-97.0000000"),
        )
        Station.objects.filter(pk=station.pk).update(retail_price=Decimal("4.00000000"))
        station.refresh_from_db()
        self.assertEqual(station.latitude, Decimal("30.0000000"))
        self.assertEqual(station.geocoding_status, "matched")

    def test_database_constraints(self):
        with self.assertRaises(IntegrityError):
            Station.objects.create(**station_values(retail_price=Decimal("0")))
        with self.assertRaises(IntegrityError):
            Station.objects.create(**station_values(source_truckstop_id="2", latitude=Decimal("30")))
