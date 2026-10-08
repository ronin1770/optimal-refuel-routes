from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.exceptions import ProviderError
from integrations.osrm import OSRMNoRoute
from routing.models import MapResult
from routing.serializers import RouteRequestSerializer
from routing.services.endpoint_resolution import EndpointProviderError, EndpointValidationError
from routing.services.planner import JourneyPlanningError, plan_journey


class RouteView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = plan_journey(serializer.validated_data)
        except EndpointValidationError as exc:
            body = {"error": str(exc)}
            if exc.candidates:
                body["candidates"] = exc.candidates
            return Response(body, status=status.HTTP_400_BAD_REQUEST)
        except EndpointProviderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except JourneyPlanningError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except OSRMNoRoute as exc:
            return Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except ProviderError as exc:
            return Response(
                {"error": f"routing provider is temporarily unavailable: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        result["map_url"] = request.build_absolute_uri(result["map_url"])
        return Response(result)


def saved_map(request, token):
    result = get_object_or_404(MapResult, token=token)
    if result.expires_at <= timezone.now():
        return HttpResponse("This saved route map has expired.", status=410, content_type="text/plain")
    return render(request, "routing/map.html", {
        "route_result": result.result,
        "tile_url": settings.MAP_TILE_URL,
        "tile_attribution": settings.MAP_TILE_ATTRIBUTION,
        "calculated_at": result.calculated_at,
    })
