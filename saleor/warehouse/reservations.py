from collections import defaultdict, namedtuple
from datetime import timedelta
from typing import TYPE_CHECKING, Dict, Iterable, List, cast

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..core.exceptions import AllocationError, InsufficientStock, InsufficientStockData
from ..product.models import ProductVariant
from .models import Allocation, Reservation, Stock, Warehouse

if TYPE_CHECKING:
    from ..checkout.models import CheckoutLine


RESERVATION_TTL = timedelta(seconds=15 * 60)

StockData = namedtuple("StockData", ["pk", "quantity"])


@transaction.atomic
def reserve_stocks(
    checkout_lines: Iterable["CheckoutLine"],
    country_code: str,
    channel_slug: str,
    *,
    replace=True,
):
    """Reserve stocks for given `checkout_lines` in given country."""
    # Reservation is only applied to checkout lines with variants with track inventory
    # set to True
    checkout_lines = get_checkout_lines_to_reserve(checkout_lines)
    if not checkout_lines:
        return

    variants = [line.variant for line in checkout_lines]

    stocks = list(
        Stock.objects.select_for_update(of=("self",))
        .get_variants_stocks_for_country(country_code, channel_slug, variants)
        .order_by("pk")
        .values("id", "product_variant", "pk", "quantity")
    )
    stocks_id = (stock.pop("id") for stock in stocks)

    insufficient_stock: List[InsufficientStockData] = []
    reservations: List[Reservation] = []

    quantity_allocation_list = list(
        Allocation.objects.filter(
            stock_id__in=stocks_id,
            quantity_allocated__gt=0,
        )
        .values("stock")
        .annotate(quantity_allocated_sum=Sum("quantity_allocated"))
    )
    quantity_allocation_for_stocks: Dict = defaultdict(int)
    for allocation in quantity_allocation_list:
        quantity_allocation_for_stocks[allocation["stock"]] += allocation[
            "quantity_allocated_sum"
        ]

    quantity_reservation_list = list(
        Reservation.objects.filter(
            stock_id__in=stocks_id,
            quantity_reserved__gt=0,
        )
        .not_expired()
        .exclude_checkout_lines(checkout_lines)
        .values("stock")
        .annotate(quantity_reserved_sum=Sum("quantity_reserved"))
    )
    quantity_reservation_for_stocks: Dict = defaultdict(int)
    for reservation in quantity_reservation_list:
        quantity_reservation_for_stocks[allocation["stock"]] += reservation[
            "quantity_reserved_sum"
        ]

    variant_to_stocks: Dict[str, List[StockData]] = defaultdict(list)
    for stock_data in stocks:
        variant = stock_data.pop("product_variant")
        variant_to_stocks[variant].append(StockData(**stock_data))

    insufficient_stock: List[InsufficientStockData] = []
    reservations: List[Reservation] = []
    for line in checkout_lines:
        line.variant = cast(ProductVariant, line.variant)
        stock_reservations = variant_to_stocks[line.variant.pk]
        insufficient_stock, allocation_items = _create_reservations(
            line,
            stock_reservations,
            quantity_allocation_for_stocks,
            quantity_reservation_for_stocks,
            insufficient_stock,
        )
        reservations.extend(allocation_items)

    if insufficient_stock:
        raise InsufficientStock(insufficient_stock)

    if reservations:
        if replace:
            Reservation.objects.filter(checkout_line__in=checkout_lines).delete()
        Reservation.objects.bulk_create(reservations)


def _create_reservations(
    line: "CheckoutLine",
    stocks: List[StockData],
    quantity_allocation_for_stocks: dict,
    quantity_reservation_for_stocks: dict,
    insufficient_stock: List[InsufficientStockData],
):
    quantity = line.quantity
    quantity_reserved = 0
    reservations = []
    for stock_data in stocks:
        quantity_allocated_in_stock = quantity_allocation_for_stocks.get(
            stock_data.pk, 0
        )
        quantity_reserved_in_stock = quantity_reservation_for_stocks.get(
            stock_data.pk, 0
        )

        quantity_available_in_stock = max(
            stock_data.quantity
            - quantity_allocated_in_stock
            - quantity_reserved_in_stock,
            0,
        )

        quantity_to_reserve = min(
            (quantity - quantity_reserved), quantity_available_in_stock
        )
        if quantity_to_reserve > 0:
            reservations.append(
                Reservation(
                    checkout_line=line,
                    stock_id=stock_data.pk,
                    quantity_reserved=quantity_to_reserve,
                    reserved_until=_get_expiration_datetime(),
                )
            )

            quantity_reserved += quantity_to_reserve
            if quantity_reserved == quantity:
                return insufficient_stock, reservations

    if not quantity_reserved == quantity:
        insufficient_stock.append(
            InsufficientStockData(
                variant=line.variant, checkout_line=line  # type: ignore
            )
        )
        return insufficient_stock, []

    return [], []


def get_checkout_lines_to_reserve(
    checkout_lines: Iterable["CheckoutLine"],
) -> Iterable["CheckoutLine"]:
    """Return order lines which can be reserved."""
    valid_lines = []
    for line in checkout_lines:
        if line.quantity and line.variant and line.variant.track_inventory:
            valid_lines.append(line)
    return valid_lines


def _get_expiration_datetime():
    return timezone.now() + RESERVATION_TTL
