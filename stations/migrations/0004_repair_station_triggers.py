from django.db import migrations


DROP_TRIGGERS = [
    "DROP TRIGGER IF EXISTS station_identity_invalidation",
    "DROP TRIGGER IF EXISTS station_rtree_after_delete",
    "DROP TRIGGER IF EXISTS station_rtree_after_location_update",
    "DROP TRIGGER IF EXISTS station_rtree_after_insert",
]

CREATE_TRIGGERS = [
    """
    CREATE TRIGGER station_rtree_after_insert
    AFTER INSERT ON stations_station
    WHEN NEW.geocoding_status = 'matched'
     AND NEW.latitude BETWEEN -90 AND 90
     AND NEW.longitude BETWEEN -180 AND 180
    BEGIN
        INSERT OR REPLACE INTO station_location_rtree
          (id, min_longitude, max_longitude, min_latitude, max_latitude)
        VALUES (NEW.id, NEW.longitude, NEW.longitude, NEW.latitude, NEW.latitude);
    END
    """,
    """
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
    END
    """,
    """
    CREATE TRIGGER station_rtree_after_delete
    AFTER DELETE ON stations_station
    BEGIN
        DELETE FROM station_location_rtree WHERE id = OLD.id;
    END
    """,
    """
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
               geocoding_decision_summary = '{}',
               updated_at = CURRENT_TIMESTAMP
         WHERE id = NEW.id;
    END
    """,
]


def install_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        raise RuntimeError("Station spatial indexing requires SQLite")
    with schema_editor.connection.cursor() as cursor:
        for statement in DROP_TRIGGERS + CREATE_TRIGGERS:
            cursor.execute(statement)


def remove_triggers(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for statement in DROP_TRIGGERS:
            cursor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [("stations", "0003_geocoding_decisions")]
    operations = [migrations.RunPython(install_triggers, remove_triggers)]
