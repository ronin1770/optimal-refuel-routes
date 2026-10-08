import uuid

from django.db import models


class AddressGeocodeCache(models.Model):
    cache_key = models.CharField(max_length=64, unique=True)
    normalized_address = models.TextField()
    city_constraint = models.CharField(max_length=128, blank=True)
    state_constraint = models.CharField(max_length=2, blank=True)
    provider = models.CharField(max_length=64, default="geocode.maps.co")
    provider_response = models.JSONField(default=list)
    decision = models.JSONField(default=dict)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=7, null=True, blank=True)
    outcome = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AddressGeocodingAttempt(models.Model):
    cache_key = models.CharField(max_length=64, db_index=True)
    normalized_query = models.TextField()
    attempted_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=32)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    candidate_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)


class RoutingProviderState(models.Model):
    provider = models.CharField(max_length=32, primary_key=True, default="osrm")
    next_request_at = models.DateTimeField(null=True, blank=True)
    not_before = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class MapResult(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cache_key = models.CharField(max_length=128, db_index=True)
    result = models.JSONField(default=dict)
    calculated_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
