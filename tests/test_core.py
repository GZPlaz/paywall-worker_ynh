import time

import pytest
from conftest import make_config

from paywall import access, codes, config, db, plans, tokens
from paywall.stripe_client import StripeError, encode_form, subscription_period_end, verify_webhook


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path))
    db.migrate(connection)
    return connection


def test_token_roundtrip_and_tampering():
    token = tokens.sign({"v": 1, "typ": "access", "sub": "c_1", "exp": time.time() + 60}, "k")
    assert tokens.verify(token, "k", "access")["sub"] == "c_1"
    assert tokens.verify(token, "other", "access") is None
    assert tokens.verify(token, "k", "handoff") is None
    body, sig = token.split(".")
    assert tokens.verify(body + "x." + sig, "k", "access") is None


def test_expired_token_rejected():
    token = tokens.sign({"v": 1, "typ": "access", "sub": "c", "exp": time.time() - 1}, "k")
    assert tokens.verify(token, "k", "access") is None


def test_routes_parsing_and_return_urls(cfg):
    assert [r[0] for r in cfg.routes] == ["blog.example.com/*", "news.example.org/premium/*"]
    assert cfg.is_allowed_return("https://blog.example.com/post/1")
    assert not cfg.is_allowed_return("http://blog.example.com/post/1")
    assert not cfg.is_allowed_return("https://evil.com/?blog.example.com")
    assert not cfg.is_allowed_return("https://user@blog.example.com/")
    assert cfg.default_return() == "https://blog.example.com/"


def test_route_cannot_cover_paywall(tmp_path):
    with pytest.raises(config.ConfigError):
        make_config(tmp_path, protected_routes="*.example.com/*")
    with pytest.raises(config.ConfigError):
        make_config(tmp_path, protected_routes="not a route")


def test_plans_from_config(cfg):
    built = {p.id: p for p in plans.build(cfg)}
    assert set(built) == {"one_time", "subscription", "pass", "pwyw"}
    assert built["one_time"].amount == 900 and built["one_time"].access_seconds is None
    assert built["subscription"].interval == "month"
    assert built["pass"].access_seconds == 24 * 3600
    assert built["pwyw"].custom_amount and built["pwyw"].suggested == 500
    assert plans.price_label(built["subscription"], cfg) == "$4 / month"


def test_zero_decimal_currency(tmp_path):
    conf = make_config(tmp_path, currency="jpy", one_time_price="900")
    assert plans.get(conf, "one_time").amount == 900
    assert plans.format_money(900, conf) == "¥900"


def test_codes_lifecycle(conn):
    row = codes.create(conn, code="spring25", kind="percent", value=25, plans="one_time", max_uses=1)
    assert row["code"] == "SPRING25"
    assert codes.discounted(row, 1000) == 750
    with pytest.raises(codes.CodeError):
        codes.check(conn, "SPRING25", "subscription")
    assert codes.check(conn, "spring25", "one_time")
    assert codes.record_use(conn, "SPRING25", "c_1", "ctx1")
    assert not codes.record_use(conn, "SPRING25", "c_1", "ctx1")  # idempotent
    with pytest.raises(codes.CodeError):
        codes.check(conn, "SPRING25")  # used up
    with pytest.raises(codes.CodeError):
        codes.create(conn, code="SPRING25", kind="percent", value=10)
    with pytest.raises(codes.CodeError):
        codes.create(conn, kind="percent", value=150)


def test_expired_and_disabled_codes(conn):
    codes.create(conn, code="OLD", kind="free", expires_at=int(time.time()) - 10)
    with pytest.raises(codes.CodeError):
        codes.check(conn, "OLD")
    codes.create(conn, code="OFF", kind="fixed", value=100)
    codes.set_active(conn, "OFF", False)
    with pytest.raises(codes.CodeError):
        codes.check(conn, "OFF")


def test_access_until(conn):
    customer = access.create_customer(conn)
    assert access.access_until(conn, customer["id"]) == (False, None)
    soon = access.now() + 100
    access.grant(conn, customer["id"], "pass", soon, "ref1")
    assert access.access_until(conn, customer["id"]) == (True, soon)
    access.grant(conn, customer["id"], "one_time", None, "ref2")
    assert access.access_until(conn, customer["id"]) == (True, None)
    access.grant(conn, customer["id"], "one_time", None, "ref2")  # idempotent
    assert len(access.entitlements_for(conn, customer["id"])) == 2


def test_attach_details_never_steals_an_existing_email(conn):
    owner = access.create_customer(conn, "victim@example.com")
    other = access.create_customer(conn)
    updated = access.attach_details(conn, other["id"], "victim@example.com", "cus_x")
    assert updated["email"] is None
    assert updated["stripe_customer_id"] == "cus_x"
    assert access.find_by_email(conn, "VICTIM@example.com")["id"] == owner["id"]


def test_merge_moves_entitlements(conn):
    anon = access.create_customer(conn)
    target = access.create_customer(conn, "me@example.com")
    access.grant(conn, anon["id"], "code", None, "free:X:anon")
    assert access.merge(conn, anon["id"], target["id"])
    assert access.get_customer(conn, anon["id"]) is None
    assert access.access_until(conn, target["id"])[0]
    # customers with an email are never merged away
    assert not access.merge(conn, target["id"], anon["id"])


def test_stripe_form_encoding():
    pairs = encode_form({"line_items": [{"price_data": {"unit_amount": 500}, "quantity": 1}], "a": True, "n": None})
    assert ("line_items[0][price_data][unit_amount]", "500") in pairs
    assert ("a", "true") in pairs
    assert all(k != "n" for k, _ in pairs)


def test_stripe_webhook_signature():
    import hashlib
    import hmac
    payload = b'{"type":"x"}'
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec", ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    assert verify_webhook(payload, f"t={ts},v1={sig}", "whsec") == {"type": "x"}
    with pytest.raises(StripeError):
        verify_webhook(payload, f"t={ts},v1={'0' * 64}", "whsec")
    with pytest.raises(StripeError):
        verify_webhook(payload, f"t={int(ts) - 1000},v1={sig}", "whsec")


def test_subscription_period_end_shapes():
    assert subscription_period_end({"current_period_end": 10}) == 10
    assert subscription_period_end({"items": {"data": [{"current_period_end": 20}]}}) == 20


def test_instance_id_survives_domain_change(tmp_path):
    assert make_config(tmp_path).instance_id == make_config(tmp_path, domain="other.example.net").instance_id
