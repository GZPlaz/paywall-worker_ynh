import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_config, seed

from paywall import access, codes, db, sites, tokens
from paywall.web import create_app


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
    conf = make_config(tmp_path)
    application = create_app(conf, stripe_factory=lambda _cfg: stripe)
    application.config.update(TESTING=True, ADMIN_WITHOUT_SSO=False)
    application.offers = seed(conn_for(application), conf)
    return application


def https_client(app):
    test_client = app.test_client()
    test_client.environ_base.update({"wsgi.url_scheme": "https", "HTTP_HOST": "pay.example.com"})
    return test_client


@pytest.fixture
def client(app):
    return https_client(app)


def offer(app, key):
    return app.offers[key].id


def csrf(client, path="/s/blog?stay=1"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def follow_handoff(location, host="blog.example.com", base="", site="blog"):
    """Check a handoff redirect and return its decoded payload."""
    parts = urlsplit(location)
    assert parts.scheme == "https" and parts.netloc == host
    assert parts.path == f"{base}/__paywall/auth"
    token = parse_qs(parts.query)["t"][0]
    payload = tokens.verify(token, "s3cret-signing-key", "handoff")
    assert payload is not None and payload["site"] == site
    return payload


def conn_for(app):
    return db.connect(app.extensions["paywall_config"]["data_dir"])


def signed(body):
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={sig}"}


def test_each_site_has_its_own_page(client):
    blog = client.get("/s/blog").get_data(as_text=True)
    assert "The Long Read" in blog and "Lifetime access" in blog and "Day pass" in blog
    assert "Pay $9" in blog and "Subscribe for $4" in blog and "Also opens Daily Brief" in blog
    news = client.get("/s/news").get_data(as_text=True)
    assert "Daily Brief" in news and "News pass" in news and "Membership" in news
    assert "Lifetime access" not in news
    assert client.get("/s/nope").status_code == 404


def test_root_routes_visitors_to_the_right_site(client):
    resp = client.get("/?return=https://news.example.org/premium/story")
    assert resp.status_code == 302 and "/s/news?return=" in resp.location
    chooser = https_client(client.application).get("/").get_data(as_text=True)
    assert "The Long Read" in chooser and "Daily Brief" in chooser


def test_post_without_csrf_is_rejected(client):
    assert client.post("/s/blog/code", data={"code": "X"}).status_code == 400


def test_one_time_checkout_opens_only_its_site(app, client, stripe):
    client.get("/s/blog?return=https://blog.example.com/post/1")
    resp = client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "one_time")})
    assert resp.status_code == 303 and resp.location.startswith("https://checkout.stripe.com/")
    params = stripe.created[-1]
    assert params["mode"] == "payment" and params["line_items"][0]["price_data"]["unit_amount"] == 900
    assert params["metadata"]["sites"] == "blog"
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    payload = follow_handoff(client.get(f"/checkout/success?session_id={sid}").location)
    assert payload["r"] == "https://blog.example.com/post/1"
    conn = conn_for(app)
    assert access.get_customer(conn, payload["sub"])["email"] == "buyer@example.com"
    assert access.access_until(conn, payload["sub"], "blog") == (True, None)
    assert access.access_until(conn, payload["sub"], "news") == (False, None)
    # Coming back to the same site: immediate handoff
    resp = client.get("/s/blog?return=https://blog.example.com/other")
    assert follow_handoff(resp.location)["r"] == "https://blog.example.com/other"
    # The other site still shows its paywall
    assert client.get("/s/news?return=https://news.example.org/premium/x").status_code == 200


def test_offer_from_another_site_cannot_be_bought_here(app, client):
    resp = client.post("/s/news/checkout", data={"csrf": csrf(client), "plan": offer(app, "one_time")})
    assert resp.status_code == 400


def test_bundle_subscription_opens_both_sites(app, client, stripe):
    client.get("/s/news?return=https://news.example.org/premium/today")
    client.post("/s/news/checkout", data={"csrf": csrf(client, "/s/news?stay=1"), "plan": offer(app, "subscription")})
    assert stripe.created[-1]["subscription_data"]["metadata"]["sites"] == "blog,news"
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    payload = follow_handoff(client.get(f"/checkout/success?session_id={sid}").location,
                             host="news.example.org", base="/premium", site="news")
    conn = conn_for(app)
    for site_id in ("blog", "news"):
        has, until = access.access_until(conn, payload["sub"], site_id)
        assert has and until > time.time() + 29 * 86400
    # Going to the blog now hands off straight away
    follow_handoff(client.get("/s/blog?return=https://blog.example.com/").location)

    # Cancellation webhook ends access on both
    sub = dict(stripe.subscriptions["sub_1"], status="canceled", ended_at=int(time.time()) - 1)
    body = json.dumps({"id": "evt_1", "type": "customer.subscription.deleted", "data": {"object": sub}}).encode()
    resp = client.post("/webhook/stripe", data=body, headers=signed(body))
    assert resp.status_code == 200 and resp.json["result"] == "synced"
    assert access.access_by_site(conn, payload["sub"]) == {}


