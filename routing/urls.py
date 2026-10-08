from django.urls import path

from routing.views import RouteView, saved_map


urlpatterns = [
    path("api/route/", RouteView.as_view(), name="route"),
    path("maps/<uuid:token>/", saved_map, name="saved-map"),
]
