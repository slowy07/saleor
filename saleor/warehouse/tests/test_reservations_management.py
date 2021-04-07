from datetime import timedelta

import pytest
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from ...core.exceptions import InsufficientStock
from ..reservations import reserve_stocks
from ..models import Reservation, Stock, Warehouse

COUNTRY_CODE = "US"


def test_reserve_stocks(checkout_line):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.get(product_variant=checkout_line.variant)
    stock.quantity = 10
    stock.save(update_fields=["quantity"])

    reserve_stocks([checkout_line], COUNTRY_CODE)

    stock.refresh_from_db()
    assert stock.quantity == 10
    reservation = Reservation.objects.get(checkout_line=checkout_line, stock=stock)
    assert reservation.quantity_reserved == 5
    assert reservation.reserved_until > timezone.now() + timedelta(minutes=1)


def test_multiple_stocks_are_reserved_if_single_stock_is_not_enough(
    checkout_line, warehouse, shipping_zone
):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.get(product_variant=checkout_line.variant)
    stock.quantity = 3
    stock.save(update_fields=["quantity"])

    secondary_warehouse = Warehouse.objects.create(
        address=warehouse.address,
        name="Warehouse 2",
        slug="warehouse-2",
        email=warehouse.email,
    )
    secondary_warehouse.shipping_zones.add(shipping_zone)
    secondary_warehouse.save()

    secondary_stock = Stock.objects.create(
        warehouse=secondary_warehouse, product_variant=stock.product_variant, quantity=3
    )

    reserve_stocks([checkout_line], COUNTRY_CODE)

    stock.refresh_from_db()
    assert stock.quantity == 3

    reservation = Reservation.objects.get(checkout_line=checkout_line, stock=stock)
    assert reservation.quantity_reserved == 3
    assert reservation.reserved_until > timezone.now() + timedelta(minutes=1)

    second_reservation = Reservation.objects.get(
        checkout_line=checkout_line, stock=secondary_stock
    )
    assert second_reservation.quantity_reserved == 2
    assert second_reservation.reserved_until > timezone.now() + timedelta(minutes=1)


def test_stocks_reservation_removes_previous_reservations_for_checkout(checkout_line):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.get(product_variant=checkout_line.variant)
    stock.quantity = 10
    stock.save(update_fields=["quantity"])

    previous_reservation = Reservation.objects.create(
        checkout_line=checkout_line,
        stock=stock,
        quantity_reserved=5,
        reserved_until=timezone.now() + timedelta(hours=1),
    )

    reserve_stocks([checkout_line], COUNTRY_CODE)

    with pytest.raises(Reservation.DoesNotExist):
        previous_reservation.refresh_from_db()


def test_stock_reservation_fails_if_there_is_not_enough_stock_available(checkout_line):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.get(product_variant=checkout_line.variant)
    stock.quantity = 3
    stock.save(update_fields=["quantity"])

    with pytest.raises(InsufficientStock):
        reserve_stocks([checkout_line], COUNTRY_CODE)


def test_stock_reservation_fails_if_there_is_no_stock(checkout_line):
    checkout_line.quantity = 5
    checkout_line.save()

    stock = Stock.objects.all().delete()

    with pytest.raises(InsufficientStock):
        reserve_stocks([checkout_line], COUNTRY_CODE)