def test_webhook_rejects_bad_signature(client):
    resp = client.post("/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=00"})
    assert resp.status_code == 400


def test_webhook_fulfills_checkout_without_browser(app, client, stripe):
    client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "pass")})
    sid = next(iter(stripe.sessions))
    session = stripe.complete(sid)
    body = json.dumps({"id": "evt", "type": "checkout.session.completed", "data": {"object": session}}).encode()
    assert client.post("/webhook/stripe", data=body, headers=signed(body)).json["result"] == "fulfilled"
    has, until = access.access_until(conn_for(app), session["metadata"]["paywall_customer"], "blog")
    assert has and until <= time.time() + 24 * 3600 + 5


def test_free_code_opens_its_linked_sites(app, client):
    conn = conn_for(app)
    codes.create(conn, code="VIP", kind="free", grant_days=7, max_uses=5, sites=["blog", "news"])
    client.get("/s/blog?return=https://blog.example.com/x")
    payload = follow_handoff(client.post("/s/blog/code", data={"csrf": csrf(client), "code": "vip"}).location)
    for site_id in ("blog", "news"):
        has, until = access.access_until(conn, payload["sub"], site_id)
        assert has and 6 * 86400 < until - time.time() <= 7 * 86400
    assert codes.get(conn, "VIP")["uses"] == 1
    client.post("/s/blog/code", data={"csrf": csrf(client), "code": "VIP"})
    assert codes.get(conn, "VIP")["uses"] == 1  # same visitor cannot redeem twice


def test_code_for_one_site_is_refused_on_another(app, client):
    codes.create(conn_for(app), code="NEWSFREE", kind="free", sites=["news"])
    client.post("/s/blog/code", data={"csrf": csrf(client), "code": "NEWSFREE"})
    assert "not valid on this site" in client.get("/s/blog?stay=1").get_data(as_text=True)


def test_discount_code_applies_to_checkout(app, client, stripe):
    conn = conn_for(app)
    codes.create(conn, code="HALF", kind="percent", value=50)
    client.post("/s/blog/code", data={"csrf": csrf(client), "code": "HALF"})
    html = client.get("/s/blog?stay=1").get_data(as_text=True)
    assert "<s>$9</s>" in html and "Pay $4.50" in html
    client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "one_time")})
    assert stripe.created[-1]["discounts"] == [{"coupon": "coupon_1"}]
    assert stripe.coupons[0]["percent_off"] == 50
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    client.get(f"/checkout/success?session_id={sid}")
    assert codes.get(conn, "HALF")["uses"] == 1


def test_subscription_discount_explains_later_price(app, client):
    codes.create(conn_for(app), code="FIRST", kind="percent", value=50, plans=offer(app, "subscription"))
    client.post("/s/blog/code", data={"csrf": csrf(client), "code": "FIRST"})
    html = client.get("/s/blog?stay=1").get_data(as_text=True)
    assert "first month, then $4" in html and "Subscribe for $2" in html


def test_full_discount_on_one_off_offer_skips_payment(app, client, stripe):
    conn = conn_for(app)
    codes.create(conn, code="ALLFREE", kind="percent", value=100, plans=offer(app, "one_time"))
    client.get("/s/blog?return=https://blog.example.com/")
    client.post("/s/blog/code", data={"csrf": csrf(client), "code": "ALLFREE"})
    resp = client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "one_time")})
    payload = follow_handoff(resp.location)
    assert access.access_until(conn, payload["sub"], "blog") == (True, None)
    assert not stripe.created


def test_wrong_codes_are_rate_limited(client):
    for _ in range(11):
        client.post("/s/blog/code", data={"csrf": csrf(client), "code": "NOPE"})
    assert "Too many attempts" in client.get("/s/blog?stay=1").get_data(as_text=True)


def test_pay_what_you_want_minimum(app, client, stripe):
    resp = client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "pwyw"), "amount": "1"})
    assert resp.status_code == 302 and not stripe.created
    client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "pwyw"), "amount": "12.50"})
    assert stripe.created[-1]["line_items"][0]["price_data"]["unit_amount"] == 1250


def test_admin_requires_sso_header(client):
    assert client.get("/admin/").status_code == 403
    resp = client.get("/admin/", headers={"Ynh-User": "alice"})
    assert resp.status_code == 200 and "Setup" in resp.get_data(as_text=True)


def admin_csrf(client, path="/admin/codes"):
    html = client.get(path, headers={"Ynh-User": "alice"}).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_admin_creates_a_site_and_a_bundle_offer(app, client):
    headers = {"Ynh-User": "alice"}
    token = admin_csrf(client, "/admin/sites/new")
    resp = client.post("/admin/sites", headers=headers, data={
        "csrf": token, "id": "pods", "name": "Podcasts", "title": "Pod Club", "routes": "pods.example.com/*",
        "accent_color": "#f5e663", "recheck_hours": "6", "active": "1", "show_codes": "1",
    })
    assert resp.status_code == 302
    site = sites.get(conn_for(app), "pods")
    assert site.title == "Pod Club" and site.recheck_hours == 6
    resp = client.post("/admin/offers", headers=headers, data={
        "csrf": token, "name": "Everything", "kind": "one_time", "price": "20", "active": "1",
        "sites": ["blog", "news", "pods"],
    })
    assert resp.status_code == 302
    html = client.get("/s/pods").get_data(as_text=True)
    assert "Everything" in html and "Also opens" in html
    assert "--accent-ink: #111827" in html  # readable text on a light button
    # same route twice is refused
    resp = client.post("/admin/sites", headers=headers, data={
        "csrf": token, "id": "dup", "title": "x", "routes": "pods.example.com/*", "active": "1"})
    assert resp.status_code == 400


