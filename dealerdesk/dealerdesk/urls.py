from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("api/", include("catalog.urls")),
    path("api/", include("orders.urls")),
    path("api/", include("supplier.urls")),
    path("api/", include("assistant.urls")),
    path("api/", include("forecasting.urls")),
    path("", include("core.portal_urls")),
]

handler404 = "core.views.not_found"
handler500 = "core.views.server_error"
