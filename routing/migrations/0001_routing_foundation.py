import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("stations", "0005_station_dataset_version")]
    operations = [
        migrations.CreateModel(
            name="AddressGeocodeCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cache_key", models.CharField(max_length=64, unique=True)),
                ("normalized_address", models.TextField()),
                ("city_constraint", models.CharField(blank=True, max_length=128)),
                ("state_constraint", models.CharField(blank=True, max_length=2)),
                ("provider", models.CharField(default="geocode.maps.co", max_length=64)),
                ("provider_response", models.JSONField(default=list)),
                ("decision", models.JSONField(default=dict)),
                ("latitude", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ("longitude", models.DecimalField(blank=True, decimal_places=7, max_digits=11, null=True)),
                ("outcome", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="AddressGeocodingAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cache_key", models.CharField(db_index=True, max_length=64)),
                ("normalized_query", models.TextField()),
                ("attempted_at", models.DateTimeField(auto_now_add=True)),
                ("outcome", models.CharField(max_length=32)),
                ("http_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("candidate_count", models.PositiveIntegerField(default=0)),
                ("error_message", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="RoutingProviderState",
            fields=[
                ("provider", models.CharField(default="osrm", max_length=32, primary_key=True, serialize=False)),
                ("next_request_at", models.DateTimeField(blank=True, null=True)),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="MapResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("cache_key", models.CharField(db_index=True, max_length=128)),
                ("result", models.JSONField(default=dict)),
                ("calculated_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
