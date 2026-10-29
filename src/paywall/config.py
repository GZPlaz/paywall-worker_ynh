"""Runtime configuration.

The YunoHost scripts store every option as an app setting (most of them are
editable from the app's config panel). On install/upgrade/config change they
are piped into ``python -m paywall write-config`` which validates them against
``SCHEMA`` and writes a JSON file read by the web app at startup.
"""

import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation

# key -> (type, default). Types: str, text, int, bool, money.
# Instance-wide settings (config panel). Per-site settings live in the
# database and are edited from the admin pages.
SCHEMA = {
    # Set by YunoHost itself
    "app": ("str", "paywall_worker"),
    "domain": ("str", "localhost"),
    "path": ("str", "/"),
    "port": ("int", 8095),
    "data_dir": ("str", "/home/yunohost.app/paywall_worker"),
    # Secrets generated at install time
    "signing_secret": ("str", ""),
    "flask_secret": ("str", ""),
    "origin_secret": ("str", ""),
    # Shared by all sites
    "currency": ("str", "usd"),
    "callback_prefix": ("str", "/__paywall"),
    "cookie_name": ("str", "__Host-paywall"),
    "session_days": ("int", 365),
    # Payments
    "payment_provider": ("str", "stripe"),
    "stripe_secret_key": ("str", ""),
    "stripe_webhook_secret": ("str", ""),
    # Invoicing and Stripe Tax
    "stripe_invoices": ("bool", True),
    "stripe_automatic_tax": ("bool", False),
    "stripe_tax_behavior": ("str", "account_default"),
    "stripe_tax_code": ("str", ""),
    "stripe_collect_tax_ids": ("bool", False),
    # Email login ("restore my access" magic links)
    "enable_email_login": ("bool", True),
    "mail_from": ("str", ""),
    "smtp_host": ("str", "localhost"),
    "smtp_port": ("int", 25),
    # Cloudflare
    "cf_api_token": ("str", ""),
    "cf_account_id": ("str", ""),
    "cf_zone_id": ("str", ""),
    "worker_name": ("str", ""),
    "cf_auto_deploy": ("bool", True),
    "cf_cleanup_on_remove": ("bool", True),
}

# Settings of version 1.0.0~ynh1, when there was a single site configured in
# the config panel. Only read once, to create the first site and its offers.
LEGACY_SCHEMA = {
    "paywall_title": ("str", "Members only"),
    "paywall_description": ("text", ""),
    "accent_color": ("str", "#1b2a41"),
    "support_email": ("str", ""),
    "show_codes_field": ("bool", True),
    "protected_routes": ("text", ""),
    "bypass_paths": ("text", "/robots.txt\n/favicon.ico\n/.well-known/*"),
    "access_recheck_hours": ("int", 24),
    "enable_one_time": ("bool", False),
    "one_time_name": ("str", "Lifetime access"),
    "one_time_price": ("money", "9.00"),
    "one_time_access_days": ("int", 0),
    "enable_subscription": ("bool", False),
    "subscription_name": ("str", "Membership"),
    "subscription_price": ("money", "4.00"),
    "subscription_interval": ("str", "month"),
    "subscription_trial_days": ("int", 0),
    "enable_pass": ("bool", False),
    "pass_name": ("str", "Day pass"),
    "pass_price": ("money", "1.00"),
    "pass_hours": ("int", 24),
    "enable_pwyw": ("bool", False),
    "pwyw_name": ("str", "Pay what you want"),
    "pwyw_min_price": ("money", "2.00"),
    "pwyw_suggested_price": ("money", "5.00"),
    "pwyw_access_days": ("int", 0),
}

# Keys that are not YunoHost app settings (passed explicitly by the scripts).
DERIVED_KEYS = {"app", "data_dir"}

CURRENCIES = {
    "usd", "eur", "gbp", "cad", "aud", "nzd", "chf", "sek", "nok", "dkk",
    "pln", "czk", "brl", "mxn", "inr", "sgd", "hkd", "jpy", "krw",
}
ZERO_DECIMAL_CURRENCIES = {"jpy", "krw"}
PAYMENT_PROVIDERS = {"stripe", "none"}
TAX_BEHAVIORS = {"account_default", "inclusive", "exclusive"}


