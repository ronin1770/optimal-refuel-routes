from rest_framework import serializers


class EndpointSerializer(serializers.Serializer):
    address = serializers.CharField(required=False, allow_blank=False, max_length=500)
    city = serializers.CharField(required=False, allow_blank=False, max_length=128)
    state = serializers.CharField(required=False, allow_blank=False, max_length=64)
    latitude = serializers.FloatField(required=False, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, min_value=-180, max_value=180)

    def validate(self, attrs):
        has_address = "address" in attrs
        has_latitude = "latitude" in attrs
        has_longitude = "longitude" in attrs
        if has_address and (has_latitude or has_longitude):
            raise serializers.ValidationError("address cannot be combined with coordinates")
        if has_latitude != has_longitude:
            raise serializers.ValidationError("latitude and longitude must be supplied together")
        if not has_address and not (has_latitude and has_longitude):
            raise serializers.ValidationError("provide either address or latitude and longitude")
        if not has_address and ("city" in attrs or "state" in attrs):
            raise serializers.ValidationError("city and state constraints apply only to addresses")
        return attrs


class RouteRequestSerializer(serializers.Serializer):
    start = EndpointSerializer()
    finish = EndpointSerializer()
    initial_fuel_gallons = serializers.FloatField(default=50.0, min_value=0, max_value=50)
    verify_detours = serializers.BooleanField(default=True)
