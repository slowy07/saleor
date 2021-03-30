import pytest
from django.db.models import Sum
from django.db.models.functions import Coalesce

from ...core.exceptions import InsufficientStock
from ...checkout.fetch import fetch_checkout_lines
from ..reservations import reserve_stocks
from ..models import Reservation, Stock

COUNTRY_CODE = "US"


def test_reserve_stocks(checkout_line):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.get(product_variant=checkout_line.variant)
    stock.quantity = 10
    stock.save(update_fields=["quantity"])

    lines = fetch_checkout_lines(checkout_line.checkout)

    reserve_stocks(lines, COUNTRY_CODE)

    stock.refresh_from_db()
    assert stock.quantity == 10
    reservation = Reservation.objects.get(checkout_line=checkout_line, stock=stock)
    assert reservation.quantity_reserved == 5