class ConfigError(ValueError):
    pass


def _coerce(key, kind, raw, default):
    if raw is None:
        return default
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().lower()
        if value == "":
            return default
        return value in ("1", "true", "yes", "on")
    if kind == "int":
        if isinstance(raw, int):
            return raw
        value = str(raw).strip()
        if value == "":
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{key}: '{raw}' is not a whole number") from exc
    if kind == "money":
        value = str(raw).strip().replace(",", ".")
        if value == "":
            return default
        try:
            dec = Decimal(value)
        except InvalidOperation as exc:
            raise ConfigError(f"{key}: '{raw}' is not a valid price") from exc
        if dec < 0:
            raise ConfigError(f"{key}: price cannot be negative")
        return str(dec)
    if kind == "text":
        return str(raw).replace("\r\n", "\n")
    return str(raw).strip()


def normalize(values):
    """Validate a dict of raw values (strings) and return a typed dict."""
    cfg = {}
    for key, (kind, default) in SCHEMA.items():
        cfg[key] = _coerce(key, kind, values.get(key), default)
    legacy = {}
    for key, (kind, default) in LEGACY_SCHEMA.items():
        try:
            legacy[key] = _coerce(key, kind, values.get(key), default)
        except ConfigError:
            legacy[key] = default
    if isinstance(values.get("legacy"), dict):
        legacy.update(values["legacy"])
    cfg["legacy"] = legacy

    cfg["currency"] = cfg["currency"].lower()
    if cfg["currency"] not in CURRENCIES:
        raise ConfigError(f"currency: unsupported currency '{cfg['currency']}'")
    if cfg["payment_provider"] not in PAYMENT_PROVIDERS:
        raise ConfigError("payment_provider: must be 'stripe' or 'none'")
    if cfg["stripe_tax_behavior"] not in TAX_BEHAVIORS:
        raise ConfigError("stripe_tax_behavior: must be account_default, inclusive or exclusive")
    if cfg["stripe_tax_code"] and not re.fullmatch(r"txcd_\d{8}", cfg["stripe_tax_code"]):
        raise ConfigError("stripe_tax_code: must look like txcd_12345678 (or be empty)")
    if not re.fullmatch(r"/[A-Za-z0-9_-]+", cfg["callback_prefix"]):
        raise ConfigError("callback_prefix: must look like /__paywall")
    cfg["session_days"] = max(1, cfg["session_days"])
    if not cfg["path"].startswith("/"):
        cfg["path"] = "/" + cfg["path"]
    if not cfg["worker_name"]:
        cfg["worker_name"] = re.sub(r"[^a-z0-9-]", "-", cfg["app"].lower())[:63]
    if not cfg["mail_from"]:
        cfg["mail_from"] = f"noreply@{cfg['domain']}"
    return cfg


class Config(dict):
    """Typed configuration with a few derived helpers."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    @property
    def public_url(self):
        return f"https://{self['domain']}{self['path'].rstrip('/')}"

    @property
    def zero_decimal(self):
        return self["currency"] in ZERO_DECIMAL_CURRENCIES

    @property
    def instance_id(self):
        """Tags the Stripe objects created by this paywall. Derived from the
        signing secret so it survives domain changes."""
        return "pw_" + hashlib.sha256(self["signing_secret"].encode()).hexdigest()[:16]

    @property
    def integration_identifier(self):
        """Label for Checkout Sessions in the Stripe Dashboard, with the
        8-letter suffix Stripe asks for (stable per installation)."""
        digest = hashlib.sha256(("integration:" + self["signing_secret"]).encode()).digest()
        return "paywall-worker-" + "".join(chr(97 + b % 26) for b in digest[:8])

    @property
    def stripe_ready(self):
        return self["payment_provider"] == "stripe" and bool(self["stripe_secret_key"])

    @property
    def cloudflare_ready(self):
        return bool(self["cf_api_token"] and self["cf_account_id"])


def load(path=None):
    path = path or os.environ.get("PAYWALL_CONFIG", "config.json")
    with open(path, encoding="utf-8") as fh:
        return Config(normalize(json.load(fh)))


def from_dict(values):
    return Config(normalize(values))
