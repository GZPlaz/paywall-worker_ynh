"""Offers built from the configuration."""

from dataclasses import dataclass
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


def build(cfg):
    plans = []
    if cfg["enable_one_time"]:
        days = cfg["one_time_access_days"]
        plans.append(Plan(
            id="one_time", name=cfg["one_time_name"], mode="payment",
            amount=to_minor(cfg["one_time_price"], cfg),
            access_seconds=days * DAY if days else None,
        ))
    if cfg["enable_subscription"]:
        plans.append(Plan(
            id="subscription", name=cfg["subscription_name"], mode="subscription",
            amount=to_minor(cfg["subscription_price"], cfg), access_seconds=None,
            interval=cfg["subscription_interval"], trial_days=cfg["subscription_trial_days"],
        ))
    if cfg["enable_pass"]:
        plans.append(Plan(
            id="pass", name=cfg["pass_name"], mode="payment",
            amount=to_minor(cfg["pass_price"], cfg), access_seconds=cfg["pass_hours"] * 3600,
        ))
    if cfg["enable_pwyw"]:
        days = cfg["pwyw_access_days"]
        minimum = to_minor(cfg["pwyw_min_price"], cfg)
        plans.append(Plan(
            id="pwyw", name=cfg["pwyw_name"], mode="payment", amount=minimum,
            access_seconds=days * DAY if days else None, custom_amount=True,
            suggested=max(minimum, to_minor(cfg["pwyw_suggested_price"], cfg)),
        ))
    return plans


def get(cfg, plan_id):
    for plan in build(cfg):
        if plan.id == plan_id:
            return plan
    return None


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
