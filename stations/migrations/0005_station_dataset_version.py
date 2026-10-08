from django.db import migrations, models


INSERT_SQL = """
INSERT OR IGNORE INTO stations_stationdatasetversion(name, version, updated_at)
VALUES ('stations', 0, CURRENT_TIMESTAMP)
"""

INSERT_TRIGGER_SQL = """
CREATE TRIGGER station_dataset_version_after_insert
AFTER INSERT ON stations_station
BEGIN
    UPDATE stations_stationdatasetversion
       SET version = version + 1, updated_at = CURRENT_TIMESTAMP
     WHERE name = 'stations';
END
"""

DELETE_TRIGGER_SQL = """
CREATE TRIGGER station_dataset_version_after_delete
AFTER DELETE ON stations_station
BEGIN
    UPDATE stations_stationdatasetversion
       SET version = version + 1, updated_at = CURRENT_TIMESTAMP
     WHERE name = 'stations';
END
"""

UPDATE_TRIGGER_SQL = """
CREATE TRIGGER station_dataset_version_after_routing_update
AFTER UPDATE OF retail_price, latitude, longitude, geocoding_status ON stations_station
WHEN OLD.retail_price IS NOT NEW.retail_price
  OR OLD.latitude IS NOT NEW.latitude
  OR OLD.longitude IS NOT NEW.longitude
  OR OLD.geocoding_status IS NOT NEW.geocoding_status
BEGIN
    UPDATE stations_stationdatasetversion
       SET version = version + 1, updated_at = CURRENT_TIMESTAMP
     WHERE name = 'stations';
END
"""

def install(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        raise RuntimeError("Station dataset versioning requires SQLite")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(INSERT_SQL)
        cursor.execute(INSERT_TRIGGER_SQL)
        cursor.execute(DELETE_TRIGGER_SQL)
        cursor.execute(UPDATE_TRIGGER_SQL)


def remove(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS station_dataset_version_after_routing_update")
        cursor.execute("DROP TRIGGER IF EXISTS station_dataset_version_after_delete")
        cursor.execute("DROP TRIGGER IF EXISTS station_dataset_version_after_insert")


class Migration(migrations.Migration):
    dependencies = [("stations", "0004_repair_station_triggers")]
    operations = [
        migrations.CreateModel(
            name="StationDatasetVersion",
            fields=[
                ("name", models.CharField(default="stations", max_length=32, primary_key=True, serialize=False)),
                ("version", models.PositiveBigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RunPython(install, remove),
    ]
