"""Checkout, fulfillment and Stripe webhook handling."""

import logging

from . import access, codes, plans
from .stripe_client import Stripe, StripeError, subscription_period_end

log = logging.getLogger(__name__)

ACTIVE_SUB_STATUSES = {"active", "trialing", "past_due"}
ENDED_SUB_STATUSES = {"canceled", "unpaid", "incomplete_expired", "paused"}


class PaymentError(Exception):
    pass


def client(cfg):
    return Stripe(cfg["stripe_secret_key"])


def stripe_coupon(db, cfg, code_row, stripe=None):
    """Return a Stripe coupon id mirroring ``code_row`` (created lazily and
    re-created when the code or the currency changes)."""
    key = f"{code_row['kind']}:{code_row['value']}:{cfg['currency']}:{code_row['duration']}:{code_row['duration_months']}"
    if code_row["stripe_coupon_id"] and code_row["stripe_coupon_key"] == key:
        return code_row["stripe_coupon_id"]
    params = {
        "name": code_row["code"][:40],
        "duration": code_row["duration"],
        "metadata": {"paywall_instance": cfg.instance_id, "paywall_code": code_row["code"]},
    }
    if code_row["duration"] == "repeating":
        params["duration_in_months"] = code_row["duration_months"]
    if code_row["kind"] == "percent":
        params["percent_off"] = code_row["value"]
    else:
        params["amount_off"] = code_row["value"]
        params["currency"] = cfg["currency"]
    coupon = (stripe or client(cfg)).create_coupon(params)
    db.execute(
        "UPDATE codes SET stripe_coupon_id = ?, stripe_coupon_key = ? WHERE code = ?",
        (coupon["id"], key, code_row["code"]),
    )
    return coupon["id"]


def create_checkout(db, cfg, plan, customer, email=None, code_row=None, custom_amount=None, return_url=None,
                    stripe=None):
    """Create a Stripe Checkout session and return its URL."""
    if not cfg.stripe_ready:
        raise PaymentError("Online payments are not configured yet.")
    stripe = stripe or client(cfg)
    amount = plan.amount
    if plan.custom_amount:
        amount = max(plan.amount, int(custom_amount or 0))
        code_row = None  # discounts make no sense when you choose the price
    metadata = {
        "paywall_instance": cfg.instance_id,
        "plan": plan.id,
        "paywall_customer": customer["id"] if customer else "",
        "code": code_row["code"] if code_row else "",
        "access_seconds": "" if plan.access_seconds is None else str(plan.access_seconds),
        "return_url": return_url or "",
    }
    price_data = {
        "currency": cfg["currency"],
        "unit_amount": amount,
        "product_data": {"name": f"{cfg['paywall_title']} - {plan.name}"[:250]},
    }
    if plan.is_subscription:
        price_data["recurring"] = {"interval": plan.interval}
    params = {
        "mode": plan.mode,
        "line_items": [{"price_data": price_data, "quantity": 1}],
        "success_url": f"{cfg.public_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{cfg.public_url}/",
        "metadata": metadata,
    }
    if customer:
        params["client_reference_id"] = customer["id"]
    if customer and customer["stripe_customer_id"]:
        params["customer"] = customer["stripe_customer_id"]
    elif email or (customer and customer["email"]):
        params["customer_email"] = email or customer["email"]
    if plan.is_subscription:
        params["subscription_data"] = {"metadata": metadata}
        if plan.trial_days:
            params["subscription_data"]["trial_period_days"] = plan.trial_days
    else:
        params["customer_creation"] = "always"
        params["payment_intent_data"] = {"metadata": metadata}
    if code_row and code_row["kind"] in ("percent", "fixed"):
        params["discounts"] = [{"coupon": stripe_coupon(db, cfg, code_row, stripe)}]
    try:
        session = stripe.create_checkout_session(params)
    except StripeError as exc:
        raise PaymentError(str(exc)) from exc
    return session["url"]


