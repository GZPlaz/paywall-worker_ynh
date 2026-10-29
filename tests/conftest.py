import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paywall import config, db, offers, sites  # noqa: E402

PAYWALL_DOMAIN = "pay.example.com"


def make_config(tmp_path, **overrides):
    values = {
        "app": "paywall_worker",
        "domain": PAYWALL_DOMAIN,
        "path": "/",
        "data_dir": str(tmp_path),
        "signing_secret": "s3cret-signing-key",
        "flask_secret": "flask-secret",
        "origin_secret": "origin-secret",
        "stripe_secret_key": "sk_test_123",
        "stripe_webhook_secret": "whsec_test",
    }
    values.update({k: str(v) for k, v in overrides.items()})
    return config.from_dict(values)


def seed(conn, conf):
    """Two sites, offers for each, and a bundle sold on both."""
    for site_id, title, routes in [
        ("blog", "The Long Read", "blog.example.com/*"),
        ("news", "Daily Brief", "news.example.org/premium/*"),
    ]:
        sites.save(conn, {"id": site_id, "name": site_id.title(), "title": title, "routes": routes,
                          "bypass_paths": sites.DEFAULT_BYPASS, "show_codes": "1", "active": "1"},
                   PAYWALL_DOMAIN, create=True)
    made = {}
    for key, values, linked in [
        ("one_time", {"name": "Lifetime access", "kind": "one_time", "price": "9"}, ["blog"]),
        ("subscription", {"name": "Membership", "kind": "subscription", "price": "4", "interval": "month"},
         ["blog", "news"]),
        ("pass", {"name": "Day pass", "kind": "pass", "price": "1", "pass_hours": "24"}, ["blog"]),
        ("pwyw", {"name": "Pay what you want", "kind": "pwyw", "price": "2", "suggested": "5"}, ["blog"]),
        ("news_only", {"name": "News pass", "kind": "one_time", "price": "3", "access_days": "30"}, ["news"]),
    ]:
        made[key] = offers.save(conn, conf, dict(values, active="1"), linked)
    return made


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path))
    db.migrate(connection)
    return connection