def test_admin_links_codes_and_grants_to_several_sites(app, client):
    headers = {"Ynh-User": "alice"}
    token = admin_csrf(client)
    resp = client.post("/admin/codes", headers=headers, data={
        "csrf": token, "kind": "free", "grant_days": "30", "count": "3", "sites": ["blog", "news"],
    })
    assert resp.status_code == 302
    conn = conn_for(app)
    created = codes.list_all(conn)
    assert len(created) == 3
    assert all(codes.site_ids(conn, c["code"]) == ["blog", "news"] for c in created)
    resp = client.post("/admin/grant", headers=headers, data={
        "csrf": token, "email": "friend@example.com", "sites": ["news"]})
    assert resp.status_code == 302
    friend = access.find_by_email(conn, "friend@example.com")
    assert access.access_by_site(conn, friend["id"]) == {"news": None}
    # a grant without any site is refused
    client.post("/admin/grant", headers=headers, data={"csrf": token, "email": "nobody@example.com"})
    assert access.find_by_email(conn, "nobody@example.com") is None


def test_magic_link_login(app, client, monkeypatch):
    conn = conn_for(app)
    customer = access.create_customer(conn, "member@example.com")
    access.grant(conn, customer["id"], "manual", sites=["blog"])
    sent = {}
    monkeypatch.setattr("paywall.mailer.send_login_link",
                        lambda cfg, to, link, title: sent.update(to=to, link=link, title=title))
    client.get("/s/blog?return=https://blog.example.com/a")
    client.post("/login", data={"csrf": csrf(client, "/login"), "email": "member@example.com"})
    assert sent["to"] == "member@example.com" and sent["title"] == "The Long Read"
    link = urlsplit(sent["link"])
    assert follow_handoff(client.get(f"{link.path}?{link.query}").location)["sub"] == customer["id"]
    assert client.get(f"{link.path}?{link.query}").status_code == 400  # single use


def test_account_lists_access_per_site(app, client):
    conn = conn_for(app)
    codes.create(conn, code="NEWS7", kind="free", grant_days=7, sites=["news"])
    client.post("/s/news/code", data={"csrf": csrf(client, "/s/news?stay=1"), "code": "NEWS7"})
    html = client.get("/account").get_data(as_text=True)
    assert "Open Daily Brief" in html and "Open The Long Read" not in html


def test_success_page_does_not_log_in_someone_else(app, client, stripe):
    client.post("/s/blog/checkout", data={"csrf": csrf(client), "plan": offer(app, "one_time")})
    sid = next(iter(stripe.sessions))
    stripe.complete(sid)
    other = https_client(app)
    other.get("/s/blog")
    with other.session_transaction() as sess:
        sess["customer"] = access.create_customer(conn_for(app))["id"]
    resp = other.get(f"/checkout/success?session_id={sid}")
    assert resp.status_code == 200 and "Sign in" in resp.get_data(as_text=True)


def test_every_admin_page_renders_with_data(app, client):
    conn = conn_for(app)
    codes.create(conn, code="HALF", kind="percent", value=50, plans=offer(app, "one_time"), sites=["blog"])
    codes.create(conn, code="FREE", kind="free", sites=["news"])
    customer = access.create_customer(conn, "someone@example.com")
    access.grant(conn, customer["id"], "manual", sites=["blog", "news"])
    headers = {"Ynh-User": "alice"}
    for path in ["/admin/", "/admin/sites", "/admin/sites/new", "/admin/sites/blog", "/admin/sites/new?copy=news",
                 "/admin/offers", "/admin/offers/new", f"/admin/offers/{offer(app, 'subscription')}",
                 "/admin/codes", "/admin/codes.csv", "/admin/customers", f"/admin/customers/{customer['id']}",
                 "/admin/cloudflare", "/admin/cloudflare/wrangler.toml"]:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, path
    assert "Lifetime access" in client.get("/admin/codes", headers=headers).get_data(as_text=True)


def test_admin_accepts_the_sso_basic_auth_user(client):
    # SSOwat sends YNH_USER (dropped by gunicorn: underscores) and Basic auth.
    import base64
    basic = base64.b64encode(b"alice:-").decode()
    assert client.get("/admin/", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_fresh_install_root_points_admins_to_setup(tmp_path):
    fresh = create_app(make_config(tmp_path))
    html = https_client(fresh).get("/").get_data(as_text=True)
    assert "The paywall is running" in html and "Open the admin pages" in html
