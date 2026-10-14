from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

from .sync import run_sync


class SupplierSyncView(APIView):
    def post(self, request):
        return Response(run_sync())


urlpatterns = [path("sync/supplier/", SupplierSyncView.as_view())]
