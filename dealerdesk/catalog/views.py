from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import not_found
from core.pagination import paginate

from . import services
from .models import Dealer, Product
from .serializers import dealer_dict, product_dict


class ProductListView(APIView):
    def get(self, request):
        qs = Product.objects.select_related("inventory").order_by("sku")
        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(sku__icontains=search) | Q(name__icontains=search) | Q(brand__icontains=search))
        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        vehicle = request.query_params.get("vehicle", "").strip()
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle)
        return paginate(request, qs, self, product_dict)

    def post(self, request):
        product = services.create_product(request.data)
        product = Product.objects.select_related("inventory").get(pk=product.pk)
        return Response(product_dict(product), status=201)


class ProductDetailView(APIView):
    def get(self, request, sku):
        product = Product.objects.select_related("inventory").filter(sku=sku).first()
        if product is None:
            raise not_found(f"Product {sku} not found.")
        return Response(product_dict(product))

    def patch(self, request, sku):
        return Response(product_dict(services.update_product(sku, request.data)))


class InventoryView(APIView):
    def put(self, request, sku):
        quantity = services.set_stock(sku, request.data)
        return Response({"sku": sku, "quantity": quantity})


class DealerListView(APIView):
    def get(self, request):
        return paginate(request, Dealer.objects.order_by("id"), self, dealer_dict)

    def post(self, request):
        return Response(dealer_dict(services.create_dealer(request.data)), status=201)


class DealerDetailView(APIView):
    def get(self, request, dealer_id):
        dealer = Dealer.objects.filter(pk=int(dealer_id)).first() if dealer_id.isdigit() and len(dealer_id) < 19 else None
        if dealer is None:
            raise not_found(f"Dealer {dealer_id} not found.")
        return Response(dealer_dict(dealer))
