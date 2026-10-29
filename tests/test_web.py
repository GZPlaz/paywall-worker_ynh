import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_config

from paywall import access, codes, db, tokens
from paywall.web import create_app

BASE = "https://pay.example.com"


class FakeStripe:
    def __init__(self):
        self.sessions = {}
        self.subscriptions = {}
        self.coupons = []
        self.created = []

    def create_checkout_session(self, params):
        sid = f"cs_test_{len(self.sessions) + 1}"
        self.created.append(params)
        self.sessions[sid] = {
            "id": sid, "status": "open", "payment_status": "unpaid", "mode": params["mode"],
            "metadata": params["metadata"], "client_reference_id": params.get("client_reference_id"),
            "amount_total": params["line_items"][0]["price_data"]["unit_amount"], "currency": "usd",
            "created": int(time.time()), "customer": None, "subscription": None,
        }
        return {"id": sid, "url": f"https://checkout.stripe.com/c/{sid}"}

    def complete(self, sid, email="buyer@example.com"):
        s = self.sessions[sid]
        s.update(status="complete", payment_status="paid", customer="cus_1", customer_details={"email": email})
        if s["mode"] == "subscription":
            sub = {"id": "sub_1", "status": "active", "current_period_end": int(time.time()) + 30 * 86400,
                   "metadata": s["metadata"]}
            self.subscriptions["sub_1"] = sub
            s["subscription"] = sub
        return s

    def get_checkout_session(self, sid):
        return self.sessions[sid]

    def get_subscription(self, sub_id):
        return self.subscriptions[sub_id]

    def create_coupon(self, params):
        self.coupons.append(params)
        return {"id": f"coupon_{len(self.coupons)}"}

    def create_portal_session(self, customer, return_url):
        return {"url": "https://billing.stripe.com/p/session"}


@pytest.fixture
def stripe():
    return FakeStripe()


@pytest.fixture
def app(tmp_path, stripe):
    application = create_app(make_config(tmp_path, enable_pass="1"), stripe_factory=lambda _cfg: stripe)
    application.config.update(TESTING=True, ADMIN_WITHOUT_SSO=False)
    return application


def https_client(app):
    test_client = app.test_client()
    test_client.environ_base.update({"wsgi.url_scheme": "https", "HTTP_HOST": "pay.example.com"})
    return test_client


@pytest.fixture
def client(app):
    return https_client(app)


