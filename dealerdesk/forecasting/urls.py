from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import not_found

from . import services


class ForecastView(APIView):
    def get(self, request, sku):
        forecast = services.forecast_for(sku)
        if forecast is None:
            raise not_found(f"No forecast for SKU {sku}.")
        return Response({"sku": sku, "forecast": forecast})


class ReorderSuggestionsView(APIView):
    def get(self, request):
        return Response({"results": services.reorder_suggestions()})


urlpatterns = [
    path("forecast/<str:sku>/", ForecastView.as_view()),
    path("reorder-suggestions/", ReorderSuggestionsView.as_view()),
]
