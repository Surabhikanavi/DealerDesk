from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import not_found, validation_error
from core.pagination import paginate

from . import services
from .models import Order


class OrderListView(APIView):
    def get(self, request):
        qs = Order.objects.order_by("-id")
        dealer_id = request.query_params.get("dealer_id")
        if dealer_id not in (None, ""):
            if not dealer_id.isdigit() or len(dealer_id) > 18:
                raise validation_error("dealer_id must be an integer.")
            qs = qs.filter(dealer_id=int(dealer_id))
        status = request.query_params.get("status")
        if status not in (None, ""):
            status = status.upper()
            if status not in dict(Order.STATUSES):
                raise validation_error(f"status must be one of {', '.join(dict(Order.STATUSES))}.")
            qs = qs.filter(status=status)
        return paginate(request, qs, self, services.order_dict)

    def post(self, request):
        order, created = services.create_draft_order(request.data, request.headers.get("Idempotency-Key"))
        return Response(services.order_dict(order), status=201 if created else 200)


class OrderDetailView(APIView):
    def get(self, request, order_number):
        order = Order.objects.filter(order_number=order_number).first()
        if order is None:
            raise not_found(f"Order {order_number} not found.")
        return Response(services.order_dict(order))

    def put(self, request, order_number):
        return Response(services.order_dict(services.replace_items(order_number, request.data)))


class OrderActionView(APIView):
    action_name = None

    def post(self, request, order_number):
        if self.action_name == "confirm":
            order = services.confirm(order_number)
        elif self.action_name == "cancel":
            order = services.cancel(order_number)
        else:
            order = services.simple_transition(order_number, self.action_name)
        return Response(services.order_dict(order))
