from django.contrib import admin

from .models import GeocodingAttempt, SourceRecord, Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("source_truckstop_id", "name", "city", "state", "retail_price", "geocoding_status", "requires_review")
    list_filter = ("state", "geocoding_status", "requires_review")
    search_fields = ("source_truckstop_id", "name", "address", "city")


admin.site.register(SourceRecord)
admin.site.register(GeocodingAttempt)