def fulfill_checkout(db, cfg, session, stripe=None):
    """Grant access for a completed Checkout session. Safe to call several
    times (success page + webhook). Returns the customer id or None."""
    metadata = session.get("metadata") or {}
    if metadata.get("paywall_instance") != cfg.instance_id:
        return None
    if session.get("status") != "complete" or session.get("payment_status") not in ("paid", "no_payment_required"):
        return None
    details = session.get("customer_details") or {}
    email = details.get("email") or session.get("customer_email")
    stripe_customer = session.get("customer")
    if isinstance(stripe_customer, dict):
        stripe_customer = stripe_customer.get("id")
    customer_id = metadata.get("paywall_customer") or session.get("client_reference_id")
    if access.get_customer(db, customer_id) is None:
        customer_id = access.create_customer(db)["id"]
    customer = access.attach_details(db, customer_id, email, stripe_customer)
    code = metadata.get("code") or None
    plan_id = metadata.get("plan")
    amount = session.get("amount_total")
    currency = session.get("currency")
    if session.get("mode") == "subscription":
        sub = session.get("subscription")
        if isinstance(sub, str):
            sub = (stripe or client(cfg)).get_subscription(sub)
        if sub:
            sync_subscription(db, cfg, sub, customer["id"], amount=amount, currency=currency, code=code)
    else:
        seconds = metadata.get("access_seconds")
        expires = access.now() + int(seconds) if seconds else None
        existing = db.execute("SELECT 1 FROM entitlements WHERE source_ref = ?", (session["id"],)).fetchone()
        if not existing:
            access.grant(db, customer["id"], plan_id or "one_time", expires, session["id"], amount, currency, code)
    if code:
        codes.record_use(db, code, customer["id"], f"checkout:{session['id']}")
    return customer["id"]


def sync_subscription(db, cfg, sub, customer_id=None, amount=None, currency=None, code=None):
    metadata = sub.get("metadata") or {}
    if metadata.get("paywall_instance") not in (None, "", cfg.instance_id):
        return None
    customer_id = customer_id or metadata.get("paywall_customer") or None
    if customer_id and access.get_customer(db, customer_id) is None:
        customer_id = None
    status = sub.get("status")
    period_end = subscription_period_end(sub)
    if status in ACTIVE_SUB_STATUSES and period_end:
        return access.upsert_subscription(
            db, customer_id, sub["id"], "active", period_end + access.SUBSCRIPTION_GRACE,
            amount, currency, code or metadata.get("code") or None,
        )
    if status in ENDED_SUB_STATUSES:
        ended = sub.get("ended_at") or access.now()
        return access.upsert_subscription(db, customer_id, sub["id"], "ended", int(ended))
    return None


def refresh_lapsed(db, cfg, customer_id):
    """Ask Stripe about subscriptions that look expired locally."""
    if not cfg.stripe_ready:
        return
    lapsed = access.lapsed_subscriptions(db, customer_id)
    if not lapsed:
        return
    stripe = client(cfg)
    for ent in lapsed:
        try:
            sync_subscription(db, cfg, stripe.get_subscription(ent["source_ref"]), customer_id)
        except StripeError as exc:
            log.warning("Could not refresh subscription %s: %s", ent["source_ref"], exc)


def handle_event(db, cfg, event, stripe=None):
    """Process a verified Stripe webhook event. Returns a short status string."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if (
            obj.get("mode") == "subscription"
            and isinstance(obj.get("subscription"), str)
            and (obj.get("metadata") or {}).get("paywall_instance") == cfg.instance_id
        ):
            obj = dict(obj, subscription=(stripe or client(cfg)).get_subscription(obj["subscription"]))
        return "fulfilled" if fulfill_checkout(db, cfg, obj, stripe) else "ignored"
    if etype.startswith("customer.subscription."):
        return "synced" if sync_subscription(db, cfg, obj) else "ignored"
    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        sub_id = obj.get("subscription")
        if not sub_id:
            sub_id = ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if not sub_id or not db.execute("SELECT 1 FROM entitlements WHERE source_ref = ?", (sub_id,)).fetchone():
            return "ignored"
        sub = (stripe or client(cfg)).get_subscription(sub_id)
        return "synced" if sync_subscription(db, cfg, sub) else "ignored"
    return "ignored"


def grant_free(db, cfg, code_row, customer):
    """Redeem a ``free`` code (or a 100 % discount on a one-off plan)."""
    days = code_row["grant_days"]
    expires = access.now() + days * plans.DAY if days else None
    context = f"free:{code_row['code']}:{customer['id']}"
    if codes.record_use(db, code_row["code"], customer["id"], context):
        access.grant(db, customer["id"], "code", expires, context, 0, cfg["currency"], code_row["code"])
