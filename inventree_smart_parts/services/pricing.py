"""Normalize SmartParts supplier prices using InvenTree's stored rates."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Optional


class PricingError(ValueError):
    """A price or exchange rate cannot be used."""


@dataclass(frozen=True)
class ExchangeRate:
    eur_per_usd: Decimal
    fetched_at: datetime


def get_usd_to_eur_rate() -> ExchangeRate:
    """Read the amounts and base currency together; no distributor-time FX calls."""
    from djmoney.contrib.exchange.models import Rate, get_default_backend_name

    rows = list(
        Rate.objects.filter(
            backend_id=get_default_backend_name(), currency__in=("USD", "EUR")
        ).select_related("backend")
    )
    if rows:
        backend = rows[0].backend
        rates = {row.currency: row.value for row in rows}
        rates[backend.base_currency] = Decimal(1)
        usd, eur = rates.get("USD"), rates.get("EUR")
        if (
            usd is not None
            and eur is not None
            and all(value.is_finite() and value > 0 for value in (usd, eur))
        ):
            return ExchangeRate(eur / usd, backend.last_update)
    raise PricingError("No usable USD → EUR exchange rate. Refresh rates in InvenTree.")


def normalize_price(amount, currency, rate: Optional[ExchangeRate] = None):
    """Return a Decimal amount and ISO currency; convert USD exactly once."""
    try:
        amount = Decimal(str(amount))
    except DecimalException as exc:
        raise PricingError("Invalid supplier unit price") from exc
    if not amount.is_finite() or amount < 0:
        raise PricingError("Invalid supplier unit price")

    currency = str(currency or "").strip().upper()
    if currency != "USD":
        return amount, currency
    rate = rate or get_usd_to_eur_rate()
    try:
        return (amount * rate.eur_per_usd).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        ), "EUR"
    except DecimalException as exc:
        raise PricingError(
            "Converted supplier unit price exceeds the supported precision"
        ) from exc


class PriceSnapshot:
    """Lazily reuse one FX rate (or one missing-rate result) across a batch of tiers."""

    def __init__(self):
        self.rate = None
        self.error = None

    def normalize(self, amount, currency):
        if str(currency or "").strip().upper() == "USD" and self.rate is None:
            if self.error is None:
                try:
                    self.rate = get_usd_to_eur_rate()
                except PricingError as exc:
                    self.error = str(exc)
            if self.error:
                raise PricingError(self.error)
        return normalize_price(amount, currency, self.rate)


def present_price_breaks(rows, snapshot):
    """Keep the source price visible with a warning when it cannot be converted."""
    prices, warnings = [], set()
    for row in rows:
        try:
            amount, currency = snapshot.normalize(row["price"], row["currency"])
        except PricingError as exc:
            warnings.add(str(exc))
            prices.append(row)
        else:
            prices.append({**row, "price": format(amount, "f"), "currency": currency})
    return prices, sorted(warnings)
