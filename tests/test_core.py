import hashlib
import hmac
import time

import pytest
from conftest import PAYWALL_DOMAIN, make_config, seed

from paywall import access, codes, db, legacy, offers, plans, sites, tokens
from paywall.stripe_client import StripeError, encode_form, subscription_period_end, verify_webhook


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


def test_handoff_names_the_site():
    token = tokens.handoff("k", "c_1", "blog", time.time() + 60, "https://blog.example.com/")
    assert tokens.verify(token, "k", "handoff")["site"] == "blog"


def test_route_parsing_and_matching(conn, cfg):
    seed(conn, cfg)
    blog, news = sites.get(conn, "blog"), sites.get(conn, "news")
    assert blog.match("https://blog.example.com/post/1") == ""
    assert news.match("https://news.example.org/premium/today") == "/premium"
    assert news.match("https://news.example.org/free") is None
    assert blog.match("http://blog.example.com/post") is None
    assert blog.match("https://user@blog.example.com/") is None
    assert blog.default_return() == "https://blog.example.com/"
    assert news.default_return() == "https://news.example.org/premium/"
    assert sites.for_url(conn, "https://news.example.org/premium/x").id == "news"
    assert sites.for_url(conn, "https://evil.example.net/") is None


def test_site_validation(conn, cfg):
    seed(conn, cfg)
    base = {"name": "x", "title": "x", "active": "1"}
    with pytest.raises(sites.SiteError, match="paywall"):
        sites.save(conn, dict(base, id="pw", routes="*.example.com/*"), PAYWALL_DOMAIN, create=True)
    with pytest.raises(sites.SiteError, match="already used"):
        sites.save(conn, dict(base, id="dup", routes="blog.example.com/*"), PAYWALL_DOMAIN, create=True)
    with pytest.raises(sites.SiteError, match="short name"):
        sites.save(conn, dict(base, id="Bad Name", routes=""), PAYWALL_DOMAIN, create=True)
    with pytest.raises(sites.SiteError, match="not a valid route"):
        sites.save(conn, dict(base, id="ok", routes="not a route"), PAYWALL_DOMAIN, create=True)


def test_offers_link_to_several_sites(conn, cfg):
    made = seed(conn, cfg)
    assert made["subscription"].site_ids == ["blog", "news"]
    assert [p.name for p in offers.list_all(conn, site_id="news")] == ["Membership", "News pass"]
    assert made["one_time"].amount == 900 and made["one_time"].access_seconds is None
    assert made["pass"].access_seconds == 24 * 3600
    assert made["pwyw"].custom_amount and made["pwyw"].suggested == 500
    assert plans.price_label(made["subscription"], cfg) == "$4 / month"
    with pytest.raises(offers.OfferError, match="at least one site"):
        offers.save(conn, cfg, {"name": "x", "kind": "one_time", "price": "1"}, [])


def test_extending_an_offer_gives_holders_the_new_site(conn, cfg):
    made = seed(conn, cfg)
    customer = access.create_customer(conn)
    access.grant(conn, customer["id"], "one_time", sites=["blog"], offer_id=int(made["one_time"].id))
    assert access.access_until(conn, customer["id"], "news") == (False, None)
    values = {"name": "Lifetime access", "kind": "one_time", "price": "9", "active": "1"}
    offers.save(conn, cfg, values, ["blog", "news"], offer_id=made["one_time"].id, extend_holders=True)
    assert access.access_until(conn, customer["id"], "news") == (True, None)


def test_zero_decimal_currency(tmp_path, conn):
    conf = make_config(tmp_path, currency="jpy")
    seed(conn, conf)
    plan = offers.save(conn, conf, {"name": "x", "kind": "one_time", "price": "900", "active": "1"}, ["blog"])
    assert plan.amount == 900
    assert plans.format_money(900, conf) == "¥900"


def test_codes_lifecycle(conn, cfg):
    made = seed(conn, cfg)
    one_time = made["one_time"].id
    row = codes.create(conn, code="spring25", kind="percent", value=25, plans=one_time, max_uses=1)
    assert row["code"] == "SPRING25"
    assert codes.discounted(row, 1000) == 750
    with pytest.raises(codes.CodeError):
        codes.check(conn, "SPRING25", made["subscription"].id)
    assert codes.check(conn, "spring25", one_time)
    assert codes.record_use(conn, "SPRING25", "c_1", "ctx1")
    assert not codes.record_use(conn, "SPRING25", "c_1", "ctx1")  # idempotent
    with pytest.raises(codes.CodeError):
        codes.check(conn, "SPRING25")  # used up
    with pytest.raises(codes.CodeError):
        codes.create(conn, code="SPRING25", kind="percent", value=10)
    with pytest.raises(codes.CodeError):
        codes.create(conn, kind="percent", value=150)


