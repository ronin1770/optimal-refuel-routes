import sqlite3

from django.db import migrations


FORWARD_SQL = """
CREATE VIRTUAL TABLE station_location_rtree USING rtree(
    id,
    min_longitude, max_longitude,
    min_latitude, max_latitude
);

INSERT INTO station_location_rtree(id, min_longitude, max_longitude, min_latitude, max_latitude)
SELECT id, longitude, longitude, latitude, latitude
FROM stations_station
WHERE geocoding_status = 'matched'
  AND latitude BETWEEN -90 AND 90
  AND longitude BETWEEN -180 AND 180;

CREATE TRIGGER station_rtree_after_insert
AFTER INSERT ON stations_station
WHEN NEW.geocoding_status = 'matched'
 AND NEW.latitude BETWEEN -90 AND 90
 AND NEW.longitude BETWEEN -180 AND 180
BEGIN
    INSERT OR REPLACE INTO station_location_rtree
      (id, min_longitude, max_longitude, min_latitude, max_latitude)
    VALUES (NEW.id, NEW.longitude, NEW.longitude, NEW.latitude, NEW.latitude);
END;

CREATE TRIGGER station_rtree_after_location_update
AFTER UPDATE OF latitude, longitude, geocoding_status ON stations_station
BEGIN
    DELETE FROM station_location_rtree WHERE id = OLD.id;
    INSERT OR REPLACE INTO station_location_rtree
      (id, min_longitude, max_longitude, min_latitude, max_latitude)
    SELECT NEW.id, NEW.longitude, NEW.longitude, NEW.latitude, NEW.latitude
    WHERE NEW.geocoding_status = 'matched'
      AND NEW.latitude BETWEEN -90 AND 90
      AND NEW.longitude BETWEEN -180 AND 180;
END;

CREATE TRIGGER station_rtree_after_delete
AFTER DELETE ON stations_station
BEGIN
    DELETE FROM station_location_rtree WHERE id = OLD.id;
END;

CREATE TRIGGER station_identity_invalidation
AFTER UPDATE OF name, address, city, state ON stations_station
WHEN OLD.name <> NEW.name OR OLD.address <> NEW.address
  OR OLD.city <> NEW.city OR OLD.state <> NEW.state
BEGIN
    DELETE FROM station_location_rtree WHERE id = NEW.id;
    UPDATE stations_station
       SET latitude = NULL,
           longitude = NULL,
           geocoding_status = 'pending',
           coordinate_source = '',
           geocoded_at = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS django_cache (
    cache_key varchar(255) NOT NULL PRIMARY KEY,
    value text NOT NULL,
    expires datetime NOT NULL
);
CREATE INDEX IF NOT EXISTS django_cache_expires ON django_cache(expires);
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS station_identity_invalidation;
DROP TRIGGER IF EXISTS station_rtree_after_delete;
DROP TRIGGER IF EXISTS station_rtree_after_location_update;
DROP TRIGGER IF EXISTS station_rtree_after_insert;
DROP TABLE IF EXISTS station_location_rtree;
DROP INDEX IF EXISTS django_cache_expires;
DROP TABLE IF EXISTS django_cache;
"""


def _statements(script):
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ""
    if statement.strip():
        yield statement.strip()


def create_spatial_index(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        raise RuntimeError("Station spatial indexing requires SQLite with R*Tree support")
    try:
        with schema_editor.connection.cursor() as cursor:
            for statement in _statements(FORWARD_SQL):
                cursor.execute(statement)
    except Exception as exc:
        raise RuntimeError(
            "Unable to create station R*Tree index; verify that SQLite R*Tree support is enabled"
        ) from exc


def remove_spatial_index(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for statement in _statements(REVERSE_SQL):
            cursor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [("stations", "0001_station_foundation")]
    operations = [migrations.RunPython(create_spatial_index, remove_spatial_index)]
