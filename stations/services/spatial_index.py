from __future__ import annotations

from django.db import connection


class SpatialIndexUnavailable(RuntimeError):
    pass


def verify_rtree_support():
    with connection.cursor() as cursor:
        try:
            cursor.execute("CREATE VIRTUAL TABLE temp.rtree_capability_check USING rtree(id,min_x,max_x,min_y,max_y)")
            cursor.execute("DROP TABLE temp.rtree_capability_check")
        except Exception as exc:
            raise SpatialIndexUnavailable(
                "This SQLite build does not provide the R*Tree module required for station lookup"
            ) from exc


def station_ids_in_bounds(*, min_longitude, max_longitude, min_latitude, max_latitude):
    """Return the R*Tree shortlist; callers perform exact corridor calculations."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM station_location_rtree
            WHERE max_longitude >= %s AND min_longitude <= %s
              AND max_latitude >= %s AND min_latitude <= %s
            ORDER BY id
            """,
            [min_longitude, max_longitude, min_latitude, max_latitude],
        )
        return [row[0] for row in cursor.fetchall()]
