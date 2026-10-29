"""Admin pages (protected by the YunoHost SSO through the ``admin`` permission)."""

import time
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import access, cloudflare, codes, db, plans
from .web import cfg, get_db

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def require_admin():
    # SSOwat only lets members of the app's "admin" permission reach /admin and
    # then passes the user name along. Refuse anything that did not go
    # through it (e.g. the port reached directly).
    user = request.headers.get("Ynh-User") or request.headers.get("Remote-User")
    if not user and not current_app.config.get("ADMIN_WITHOUT_SSO"):
        abort(403)
    g.admin_user = user or "admin"


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86399
    except ValueError:
        return None


def _int_or_none(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


@bp.app_template_filter("dt")
def format_ts(ts):
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + " UTC"


def setup_checklist():
    conf = cfg()
    items = [
        ("Protected routes configured", bool(conf.routes), "Config panel > Paywall > Protected sites"),
        ("At least one paid option enabled", bool(plans.build(conf)), "Config panel > Pricing"),
    ]
    if conf["payment_provider"] == "stripe":
        items += [
            ("Stripe secret key set", bool(conf["stripe_secret_key"]), "Config panel > Payments"),
            ("Stripe webhook secret set", bool(conf["stripe_webhook_secret"]), "Config panel > Payments"),
        ]
    items.append(("Cloudflare API token and account ID set", conf.cloudflare_ready, "Config panel > Cloudflare"))
    return items


@bp.get("/")
def dashboard():
    conn = get_db()
    return render_template(
        "admin/dashboard.html",
        stats=access.stats(conn),
        checklist=setup_checklist(),
        cf_status=cloudflare.read_status(cfg()),
        plans=[(p, plans.price_label(p, cfg()), plans.describe(p, cfg())) for p in plans.build(cfg())],
        recent=conn.execute(
            "SELECT e.*, c.email FROM entitlements e JOIN customers c ON c.id = e.customer_id "
            "ORDER BY e.created_at DESC LIMIT 15"
        ).fetchall(),
        now=access.now(),
    )


@bp.get("/codes")
def codes_page():
    return render_template(
        "admin/codes.html",
        codes=codes.list_all(get_db()),
        plans=plans.build(cfg()),
        summary=lambda row: codes.summary(row, lambda a: plans.format_money(a, cfg())),
        now=access.now(),
    )


@bp.post("/codes")
def create_code():
    form = request.form
    kind = form.get("kind", "percent")
    value = 0
    if kind == "percent":
        value = _int_or_none(form.get("percent")) or 0
    elif kind == "fixed":
        amount = (form.get("amount") or "0").strip() or "0"
        try:
            value = plans.to_minor(amount, cfg())
        except Exception:
            value = 0
    try:
        count = max(1, min(500, _int_or_none(form.get("count")) or 1))
        created = []
        for _ in range(count):
            row = codes.create(
                get_db(),
                code=form.get("code") if count == 1 else None,
                kind=kind,
                value=value,
                plans=",".join(form.getlist("plans")),
                max_uses=_int_or_none(form.get("max_uses")),
                expires_at=_parse_date(form.get("expires")),
                grant_days=_int_or_none(form.get("grant_days")),
                duration=form.get("duration", "once"),
                duration_months=_int_or_none(form.get("duration_months")),
                note=(form.get("note") or "").strip() or None,
            )
            created.append(row["code"])
    except codes.CodeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.codes_page"))
    flash(f"Created {len(created)} code(s): {', '.join(created[:20])}{'…' if len(created) > 20 else ''}", "success")
    return redirect(url_for("admin.codes_page"))


@bp.post("/codes/<code>/toggle")
def toggle_code(code):
    row = codes.get(get_db(), code)
    if row is None:
        abort(404)
    codes.set_active(get_db(), code, not row["active"])
    return redirect(url_for("admin.codes_page"))


@bp.post("/codes/<code>/delete")
def delete_code(code):
    codes.delete(get_db(), code)
    flash(f"Deleted code {code}.", "success")
    return redirect(url_for("admin.codes_page"))


@bp.get("/codes.csv")
def export_codes():
    lines = ["code,kind,value,plans,uses,max_uses,expires,active,note"]
    for row in codes.list_all(get_db()):
        note = (row["note"] or "").replace('"', '""')
        lines.append(
            f'{row["code"]},{row["kind"]},{row["value"]},"{row["plans"]}",{row["uses"]},'
            f'{row["max_uses"] or ""},{format_ts(row["expires_at"]) if row["expires_at"] else ""},'
            f'{row["active"]},"{note}"'
        )
    return Response("\n".join(lines) + "\n", mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=paywall-codes.csv"})


@bp.get("/customers")
def customers_page():
    query = request.args.get("q", "")
    return render_template("admin/customers.html", customers=access.search(get_db(), query), query=query)


@bp.get("/customers/<customer_id>")
def customer_page(customer_id):
    conn = get_db()
    customer = access.get_customer(conn, customer_id)
    if customer is None:
        abort(404)
    has_access, until = access.access_until(conn, customer_id)
    return render_template(
        "admin/customer.html", customer=customer, entitlements=access.entitlements_for(conn, customer_id),
        has_access=has_access, until=until, now=access.now(),
    )


@bp.post("/grant")
def grant():
    conn = get_db()
    email = request.form.get("email", "").strip()
    customer_id = request.form.get("customer_id", "").strip()
    days = _int_or_none(request.form.get("days"))
    note = (request.form.get("note") or "").strip() or f"Granted by {g.admin_user}"
    with db.transaction(conn):
        customer = access.get_customer(conn, customer_id) or access.find_by_email(conn, email)
        if customer is None:
            if "@" not in email:
                flash("Enter a valid email address.", "error")
                return redirect(request.referrer or url_for("admin.customers_page"))
            customer = access.create_customer(conn, email)
        expires = access.now() + days * plans.DAY if days else None
        access.grant(conn, customer["id"], "manual", expires, note=note)
    flash(f"Access granted to {customer['email'] or customer['id']}.", "success")
    return redirect(url_for("admin.customer_page", customer_id=customer["id"]))


@bp.post("/entitlements/<int:entitlement_id>/revoke")
def revoke(entitlement_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM entitlements WHERE id = ?", (entitlement_id,)).fetchone()
    if row is None:
        abort(404)
    access.revoke(conn, entitlement_id)
    flash("Access revoked. Note: an active Stripe subscription keeps billing until you cancel it in Stripe.", "success")
    return redirect(url_for("admin.customer_page", customer_id=row["customer_id"]))


@bp.get("/cloudflare")
def cloudflare_page():
    conf = cfg()
    return render_template(
        "admin/cloudflare.html",
        status=cloudflare.read_status(conf),
        worker_vars=cloudflare.worker_vars(conf),
        routes=conf.routes,
    )


@bp.post("/cloudflare/deploy")
def deploy_worker():
    messages = []
    try:
        cloudflare.deploy(cfg(), log=messages.append)
        flash("Worker deployed: " + " / ".join(messages), "success")
    except cloudflare.CloudflareError as exc:
        flash(f"Deployment failed: {exc}", "error")
    return redirect(url_for("admin.cloudflare_page"))


@bp.get("/cloudflare/paywall-worker.js")
def download_worker():
    return Response(cloudflare.worker_source(), mimetype="application/javascript",
                    headers={"Content-Disposition": "attachment; filename=paywall-worker.js"})


@bp.get("/cloudflare/wrangler.toml")
def download_wrangler():
    return Response(cloudflare.wrangler_toml(cfg()), mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=wrangler.toml"})
