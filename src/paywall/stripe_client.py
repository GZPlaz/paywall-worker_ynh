"""Minimal Stripe API client (no third-party dependency)."""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stripe.com/v1"
API_VERSION = "2024-06-20"


class StripeError(Exception):
    pass


def encode_form(params, prefix=None):
    """Encode nested dicts/lists the way Stripe expects (a[b][0][c]=...)."""
    pairs = []
    items = params.items() if isinstance(params, dict) else enumerate(params)
    for key, value in items:
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            pairs.extend(encode_form(value, name))
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        else:
            pairs.append((name, str(value)))
    return pairs


class Stripe:
    def __init__(self, secret_key, timeout=20):
        if not secret_key:
            raise StripeError("Stripe secret key is not configured")
        self.secret_key = secret_key
        self.timeout = timeout

    def request(self, method, path, params=None):
        url = API_BASE + path
        data = None
        if params:
            encoded = urllib.parse.urlencode(encode_form(params))
            if method == "GET":
                url += "?" + encoded
            else:
                data = encoded.encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.secret_key}")
        req.add_header("Stripe-Version", API_VERSION)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read())["error"]["message"]
            except Exception:
                message = f"HTTP {exc.code}"
            raise StripeError(message) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise StripeError(f"Could not reach Stripe: {exc}") from exc

    def create_checkout_session(self, params):
        return self.request("POST", "/checkout/sessions", params)

    def get_checkout_session(self, session_id):
        return self.request("GET", f"/checkout/sessions/{urllib.parse.quote(session_id)}",
                            {"expand": ["subscription"]})

    def get_subscription(self, subscription_id):
        return self.request("GET", f"/subscriptions/{urllib.parse.quote(subscription_id)}")

    def create_coupon(self, params):
        return self.request("POST", "/coupons", params)

    def create_portal_session(self, customer, return_url):
        return self.request("POST", "/billing_portal/sessions", {"customer": customer, "return_url": return_url})


def subscription_period_end(sub):
    """``current_period_end`` moved to subscription items in recent API
    versions; support both shapes."""
    end = sub.get("current_period_end")
    if end:
        return int(end)
    items = (sub.get("items") or {}).get("data") or []
    ends = [int(item["current_period_end"]) for item in items if item.get("current_period_end")]
    return max(ends) if ends else None


def verify_webhook(payload: bytes, header: str, secret: str, tolerance=300, now=None):
    """Verify a ``Stripe-Signature`` header and return the decoded event."""
    if not secret:
        raise StripeError("Webhook secret is not configured")
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        raise StripeError("Malformed Stripe-Signature header")
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise StripeError("Invalid webhook signature")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise StripeError("Invalid webhook timestamp") from exc
    if abs((now or time.time()) - ts) > tolerance:
        raise StripeError("Webhook timestamp outside tolerance")
    return json.loads(payload)
