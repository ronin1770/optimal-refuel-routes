from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class Station(models.Model):
    class GeocodingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        MATCHED = "matched", "Matched"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        UNRESOLVED = "unresolved", "Unresolved"
        FAILED = "failed", "Failed"

    source_truckstop_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.CharField(max_length=64)
    retail_price = models.DecimalField(max_digits=14, decimal_places=8)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.DecimalField(max_digits=11, decimal_places=7, null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)])
    geocoding_status = models.CharField(max_length=16, choices=GeocodingStatus.choices, default=GeocodingStatus.PENDING, db_index=True)
    coordinate_source = models.CharField(max_length=64, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)
    geocoding_decision_summary = models.JSONField(default=dict)
    requires_review = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "city"], name="station_state_city_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(retail_price__gt=0), name="station_positive_price"),
            models.CheckConstraint(condition=Q(latitude__isnull=True, longitude__isnull=True) | Q(latitude__isnull=False, longitude__isnull=False), name="station_coordinate_pair"),
            models.CheckConstraint(condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90), name="station_latitude_range"),
            models.CheckConstraint(condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180), name="station_longitude_range"),
            models.CheckConstraint(condition=~Q(geocoding_status="matched") | Q(latitude__isnull=False, longitude__isnull=False), name="station_matched_has_coordinates"),
        ]

    def __str__(self):
        return f"{self.source_truckstop_id}: {self.name}"


class SourceRecord(models.Model):
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name="source_records")
    fingerprint = models.CharField(max_length=64, unique=True)
    source_truckstop_id = models.CharField(max_length=64, db_index=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=64)
    rack_id = models.CharField(max_length=64)
    retail_price = models.DecimalField(max_digits=14, decimal_places=8)
    raw_values = models.JSONField(default=dict)
    is_conflicting = models.BooleanField(default=False, db_index=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class GeocodingQueryCache(models.Model):
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name="geocoding_cache_entries")
    provider = models.CharField(max_length=64)
    identity_fingerprint = models.CharField(max_length=64)
    normalized_query = models.TextField()
    response = models.JSONField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["station", "provider", "identity_fingerprint", "normalized_query"], name="unique_station_geocode_query")]


class GeocodingAttempt(models.Model):
    class Outcome(models.TextChoices):
        RESPONSE = "response", "Response"
        RATE_LIMITED = "rate_limited", "Rate limited"
        TRANSIENT_ERROR = "transient_error", "Transient error"
        ACCESS_ERROR = "access_error", "Access error"
        MALFORMED = "malformed", "Malformed response"

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name="geocoding_attempts")
    provider = models.CharField(max_length=64)
    normalized_query = models.TextField()
    attempted_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=32, choices=Outcome.choices)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    candidate_details = models.JSONField(default=list)
    candidate_decisions = models.JSONField(default=list)
    decision_reasons = models.JSONField(default=list)
    error_message = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["station", "attempted_at"], name="geocode_attempt_idx")]


class ProviderState(models.Model):
    provider = models.CharField(max_length=64, primary_key=True)
    remaining_initial_allowance = models.PositiveIntegerField(default=0)
    next_request_at = models.DateTimeField(null=True, blank=True)
    not_before = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class JobLease(models.Model):
    name = models.CharField(max_length=64, primary_key=True)
    owner_token = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)


class StationDatasetVersion(models.Model):
    name = models.CharField(max_length=32, primary_key=True, default="stations")
    version = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
