import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="JobLease",
            fields=[
                ("name", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("owner_token", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="ProviderState",
            fields=[
                ("provider", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("remaining_initial_allowance", models.PositiveIntegerField(default=0)),
                ("next_request_at", models.DateTimeField(blank=True, null=True)),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="Station",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_truckstop_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("address", models.CharField(max_length=255)),
                ("city", models.CharField(max_length=128)),
                ("state", models.CharField(max_length=2)),
                ("rack_id", models.CharField(max_length=64)),
                ("retail_price", models.DecimalField(decimal_places=8, max_digits=14)),
                ("latitude", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True, validators=[django.core.validators.MinValueValidator(-90), django.core.validators.MaxValueValidator(90)])),
                ("longitude", models.DecimalField(blank=True, decimal_places=7, max_digits=11, null=True, validators=[django.core.validators.MinValueValidator(-180), django.core.validators.MaxValueValidator(180)])),
                ("geocoding_status", models.CharField(choices=[("pending", "Pending"), ("matched", "Matched"), ("ambiguous", "Ambiguous"), ("unresolved", "Unresolved"), ("failed", "Failed")], db_index=True, default="pending", max_length=16)),
                ("coordinate_source", models.CharField(blank=True, max_length=64)),
                ("geocoded_at", models.DateTimeField(blank=True, null=True)),
                ("requires_review", models.BooleanField(db_index=True, default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["state", "city"], name="station_state_city_idx")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("retail_price__gt", 0)), name="station_positive_price"),
                    models.CheckConstraint(condition=models.Q(models.Q(("latitude__isnull", True), ("longitude__isnull", True)), models.Q(("latitude__isnull", False), ("longitude__isnull", False)), _connector="OR"), name="station_coordinate_pair"),
                    models.CheckConstraint(condition=models.Q(("latitude__isnull", True), models.Q(("latitude__gte", -90), ("latitude__lte", 90)), _connector="OR"), name="station_latitude_range"),
                    models.CheckConstraint(condition=models.Q(("longitude__isnull", True), models.Q(("longitude__gte", -180), ("longitude__lte", 180)), _connector="OR"), name="station_longitude_range"),
                    models.CheckConstraint(condition=models.Q(models.Q(("geocoding_status", "matched"), _negated=True), models.Q(("latitude__isnull", False), ("longitude__isnull", False)), _connector="OR"), name="station_matched_has_coordinates"),
                ],
            },
        ),
        migrations.CreateModel(
            name="SourceRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fingerprint", models.CharField(max_length=64, unique=True)),
                ("source_truckstop_id", models.CharField(db_index=True, max_length=64)),
                ("truckstop_name", models.CharField(max_length=255)),
                ("address", models.CharField(max_length=255)),
                ("city", models.CharField(max_length=128)),
                ("state", models.CharField(max_length=64)),
                ("rack_id", models.CharField(max_length=64)),
                ("retail_price", models.DecimalField(decimal_places=8, max_digits=14)),
                ("raw_values", models.JSONField(default=dict)),
                ("is_conflicting", models.BooleanField(db_index=True, default=False)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                ("station", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_records", to="stations.station")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="GeocodingQueryCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=64)),
                ("identity_fingerprint", models.CharField(max_length=64)),
                ("normalized_query", models.TextField()),
                ("response", models.JSONField()),
                ("fetched_at", models.DateTimeField(auto_now=True)),
                ("station", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="geocoding_cache_entries", to="stations.station")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("station", "provider", "identity_fingerprint", "normalized_query"), name="unique_station_geocode_query")]},
        ),
        migrations.CreateModel(
            name="GeocodingAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=64)),
                ("normalized_query", models.TextField()),
                ("attempted_at", models.DateTimeField(auto_now_add=True)),
                ("outcome", models.CharField(choices=[("response", "Response"), ("rate_limited", "Rate limited"), ("transient_error", "Transient error"), ("access_error", "Access error"), ("malformed", "Malformed response")], max_length=32)),
                ("http_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("candidate_details", models.JSONField(default=list)),
                ("decision_reasons", models.JSONField(default=list)),
                ("error_message", models.TextField(blank=True)),
                ("station", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="geocoding_attempts", to="stations.station")),
            ],
            options={"indexes": [models.Index(fields=["station", "attempted_at"], name="geocode_attempt_idx")]},
        ),
    ]
