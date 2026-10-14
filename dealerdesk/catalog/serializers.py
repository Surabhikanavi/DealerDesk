from core.money import fmt


def product_dict(product):
    inventory = getattr(product, "inventory", None)
    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "brand": product.brand,
        "vehicle_fitment": product.vehicle_fitment,
        "price": fmt(product.price),
        "stock": inventory.quantity if inventory else 0,
        "description": product.description,
    }


def dealer_dict(dealer):
    return {
        "id": dealer.id,
        "dealer_code": dealer.dealer_code,
        "name": dealer.name,
        "city": dealer.city,
        "phone": dealer.phone,
        "email": dealer.email,
        "gstin": dealer.gstin,
        "credit_limit": fmt(dealer.credit_limit),
    }