def csrf(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def follow_handoff(app, location):
    """Check a handoff redirect and return its decoded payload."""
    parts = urlsplit(location)
    assert parts.scheme == "https" and parts.netloc == "blog.example.com"
    assert parts.path == "/__paywall/auth"
    token = parse_qs(parts.query)["t"][0]
    payload = tokens.verify(token, "s3cret-signing-key", "handoff")
    assert payload is not None
    return payload


def conn_for(app):
    return db.connect(app.extensions["paywall_config"]["data_dir"])


def test_paywall_page_lists_offers(client):
    resp = client.get("/?return=https://blog.example.com/post")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Lifetime access" in html and "Membership" in html and "Day pass" in html
    assert "Pay $9" in html and "Subscribe for $4" in html and "per month" in html


def test_post_without_csrf_is_rejected(client):
    assert client.post("/code", data={"code": "X"}).status_code == 400


def test_one_time_checkout_flow(app, client, stripe):
    client.get("/?return=https://blog.example.com/post/1")
    resp = client.post("/checkout", data={"csrf": csrf(client), "plan": "one_time"})
    assert resp.status_code == 303 and resp.location.startswith("https://checkout.stripe.com/")
    params = stripe.created[-1]
    assert params["mode"] == "payment"
    assert params["line_items"][0]["price_data"]["unit_amount"] == 900
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    resp = client.get(f"/checkout/success?session_id={sid}")
    payload = follow_handoff(app, resp.location)
    assert payload["r"] == "https://blog.example.com/post/1"
    assert payload["aexp"] <= time.time() + 24 * 3600 + 5
    conn = conn_for(app)
    customer = access.get_customer(conn, payload["sub"])
    assert customer["email"] == "buyer@example.com"
    assert access.access_until(conn, payload["sub"]) == (True, None)
    # Coming back later: immediate handoff, no paywall shown
    resp = client.get("/?return=https://blog.example.com/other")
    assert follow_handoff(app, resp.location)["r"] == "https://blog.example.com/other"


def test_untrusted_return_url_is_ignored(client):
    client.get("/?return=https://evil.example.net/")
    with client.session_transaction() as sess:
        assert "return" not in sess


def test_subscription_flow_and_webhooks(app, client, stripe):
    client.get("/?return=https://blog.example.com/")
    client.post("/checkout", data={"csrf": csrf(client), "plan": "subscription"})
    assert stripe.created[-1]["mode"] == "subscription"
    assert stripe.created[-1]["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    payload = follow_handoff(app, client.get(f"/checkout/success?session_id={sid}").location)
    conn = conn_for(app)
    has, until = access.access_until(conn, payload["sub"])
    assert has and until > time.time() + 29 * 86400

    # Cancellation webhook ends access
    sub = dict(stripe.subscriptions["sub_1"], status="canceled", ended_at=int(time.time()) - 1)
    event = {"id": "evt_1", "type": "customer.subscription.deleted", "data": {"object": sub}}
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    resp = client.post("/webhook/stripe", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert resp.status_code == 200 and resp.json["result"] == "synced"
    assert access.access_until(conn, payload["sub"])[0] is False


def test_webhook_rejects_bad_signature(client):
    resp = client.post("/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=00"})
    assert resp.status_code == 400


def test_webhook_fulfills_checkout_without_browser(app, client, stripe):
    client.get("/")
    client.post("/checkout", data={"csrf": csrf(client), "plan": "pass"})
    sid = next(iter(stripe.sessions))
    session = stripe.complete(sid)
    body = json.dumps({"id": "evt", "type": "checkout.session.completed", "data": {"object": session}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    resp = client.post("/webhook/stripe", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
    assert resp.json["result"] == "fulfilled"
    cid = session["metadata"]["paywall_customer"]
    has, until = access.access_until(conn_for(app), cid)
    assert has and until <= time.time() + 24 * 3600 + 5


def test_free_code_grants_access(app, client):
    conn = conn_for(app)
    codes.create(conn, code="VIP", kind="free", grant_days=7, max_uses=5)
    client.get("/?return=https://blog.example.com/x")
    resp = client.post("/code", data={"csrf": csrf(client), "code": "vip"})
    payload = follow_handoff(app, resp.location)
    has, until = access.access_until(conn, payload["sub"])
    assert has and 6 * 86400 < until - time.time() <= 7 * 86400
    assert codes.get(conn, "VIP")["uses"] == 1
    # Same visitor cannot redeem it twice
    resp = client.post("/code", data={"csrf": csrf(client, "/?stay=1"), "code": "VIP"})
    assert codes.get(conn, "VIP")["uses"] == 1


def test_discount_code_applies_to_checkout(app, client, stripe):
    conn = conn_for(app)
    codes.create(conn, code="HALF", kind="percent", value=50)
    client.post("/code", data={"csrf": csrf(client), "code": "HALF"})
    html = client.get("/").get_data(as_text=True)
    assert "<s>$9</s>" in html and "Pay $4.50" in html
    client.post("/checkout", data={"csrf": csrf(client), "plan": "one_time"})
    params = stripe.created[-1]
    assert params["discounts"] == [{"coupon": "coupon_1"}]
    assert stripe.coupons[0]["percent_off"] == 50
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    client.get(f"/checkout/success?session_id={sid}")
    assert codes.get(conn, "HALF")["uses"] == 1


def test_full_discount_on_one_off_plan_skips_payment(app, client, stripe):
    conn = conn_for(app)
    codes.create(conn, code="ALLFREE", kind="percent", value=100, plans="one_time")
    client.get("/?return=https://blog.example.com/")
    client.post("/code", data={"csrf": csrf(client), "code": "ALLFREE"})
    resp = client.post("/checkout", data={"csrf": csrf(client, "/?stay=1"), "plan": "one_time"})
    payload = follow_handoff(app, resp.location)
    assert access.access_until(conn, payload["sub"]) == (True, None)
    assert not stripe.created


def test_wrong_codes_are_rate_limited(client):
    for _ in range(10):
        client.post("/code", data={"csrf": csrf(client), "code": "NOPE"})
    client.post("/code", data={"csrf": csrf(client), "code": "NOPE"})
    assert "Too many attempts" in client.get("/").get_data(as_text=True)


def test_pay_what_you_want_minimum(app, client, stripe):
    resp = client.post("/checkout", data={"csrf": csrf(client), "plan": "pwyw", "amount": "1"})
    assert resp.status_code == 302 and not stripe.created
    client.post("/checkout", data={"csrf": csrf(client), "plan": "pwyw", "amount": "12.50"})
    assert stripe.created[-1]["line_items"][0]["price_data"]["unit_amount"] == 1250


def test_admin_requires_sso_header(client):
    assert client.get("/admin/").status_code == 403
    resp = client.get("/admin/", headers={"Ynh-User": "alice"})
    assert resp.status_code == 200 and "Setup" in resp.get_data(as_text=True)


def test_admin_creates_codes_and_grants(app, client):
    headers = {"Ynh-User": "alice"}
    html = client.get("/admin/codes", headers=headers).get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    resp = client.post("/admin/codes", headers=headers, data={
        "csrf": token, "kind": "free", "grant_days": "30", "count": "3",
    })
    assert resp.status_code == 302
    conn = conn_for(app)
    assert len(codes.list_all(conn)) == 3
    resp = client.post("/admin/grant", headers=headers, data={"csrf": token, "email": "friend@example.com"})
    assert resp.status_code == 302
    friend = access.find_by_email(conn, "friend@example.com")
    assert access.access_until(conn, friend["id"]) == (True, None)


def test_magic_link_login(app, client, monkeypatch):
    conn = conn_for(app)
    customer = access.create_customer(conn, "member@example.com")
    access.grant(conn, customer["id"], "manual")
    sent = {}
    monkeypatch.setattr("paywall.mailer.send_login_link", lambda cfg, to, link: sent.update(to=to, link=link))
    client.get("/login?return=https://blog.example.com/a")
    client.post("/login", data={"csrf": csrf(client, "/login"), "email": "member@example.com"})
    assert sent["to"] == "member@example.com"
    resp = client.get(urlsplit(sent["link"]).path + "?" + urlsplit(sent["link"]).query)
    assert follow_handoff(app, resp.location)["sub"] == customer["id"]
    # links are single use
    assert client.get(urlsplit(sent["link"]).path + "?" + urlsplit(sent["link"]).query).status_code == 400


def test_success_page_does_not_log_in_someone_else(app, client, stripe):
    client.get("/")
    client.post("/checkout", data={"csrf": csrf(client), "plan": "one_time"})
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    other = https_client(app)
    other.get("/")
    with other.session_transaction() as sess:
        sess["customer"] = access.create_customer(conn_for(app))["id"]
    resp = other.get(f"/checkout/success?session_id={sid}")
    assert resp.status_code == 200 and "Sign in" in resp.get_data(as_text=True)


def test_subscription_discount_explains_later_price(app, client):
    codes.create(conn_for(app), code="FIRST", kind="percent", value=50, plans="subscription")
    client.post("/code", data={"csrf": csrf(client), "code": "FIRST"})
    html = client.get("/").get_data(as_text=True)
    assert "first month, then $4" in html and "Subscribe for $2" in html


def test_button_text_stays_readable_on_light_accent(tmp_path):
    assert make_config(tmp_path, accent_color="#f5e663").accent_ink == "#111827"
    assert make_config(tmp_path).accent_ink == "#ffffff"
