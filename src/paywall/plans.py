"""Offer value objects and price formatting."""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

DAY = 86400

CURRENCY_SYMBOLS = {
    "usd": "$", "eur": "€", "gbp": "£", "cad": "CA$", "aud": "A$", "nzd": "NZ$",
    "chf": "CHF ", "jpy": "¥", "krw": "₩", "inr": "₹", "brl": "R$", "mxn": "MX$",
    "sgd": "S$", "hkd": "HK$",
}


@dataclass
class Plan:
    id: str
    name: str
    mode: str                  # "payment" or "subscription" (Stripe Checkout mode)
    amount: int                # smallest currency unit; minimum for pay-what-you-want
    access_seconds: int | None # None = forever (ignored for subscriptions)
    interval: str | None = None
    trial_days: int = 0
    suggested: int | None = None
    custom_amount: bool = False
    kind: str = "one_time"     # one_time | subscription | pass | pwyw
    site_ids: list = field(default_factory=list)
    active: bool = True
    position: int = 0

    @property
    def is_subscription(self):
        return self.mode == "subscription"


def to_minor(price, cfg):
    factor = 1 if cfg.zero_decimal else 100
    return int((Decimal(str(price)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_money(amount, cfg):
    currency = cfg["currency"]
    if cfg.zero_decimal:
        number = f"{amount:,}"
    else:
        number = f"{amount / 100:,.2f}"
        number = number.removesuffix(".00")
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{number}" if symbol else f"{number} {currency.upper()}"


def describe_duration(seconds):
    if seconds is None:
        return "forever"
    if seconds % DAY == 0:
        days = seconds // DAY
        if days % 365 == 0:
            years = days // 365
            return f"{years} year" + ("s" if years > 1 else "")
        if days % 30 == 0:
            months = days // 30
            return f"{months} month" + ("s" if months > 1 else "")
        return f"{days} day" + ("s" if days > 1 else "")
    hours = max(1, seconds // 3600)
    return f"{hours} hour" + ("s" if hours > 1 else "")


def describe(plan, cfg):
    """Short human readable description of what the plan gives."""
    if plan.is_subscription:
        text = f"Billed every {plan.interval}, cancel anytime"
        if plan.trial_days:
            text = f"{plan.trial_days}-day free trial, then billed every {plan.interval}"
        return text
    if plan.access_seconds is None:
        return "One-time payment, access forever"
    return f"One-time payment, {describe_duration(plan.access_seconds)} of access"


def price_label(plan, cfg, amount=None):
    amount = plan.amount if amount is None else amount
    label = format_money(amount, cfg)
    if plan.custom_amount:
        return f"from {label}"
    if plan.is_subscription:
        return f"{label} / {plan.interval}"
    return label
