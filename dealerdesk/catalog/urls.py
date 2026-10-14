from django.urls import path

from . import views

urlpatterns = [
    path("products/", views.ProductListView.as_view()),
    path("products/<str:sku>/", views.ProductDetailView.as_view()),
    path("inventory/<str:sku>/", views.InventoryView.as_view()),
    path("dealers/", views.DealerListView.as_view()),
    path("dealers/<str:dealer_id>/", views.DealerDetailView.as_view()),
]
