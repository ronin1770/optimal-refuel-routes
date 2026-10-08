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


def station_ids_in_boxes(boxes, *, batch_size=100):
    """Return distinct R*Tree candidates for many segment bounding boxes."""
    identifiers = set()
    boxes = list(boxes)
    with connection.cursor() as cursor:
        for offset in range(0, len(boxes), batch_size):
            batch = boxes[offset:offset + batch_size]
            conditions = []
            parameters = []
            for min_longitude, max_longitude, min_latitude, max_latitude in batch:
                conditions.append(
                    "(max_longitude >= %s AND min_longitude <= %s "
                    "AND max_latitude >= %s AND min_latitude <= %s)"
                )
                parameters.extend([min_longitude, max_longitude, min_latitude, max_latitude])
            cursor.execute(
                "SELECT DISTINCT id FROM station_location_rtree WHERE " + " OR ".join(conditions),
                parameters,
            )
            identifiers.update(row[0] for row in cursor.fetchall())
    return sorted(identifiers)
