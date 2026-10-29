"""Public paywall pages, checkout and webhooks."""

import hmac
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import access, codes, config, db, legacy, mailer, offers, payments, plans, sites, tokens
from .stripe_client import StripeError, verify_webhook

log = logging.getLogger(__name__)

LOGIN_TOKEN_TTL = 30 * 60
CSRF_EXEMPT = {"stripe_webhook", "healthz"}


class _PrefixMiddleware:
    """nginx strips the app path before proxying; restore it for url_for()."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
        if environ.get("HTTP_X_FORWARDED_PROTO") == "https":
            environ["wsgi.url_scheme"] = "https"
        return self.wsgi_app(environ, start_response)


def cfg():
    return current_app.extensions["paywall_config"]


def get_db():
    if "db" not in g:
        g.db = db.connect(cfg()["data_dir"])
    return g.db


def stripe():
    factory = current_app.extensions.get("stripe_factory")
    return factory(cfg()) if factory else payments.client(cfg())


def client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


def rate_limited(kind, limit, window):
    conn = get_db()
    now = access.now()
    conn.execute("DELETE FROM attempts WHERE created_at < ?", (now - 86400,))
    count = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE ip = ? AND kind = ? AND created_at > ?", (client_ip(), kind, now - window)
    ).fetchone()[0]
    return count >= limit


def record_attempt(kind):
    get_db().execute("INSERT INTO attempts (ip, kind, created_at) VALUES (?, ?, ?)", (client_ip(), kind, access.now()))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def current_customer():
    customer = access.get_customer(get_db(), session.get("customer"))
    if customer is None:
        session.pop("customer", None)
    return customer


def login(customer_id):
    session.permanent = True
    session["customer"] = customer_id


def ensure_customer():
    customer = current_customer()
    if customer is None:
        customer = access.create_customer(get_db())
        login(customer["id"])
    return customer


def site_or_404(site_id):
    site = sites.get(get_db(), site_id)
    if site is None or not site.active:
        abort(404)
    g.site = site
    session["site"] = site.id
    return site


def session_site():
    site = sites.get(get_db(), session.get("site"))
    return site if site is not None and site.active else None


def remember_return(site):
    target = request.args.get("return")
    if target and site.match(target) is not None:
        session["return"] = target


def return_target(site):
    target = session.get("return")
    if target and site.match(target) is not None:
        return target
    return site.default_return()


def applied_code(site, plan_id=None):
    code = session.get("code")
    if not code:
        return None
    try:
        return codes.check(get_db(), code, plan_id, site.id)
    except codes.CodeError:
        return None


def handoff(customer_id, site):
    """Send a customer with access to ``site`` back to it."""
    conf = cfg()
    has_access, until = access.access_until(get_db(), customer_id, site.id)
    if not has_access:
        return redirect(url_for("site_page", site_id=site.id))
    target = return_target(site)
    if not target:
        return redirect(url_for("account"))
    base = site.match(target) or ""
    session.pop("return", None)
    access_exp = access.now() + site.recheck_hours * 3600
    if until is not None:
        access_exp = min(access_exp, until)
    token = tokens.handoff(conf["signing_secret"], customer_id, site.id, access_exp, target)
    host = urlsplit(target).netloc
    return redirect(f"https://{host}{base}{conf['callback_prefix']}/auth?{urlencode({'t': token})}")


def offer_list(site, code_row):
    """What each stub on the ticket shows: the big number, the line under it
    and a button label that names the exact charge."""
    conf = cfg()

    def money(amount):
        return plans.format_money(amount, conf)

    result = []
    for plan in offers.list_all(get_db(), site_id=site.id, active_only=True):
        applies = code_row is not None and not plan.custom_amount and (
            not codes.plan_ids(code_row) or plan.id in codes.plan_ids(code_row)
        )
        amount = codes.discounted(code_row, plan.amount) if applies else plan.amount
        discounted = applies and amount != plan.amount
        free = applies and amount == 0 and not plan.is_subscription
        if plan.custom_amount:
            unit, label = "or more, you choose", "Pay this amount"
        elif plan.is_subscription:
            unit = f"per {plan.interval}"
            if discounted and code_row["duration"] == "once":
                unit = f"first {plan.interval}, then {money(plan.amount)}"
            elif discounted and code_row["duration"] == "repeating":
                unit = f"per {plan.interval} for {code_row['duration_months']} months, then {money(plan.amount)}"
            label = f"Start {plan.trial_days}-day free trial" if plan.trial_days else f"Subscribe for {money(amount)}"
        else:
            unit = "once"
            label = f"Get it free with {code_row['code']}" if free else f"Pay {money(amount)}"
        others = [sites_by_id().get(s) for s in plan.site_ids if s != site.id]
        result.append({
            "plan": plan,
            "big": "Free" if free else money(amount),
            "was": money(plan.amount) if discounted else None,
            "unit": unit,
            "label": label,
            "free": free,
            "terms": plans.describe(plan, conf),
            "also": [o.title for o in others if o is not None and o.active],
            "suggested_value": _minor_to_str(plan.suggested, conf) if plan.custom_amount else None,
        })
    return result


def sites_by_id():
    if "sites_by_id" not in g:
        g.sites_by_id = {s.id: s for s in sites.list_all(get_db())}
    return g.sites_by_id


def _minor_to_str(amount, conf):
    return str(amount) if conf.zero_decimal else f"{amount / 100:.2f}"


def _parse_amount(raw, conf):
    try:
        value = Decimal((raw or "").strip().replace(",", "."))
    except InvalidOperation:
        return None
    if value <= 0 or value > 100000:
        return None
    return plans.to_minor(value, conf)


def create_app(conf=None, stripe_factory=None):
    conf = conf or config.load()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=conf["flask_secret"],
        SESSION_COOKIE_NAME="paywall_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=conf["session_days"]),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.extensions["paywall_config"] = conf
    if stripe_factory:
        app.extensions["stripe_factory"] = stripe_factory
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, conf["path"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with app.app_context():
        conn = db.connect(conf["data_dir"])
        db.migrate(conn)
        legacy.import_settings(conn, conf)
        conn.close()

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def protect_forms():
        if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Your session expired. Go back, reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.context_processor
    def inject():
        brand = g.get("site") or (session_site() if request.endpoint and not request.endpoint.startswith("admin.")
                                  else None)
        return {
            "cfg": cfg(), "csrf_token": csrf_token, "money": lambda a: plans.format_money(a, cfg()),
            "brand": brand or sites.Site(title="Paywall", accent_color=sites.DEFAULT_ACCENT, support_email="",
                                         id=""),
        }

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("message.html", title="Something went wrong", message=err.description), 400

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return jsonify(ok=True)

    @app.get("/")
    def index():
        conn = get_db()
        target = request.args.get("return")
        site = sites.for_url(conn, target) if target else None
        if site is None and not target:
            site = session_site()
        active = sites.list_all(conn, active_only=True)
        if site is None and len(active) == 1:
            site = active[0]
        if site is not None:
            return redirect(url_for("site_page", site_id=site.id, **({"return": target} if target else {})))
        return render_template("sites.html", sites=active)

    @app.get("/s/<site_id>")
    def site_page(site_id):
        site = site_or_404(site_id)
        remember_return(site)
        conn = get_db()
        customer = current_customer()
        has_access = False
        if customer:
            try:
                payments.refresh_lapsed(conn, cfg(), customer["id"])
            except StripeError:
                pass
            has_access = access.access_until(conn, customer["id"], site.id)[0]
            if has_access and request.args.get("stay") != "1" and return_target(site):
                return handoff(customer["id"], site)
        code_row = applied_code(site)
        return render_template(
            "index.html",
            site=site,
            offers=offer_list(site, code_row),
            code=code_row,
            code_summary=codes.summary(code_row, lambda a: plans.format_money(a, cfg())) if code_row else None,
            customer=customer,
            has_access=has_access,
            payments_ready=cfg().stripe_ready,
        )

    @app.post("/s/<site_id>/code")
    def redeem_code(site_id):
        site = site_or_404(site_id)
        conn = get_db()
        back = redirect(url_for("site_page", site_id=site.id, stay=1))
        if rate_limited("code", 10, 900):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return back
        try:
            row = codes.check(conn, request.form.get("code", ""), site_id=site.id)
        except codes.CodeError as exc:
            record_attempt("code")
            flash(str(exc), "error")
            return back
        if row["kind"] != "free":
            session["code"] = row["code"]
            return back
        customer = ensure_customer()
        if codes.already_redeemed(conn, row["code"], customer["id"]):
            flash("You already used this code.", "error")
            return back
        with db.transaction(conn):
            payments.grant_free(conn, cfg(), row, customer)
        flash("Code accepted, enjoy!", "success")
        return handoff(customer["id"], site)

    @app.post("/s/<site_id>/code/clear")
    def clear_code(site_id):
        site = site_or_404(site_id)
        session.pop("code", None)
        return redirect(url_for("site_page", site_id=site.id, stay=1))

    @app.post("/s/<site_id>/checkout")
    def checkout(site_id):
        site = site_or_404(site_id)
        conn = get_db()
        conf = cfg()
        back = redirect(url_for("site_page", site_id=site.id, stay=1))
        plan = offers.get(conn, request.form.get("plan", ""))
        if plan is None or not plan.active or site.id not in plan.site_ids:
            abort(400, "This option is not available.")
        custom_amount = None
        if plan.custom_amount:
            custom_amount = _parse_amount(request.form.get("amount"), conf)
            if custom_amount is None or custom_amount < plan.amount:
                flash(f"Please enter at least {plans.format_money(plan.amount, conf)}.", "error")
                return back
        code_row = None if plan.custom_amount else applied_code(site, plan.id)
        customer = ensure_customer()
        if code_row and not plan.is_subscription and codes.discounted(code_row, plan.amount) == 0:
            # 100 % discount on a one-off offer: no payment needed.
            context = f"discount:{code_row['code']}:{customer['id']}:{plan.id}"
            with db.transaction(conn):
                if codes.record_use(conn, code_row["code"], customer["id"], context):
                    expires = access.now() + plan.access_seconds if plan.access_seconds else None
                    access.grant(conn, customer["id"], plan.kind, expires, context, 0, conf["currency"],
                                 code_row["code"], sites=plan.site_ids, offer_id=int(plan.id))
            session.pop("code", None)
            return handoff(customer["id"], site)
        try:
            url = payments.create_checkout(
                conn, conf, plan, customer, site, code_row=code_row, custom_amount=custom_amount,
                return_url=return_target(site), stripe=stripe(),
            )
        except (payments.PaymentError, StripeError) as exc:
            log.warning("Checkout failed: %s", exc)
            flash(f"Could not start the payment: {exc}", "error")
            return back
        return redirect(url, 303)

    @app.get("/checkout/success")
    def checkout_success():
        conn = get_db()
        session_id = request.args.get("session_id", "")
        if not session_id.startswith("cs_"):
            abort(400, "Missing payment reference.")
        try:
            checkout_session = stripe().get_checkout_session(session_id)
        except StripeError as exc:
            log.warning("Could not fetch checkout session %s: %s", session_id, exc)
            return render_template(
                "message.html", title="Payment received?",
                message="We could not confirm your payment right now. If you were charged, your access will be "
                        "activated within a few minutes; reload this page or sign in with your email.",
            )
        with db.transaction(conn):
            customer_id = payments.fulfill_checkout(conn, cfg(), checkout_session)
        if not customer_id:
            return render_template(
                "message.html", title="Payment pending",
                message="Your payment is not confirmed yet. Your access will be activated as soon as it is.",
            )
        current = session.get("customer")
        recent = (checkout_session.get("created") or 0) > access.now() - 3600
        if current != customer_id and (current or not recent):
            return render_template(
                "message.html", title="Payment received",
                message="Thank you! Sign in with the email you used at checkout to access the site.",
                link=url_for("login_page"), link_label="Sign in",
            )
        login(customer_id)
        session.pop("code", None)
        metadata = checkout_session.get("metadata") or {}
        site = sites.get(conn, metadata.get("site"))
        if site is None or not site.active:
            return redirect(url_for("account"))
        session["site"] = site.id
        fallback = metadata.get("return_url")
        if not session.get("return") and fallback and site.match(fallback) is not None:
            session["return"] = fallback
        return handoff(customer_id, site)

    @app.get("/webhook/stripe")
    def stripe_webhook_info():
        # Stripe only POSTs here; tell people who open the URL in a browser
        # that this is expected instead of showing "Method Not Allowed".
        return render_template(
            "message.html", title="Stripe webhook endpoint",
            message="This address is for Stripe only: it receives payment notifications as POST requests. "
                    "Opening it in a browser does nothing, and that is expected. Paste this URL in Stripe "
                    "(Developers, Webhooks) and the signing secret in the app's config panel.",
        )

    @app.post("/webhook/stripe")
    def stripe_webhook():
        try:
            event = verify_webhook(
                request.get_data(), request.headers.get("Stripe-Signature", ""), cfg()["stripe_webhook_secret"]
            )
        except (StripeError, ValueError) as exc:
            log.warning("Rejected webhook: %s", exc)
            return jsonify(error=str(exc)), 400
        try:
            result = payments.handle_event(get_db(), cfg(), event, stripe())
        except StripeError as exc:
            log.error("Webhook %s failed: %s", event.get("type"), exc)
            return jsonify(error=str(exc)), 502
        log.info("Webhook %s %s: %s", event.get("type"), event.get("id"), result)
        return jsonify(received=True, result=result)

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        conf = cfg()
        if request.method == "GET":
            return render_template("login.html")
        if not conf["enable_email_login"]:
            abort(400, "Email sign-in is disabled.")
        email = request.form.get("email", "").strip()
        if "@" not in email or len(email) > 254:
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("login_page"))
        if rate_limited("login", 5, 3600):
            flash("Too many requests. Please try again later.", "error")
            return redirect(url_for("login_page"))
        record_attempt("login")
        conn = get_db()
        owner = access.find_by_email(conn, email)
        current = current_customer()
        if owner is not None:
            target, new_email = owner["id"], None
        elif current is not None and not current["email"]:
            target, new_email = current["id"], email
        else:
            flash("We could not find any purchase for this email address.", "error")
            return redirect(url_for("login_page"))
        token = tokens.random_token()
        conn.execute(
            "INSERT INTO login_tokens (token_hash, customer_id, email, expires_at) VALUES (?, ?, ?, ?)",
            (tokens.hash_token(token), target, new_email, access.now() + LOGIN_TOKEN_TTL),
        )
        link = f"{conf.public_url}/login/verify?{urlencode({'t': token})}"
        site = session_site()
        try:
            mailer.send_login_link(conf, email, link, site.title if site else "the paywall")
        except OSError as exc:
            log.error("Could not send email to %s: %s", email, exc)
            flash("We could not send the email. Please contact the site owner.", "error")
            return redirect(url_for("login_page"))
        return render_template(
            "message.html", title="Check your inbox",
            message=f"We sent a sign-in link to {email}. It is valid for 30 minutes.",
        )

    @app.get("/login/verify")
    def login_verify():
        conn = get_db()
        token_hash = tokens.hash_token(request.args.get("t", ""))
        with db.transaction(conn):
            row = conn.execute(
                "SELECT * FROM login_tokens WHERE token_hash = ? AND used = 0 AND expires_at > ?",
                (token_hash, access.now()),
            ).fetchone()
            if row is None:
                abort(400, "This sign-in link is invalid or has expired. Request a new one.")
            conn.execute("UPDATE login_tokens SET used = 1 WHERE token_hash = ?", (token_hash,))
            target = row["customer_id"]
            if row["email"]:
                access.attach_details(conn, target, row["email"])
            current = session.get("customer")
            if current and current != target:
                access.merge(conn, current, target)
        login(target)
        flash("You are signed in.", "success")
        site = session_site()
        if site and access.access_until(conn, target, site.id)[0] and return_target(site):
            return handoff(target, site)
        return redirect(url_for("account"))

    @app.get("/account")
    def account():
        customer = current_customer()
        if customer is None:
            return redirect(url_for("login_page"))
        conn = get_db()
        try:
            payments.refresh_lapsed(conn, cfg(), customer["id"])
        except StripeError:
            pass
        by_site = access.access_by_site(conn, customer["id"])
        entitlements = access.entitlements_for(conn, customer["id"])
        return render_template(
            "account.html", customer=customer,
            open_sites=[(s, by_site[s.id]) for s in sites.list_all(conn, active_only=True) if s.id in by_site],
            entitlements=entitlements, site_names={s.id: s.title for s in sites.list_all(conn)},
            can_manage=bool(customer["stripe_customer_id"] and cfg().stripe_ready
                            and any(e["kind"] == "subscription" for e in entitlements)),
            now=access.now(),
        )

    @app.get("/go/<site_id>")
    def go(site_id):
        site = site_or_404(site_id)
        customer = current_customer()
        if customer is None:
            return redirect(url_for("site_page", site_id=site.id))
        return handoff(customer["id"], site)

    @app.post("/account/portal")
    def billing_portal():
        customer = current_customer()
        if customer is None or not customer["stripe_customer_id"]:
            return redirect(url_for("account"))
        try:
            portal = stripe().create_portal_session(customer["stripe_customer_id"], f"{cfg().public_url}/account")
        except StripeError as exc:
            flash(f"Could not open the billing portal: {exc}", "error")
            return redirect(url_for("account"))
        return redirect(portal["url"], 303)

    @app.post("/logout")
    def logout():
        site = session_site()
        session.clear()
        flash("You are signed out of the paywall.", "success")
        target = site.default_return() if site else None
        if target:
            parts = urlsplit(target)
            base = site.match(target) or ""
            return redirect(f"https://{parts.netloc}{base}{cfg()['callback_prefix']}/logout")
        return redirect(url_for("index"))

    from .admin import bp as admin_bp

    app.register_blueprint(admin_bp)
    return app
