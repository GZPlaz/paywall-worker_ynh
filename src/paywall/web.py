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

from . import access, codes, config, db, mailer, payments, plans, tokens
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


def remember_return():
    target = request.args.get("return")
    if target and cfg().is_allowed_return(target):
        session["return"] = target


def return_target():
    target = session.get("return")
    if target and cfg().is_allowed_return(target):
        return target
    return cfg().default_return()


def applied_code(plan_id=None):
    code = session.get("code")
    if not code:
        return None
    try:
        return codes.check(get_db(), code, plan_id)
    except codes.CodeError:
        if plan_id is None:
            session.pop("code", None)
        return None


def handoff(customer_id):
    """Send a customer with access back to the protected site."""
    conf = cfg()
    has_access, until = access.access_until(get_db(), customer_id)
    if not has_access:
        return redirect(url_for("index"))
    target = return_target()
    if not target:
        return redirect(url_for("account"))
    session.pop("return", None)
    access_exp = access.now() + conf["access_recheck_hours"] * 3600
    if until is not None:
        access_exp = min(access_exp, until)
    token = tokens.handoff(conf["signing_secret"], customer_id, access_exp, target)
    host = urlsplit(target).netloc
    return redirect(f"https://{host}{conf['callback_prefix']}/auth?{urlencode({'t': token})}")


def offer_list(code_row):
    conf = cfg()
    offers = []
    for plan in plans.build(conf):
        applies = code_row is not None and not plan.custom_amount and (
            not codes.plan_ids(code_row) or plan.id in codes.plan_ids(code_row)
        )
        discounted = codes.discounted(code_row, plan.amount) if applies else None
        offers.append({
            "plan": plan,
            "price": plans.price_label(plan, conf),
            "discounted": plans.price_label(plan, conf, discounted) if applies else None,
            "free": applies and discounted == 0 and not plan.is_subscription,
            "description": plans.describe(plan, conf),
            "suggested_value": _minor_to_str(plan.suggested, conf) if plan.custom_amount else None,
        })
    return offers


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
        return {"cfg": cfg(), "csrf_token": csrf_token, "money": lambda a: plans.format_money(a, cfg())}

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("message.html", title="Something went wrong", message=err.description), 400

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return jsonify(ok=True)

    @app.get("/")
    def index():
        remember_return()
        customer = current_customer()
        if customer:
            try:
                payments.refresh_lapsed(get_db(), cfg(), customer["id"])
            except StripeError:
                pass
            stay = request.args.get("stay") == "1"
            if not stay and access.access_until(get_db(), customer["id"])[0] and return_target():
                return handoff(customer["id"])
        code_row = applied_code()
        return render_template(
            "index.html",
            offers=offer_list(code_row),
            code=code_row,
            code_summary=codes.summary(code_row, lambda a: plans.format_money(a, cfg())) if code_row else None,
            customer=customer,
            has_access=bool(customer and access.access_until(get_db(), customer["id"])[0]),
            payments_ready=cfg().stripe_ready,
        )

    @app.post("/code")
    def redeem_code():
        conn = get_db()
        if rate_limited("code", 10, 900):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return redirect(url_for("index"))
        raw = request.form.get("code", "")
        try:
            row = codes.check(conn, raw)
        except codes.CodeError as exc:
            record_attempt("code")
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if row["kind"] != "free":
            session["code"] = row["code"]
            flash(f"Code {row['code']} applied: {codes.summary(row, lambda a: plans.format_money(a, cfg()))}.", "success")
            return redirect(url_for("index"))
        customer = ensure_customer()
        if codes.already_redeemed(conn, row["code"], customer["id"]):
            flash("You already used this code.", "error")
            return redirect(url_for("index"))
        with db.transaction(conn):
            payments.grant_free(conn, cfg(), row, customer)
        flash("Code accepted, enjoy!", "success")
        return handoff(customer["id"])

    @app.post("/code/clear")
    def clear_code():
        session.pop("code", None)
        return redirect(url_for("index"))

    @app.post("/checkout")
    def checkout():
        conn = get_db()
        conf = cfg()
        plan = plans.get(conf, request.form.get("plan", ""))
        if plan is None:
            abort(400, "This option is not available.")
        custom_amount = None
        if plan.custom_amount:
            custom_amount = _parse_amount(request.form.get("amount"), conf)
            if custom_amount is None or custom_amount < plan.amount:
                flash(f"Please enter at least {plans.format_money(plan.amount, conf)}.", "error")
                return redirect(url_for("index"))
        code_row = None if plan.custom_amount else applied_code(plan.id)
        customer = ensure_customer()
        if code_row and not plan.is_subscription and codes.discounted(code_row, plan.amount) == 0:
            # 100 % discount on a one-off plan: no payment needed.
            context = f"discount:{code_row['code']}:{customer['id']}:{plan.id}"
            with db.transaction(conn):
                if codes.record_use(conn, code_row["code"], customer["id"], context):
                    expires = access.now() + plan.access_seconds if plan.access_seconds else None
                    access.grant(conn, customer["id"], plan.id, expires, context, 0, conf["currency"], code_row["code"])
            session.pop("code", None)
            return handoff(customer["id"])
        try:
            url = payments.create_checkout(
                conn, conf, plan, customer, code_row=code_row, custom_amount=custom_amount,
                return_url=return_target(), stripe=stripe(),
            )
        except (payments.PaymentError, StripeError) as exc:
            log.warning("Checkout failed: %s", exc)
            flash(f"Could not start the payment: {exc}", "error")
            return redirect(url_for("index"))
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
        fallback = (checkout_session.get("metadata") or {}).get("return_url")
        if not session.get("return") and fallback and cfg().is_allowed_return(fallback):
            session["return"] = fallback
        session.pop("code", None)
        return handoff(customer_id)

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
        remember_return()
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
        try:
            mailer.send_login_link(conf, email, link)
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
        if access.access_until(conn, target)[0] and return_target():
            return handoff(target)
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
        has_access, until = access.access_until(conn, customer["id"])
        entitlements = access.entitlements_for(conn, customer["id"])
        return render_template(
            "account.html", customer=customer, has_access=has_access, until=until, entitlements=entitlements,
            can_manage=bool(customer["stripe_customer_id"] and cfg().stripe_ready
                            and any(e["kind"] == "subscription" for e in entitlements)),
            site_url=return_target(), now=access.now(),
        )

    @app.get("/go")
    def go():
        customer = current_customer()
        remember_return()
        if customer is None:
            return redirect(url_for("index"))
        return handoff(customer["id"])

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
        session.clear()
        flash("You are signed out of the paywall.", "success")
        target = cfg().default_return()
        if target:
            host = urlsplit(target).netloc
            return redirect(f"https://{host}{cfg()['callback_prefix']}/logout")
        return redirect(url_for("index"))

    from .admin import bp as admin_bp

    app.register_blueprint(admin_bp)
    return app