def test_codes_are_limited_to_their_sites(conn, cfg):
    seed(conn, cfg)
    codes.create(conn, code="NEWSONLY", kind="free", sites=["news"])
    codes.create(conn, code="ANYWHERE", kind="free")
    assert codes.check(conn, "NEWSONLY", site_id="news")
    with pytest.raises(codes.CodeError, match="not valid on this site"):
        codes.check(conn, "NEWSONLY", site_id="blog")
    assert codes.check(conn, "ANYWHERE", site_id="blog")
    assert codes.unlocks(conn, codes.get(conn, "NEWSONLY")) == ["news"]
    assert sorted(codes.unlocks(conn, codes.get(conn, "ANYWHERE"))) == ["blog", "news"]


def test_expired_and_disabled_codes(conn):
    codes.create(conn, code="OLD", kind="free", expires_at=int(time.time()) - 10)
    with pytest.raises(codes.CodeError):
        codes.check(conn, "OLD")
    codes.create(conn, code="OFF", kind="fixed", value=100)
    codes.set_active(conn, "OFF", False)
    with pytest.raises(codes.CodeError):
        codes.check(conn, "OFF")


def test_access_is_per_site(conn, cfg):
    seed(conn, cfg)
    customer = access.create_customer(conn)
    assert access.access_until(conn, customer["id"], "blog") == (False, None)
    soon = access.now() + 100
    access.grant(conn, customer["id"], "pass", soon, "ref1", sites=["blog"])
    assert access.access_until(conn, customer["id"], "blog") == (True, soon)
    assert access.access_until(conn, customer["id"], "news") == (False, None)
    access.grant(conn, customer["id"], "one_time", None, "ref2", sites=["blog", "news"])
    assert access.access_until(conn, customer["id"], "news") == (True, None)
    access.grant(conn, customer["id"], "one_time", None, "ref2", sites=["blog", "news"])  # idempotent
    assert len(access.entitlements_for(conn, customer["id"])) == 2
    assert access.access_by_site(conn, customer["id"]) == {"blog": None, "news": None}


def test_attach_details_never_steals_an_existing_email(conn):
    owner = access.create_customer(conn, "victim@example.com")
    other = access.create_customer(conn)
    updated = access.attach_details(conn, other["id"], "victim@example.com", "cus_x")
    assert updated["email"] is None
    assert updated["stripe_customer_id"] == "cus_x"
    assert access.find_by_email(conn, "VICTIM@example.com")["id"] == owner["id"]


def test_merge_moves_entitlements(conn, cfg):
    seed(conn, cfg)
    anon = access.create_customer(conn)
    target = access.create_customer(conn, "me@example.com")
    access.grant(conn, anon["id"], "code", None, "free:X:anon", sites=["news"])
    assert access.merge(conn, anon["id"], target["id"])
    assert access.get_customer(conn, anon["id"]) is None
    assert access.access_until(conn, target["id"], "news")[0]
    # customers with an email are never merged away
    assert not access.merge(conn, target["id"], anon["id"])


def test_legacy_single_site_settings_are_imported_once(tmp_path, conn):
    conf = make_config(
        tmp_path, paywall_title="Old title", protected_routes="blog.example.com/*\nbad route here",
        enable_one_time="1", one_time_price="9", enable_subscription="1", subscription_price="4",
        enable_pass="0", accent_color="#4f46e5",
    )
    old_customer = access.create_customer(conn, "old@example.com")
    access.grant(conn, old_customer["id"], "subscription", access.now() + 1000, "sub_old")
    codes.create(conn, code="OLDSUB", kind="percent", value=10, plans="subscription")
    codes.create(conn, code="OLDPASS", kind="percent", value=10, plans="pass")
    assert legacy.import_settings(conn, conf) == "main"
    site = sites.get(conn, "main")
    assert site.title == "Old title" and site.routes == "blog.example.com/*"
    names = {p.kind: p for p in offers.list_all(conn)}
    assert set(names) == {"one_time", "subscription"}
    assert access.access_until(conn, old_customer["id"], "main")[0]
    assert codes.get(conn, "OLDSUB")["plans"] == names["subscription"].id
    assert codes.get(conn, "OLDPASS")["plans"] == "0"  # its offer was disabled: stays limited
    assert legacy.import_settings(conn, conf) is None


def test_fresh_install_imports_nothing(cfg, conn):
    assert legacy.import_settings(conn, cfg) is None
    assert sites.list_all(conn) == []
    assert db.get_meta(conn, "legacy_imported") == "1"


def test_button_text_stays_readable():
    assert sites.ink_for("#f5e663") == "#111827"
    assert sites.ink_for("#1b2a41") == "#ffffff"


def test_instance_id_survives_domain_change(tmp_path):
    assert make_config(tmp_path).instance_id == make_config(tmp_path, domain="other.example.net").instance_id


def test_stripe_form_encoding():
    pairs = encode_form({"line_items": [{"price_data": {"unit_amount": 500}, "quantity": 1}], "a": True, "n": None})
    assert ("line_items[0][price_data][unit_amount]", "500") in pairs
    assert ("a", "true") in pairs
    assert all(k != "n" for k, _ in pairs)


def test_stripe_webhook_signature():
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
