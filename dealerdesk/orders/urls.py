from django.urls import path

from . import views

urlpatterns = [
    path("orders/", views.OrderListView.as_view()),
    path("orders/<str:order_number>/", views.OrderDetailView.as_view()),
] + [
    path(f"orders/<str:order_number>/{action}/", views.OrderActionView.as_view(action_name=action))
    for action in ("confirm", "dispatch", "deliver", "cancel")
]
