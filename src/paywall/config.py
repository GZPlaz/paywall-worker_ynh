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
from urllib.parse import urlsplit

# key -> (type, default). Types: str, text, int, bool, money.
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
    # Paywall page
    "paywall_title": ("str", "Members only"),
    "paywall_description": ("text", "Support this site to unlock full access to every page."),
    "accent_color": ("str", "#4f46e5"),
    "support_email": ("str", ""),
    "show_codes_field": ("bool", True),
    # Protected sites
    "protected_routes": ("text", ""),
    "bypass_paths": ("text", "/robots.txt\n/favicon.ico\n/.well-known/*"),
    "callback_prefix": ("str", "/__paywall"),
    "cookie_name": ("str", "__Host-paywall"),
    "access_recheck_hours": ("int", 24),
    "session_days": ("int", 365),
    # Pricing
    "currency": ("str", "usd"),
    "enable_one_time": ("bool", True),
    "one_time_name": ("str", "Lifetime access"),
    "one_time_price": ("money", "9.00"),
    "one_time_access_days": ("int", 0),
    "enable_subscription": ("bool", True),
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
    # Payments
    "payment_provider": ("str", "stripe"),
    "stripe_secret_key": ("str", ""),
    "stripe_webhook_secret": ("str", ""),
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

# Keys that are not YunoHost app settings (passed explicitly by the scripts).
DERIVED_KEYS = {"app", "data_dir"}

CURRENCIES = {
    "usd", "eur", "gbp", "cad", "aud", "nzd", "chf", "sek", "nok", "dkk",
    "pln", "czk", "brl", "mxn", "inr", "sgd", "hkd", "jpy", "krw",
}
ZERO_DECIMAL_CURRENCIES = {"jpy", "krw"}
SUBSCRIPTION_INTERVALS = {"day", "week", "month", "year"}
PAYMENT_PROVIDERS = {"stripe", "none"}

_ROUTE_RE = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z0-9-]+(/[^\s]*)?$")


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

    cfg["currency"] = cfg["currency"].lower()
    if cfg["currency"] not in CURRENCIES:
        raise ConfigError(f"currency: unsupported currency '{cfg['currency']}'")
    if cfg["subscription_interval"] not in SUBSCRIPTION_INTERVALS:
        raise ConfigError("subscription_interval: must be day, week, month or year")
    if cfg["payment_provider"] not in PAYMENT_PROVIDERS:
        raise ConfigError("payment_provider: must be 'stripe' or 'none'")
    if not re.fullmatch(r"/[A-Za-z0-9_-]+", cfg["callback_prefix"]):
        raise ConfigError("callback_prefix: must look like /__paywall")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", cfg["accent_color"]):
        cfg["accent_color"] = SCHEMA["accent_color"][1]
    cfg["access_recheck_hours"] = max(1, cfg["access_recheck_hours"])
    cfg["session_days"] = max(1, cfg["session_days"])
    cfg["pass_hours"] = max(1, cfg["pass_hours"])
    for key in ("one_time_access_days", "pwyw_access_days", "subscription_trial_days"):
        cfg[key] = max(0, cfg[key])
    if not cfg["path"].startswith("/"):
        cfg["path"] = "/" + cfg["path"]
    if not cfg["worker_name"]:
        cfg["worker_name"] = re.sub(r"[^a-z0-9-]", "-", cfg["app"].lower())[:63]
    if not cfg["mail_from"]:
        cfg["mail_from"] = f"noreply@{cfg['domain']}"
    parse_routes(cfg["protected_routes"], paywall_domain=cfg["domain"])
    return cfg


def parse_routes(text, paywall_domain=None):
    """Parse Cloudflare route patterns, one per line. Returns a list of
    (pattern, host, path) tuples. Raises ConfigError on bad input."""
    routes = []
    for line in (text or "").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        pattern = re.sub(r"^https?://", "", pattern).lower()
        if "/" not in pattern:
            pattern += "/*"
        if not _ROUTE_RE.match(pattern):
            raise ConfigError(f"protected route '{line.strip()}' is not a valid pattern (example: blog.example.com/*)")
        host, _, path = pattern.partition("/")
        if paywall_domain and _host_matches(paywall_domain.lower(), host):
            raise ConfigError(
                f"protected route '{pattern}' would cover the paywall itself ({paywall_domain}); "
                "install the paywall on a different (sub)domain"
            )
        routes.append((pattern, host, "/" + path))
    return routes


def _host_matches(host, pattern_host):
    if pattern_host.startswith("*."):
        return host.endswith(pattern_host[1:]) or host == pattern_host[2:]
    return host == pattern_host


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
    def routes(self):
        return parse_routes(self["protected_routes"])

    @property
    def zero_decimal(self):
        return self["currency"] in ZERO_DECIMAL_CURRENCIES

    @property
    def instance_id(self):
        """Tags the Stripe objects created by this paywall. Derived from the
        signing secret so it survives domain changes."""
        return "pw_" + hashlib.sha256(self["signing_secret"].encode()).hexdigest()[:16]

    @property
    def stripe_ready(self):
        return self["payment_provider"] == "stripe" and bool(self["stripe_secret_key"])

    @property
    def cloudflare_ready(self):
        return bool(self["cf_api_token"] and self["cf_account_id"])

    def is_allowed_return(self, url):
        """True if ``url`` points to one of the protected hosts over https."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return False
        host = parts.hostname.lower()
        return any(_host_matches(host, route_host) for _, route_host, _ in self.routes)

    def default_return(self):
        for _, host, path in self.routes:
            if not host.startswith("*."):
                prefix = path.rstrip("*").rstrip("/")
                return f"https://{host}{prefix}/"
        return None


def load(path=None):
    path = path or os.environ.get("PAYWALL_CONFIG", "config.json")
    with open(path, encoding="utf-8") as fh:
        return Config(normalize(json.load(fh)))


def from_dict(values):
    return Config(normalize(values))
