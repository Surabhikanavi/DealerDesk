"""Money helpers. Money is always Decimal, never float, and leaves the API as a 2-decimal string."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISA = Decimal("0.01")
GST_RATE = Decimal("0.18")
MAX_PRICE = Decimal("9999999999.99")  # fits DecimalField(max_digits=12, decimal_places=2)


def fmt(amount):
    """Decimal -> "1459.50". Amounts are already exact to the paisa; quantize only fixes the exponent."""
    return str(Decimal(amount).quantize(PAISA))


def gst(line_subtotal):
    """18% GST on one line, rounded half-up to the paisa (contract 7.4)."""
    return (line_subtotal * GST_RATE).quantize(PAISA, rounding=ROUND_HALF_UP)


def parse_price(value):
    """A positive amount with at most 2 decimals, given as a JSON string or number. Returns Decimal or None.

    Floats are converted through str() so 12.34 stays 12.34 rather than its binary approximation.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount > MAX_PRICE:
        return None
    if amount.as_tuple().exponent < -2:  # "12.345" has 3 decimals -> invalid
        return None
    return amount.quantize(PAISA)


def parse_non_negative_amount(value):
    """Like parse_price but 0 is allowed (credit limits)."""
    if value in (0, "0", "0.00", "0.0"):
        return Decimal("0.00")
    return parse_price(value)
