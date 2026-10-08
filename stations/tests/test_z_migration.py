from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RTreeBackfillMigrationTests(TransactionTestCase):
    migrate_from = [("stations", "0001_station_foundation")]
    migrate_to = [("stations", "0002_station_rtree")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)

    def tearDown(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_matched_station_is_backfilled(self):
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        Station = old_apps.get_model("stations", "Station")
        station = Station.objects.create(
            source_truckstop_id="backfill", name="Backfill Fuel", address="1 Main St",
            city="Austin", state="TX", rack_id="R", retail_price=Decimal("3.10000000"),
            geocoding_status="matched", latitude=Decimal("30.0000000"),
            longitude=Decimal("-97.0000000"),
        )
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM station_location_rtree WHERE id = %s", [station.id])
            self.assertEqual(cursor.fetchone()[0], station.id)
