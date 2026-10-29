"""Admin pages (protected by the YunoHost SSO through the ``admin`` permission)."""

import time
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import access, cloudflare, codes, db, offers, plans, sites
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


KIND_LABELS = {
    "one_time": "One-time payment",
    "subscription": "Subscription",
    "pass": "Pass",
    "pwyw": "Pay what you want",
    "code": "Access code",
    "manual": "Granted by an admin",
}


@bp.app_template_filter("kind")
def kind_label(kind):
    return KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())


@bp.app_template_filter("dt")
def format_ts(ts):
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + " UTC"


@bp.app_context_processor
def admin_helpers():
    def site_names(ids):
        names = {s.id: s.name for s in sites.list_all(get_db())}
        return ", ".join(names.get(i, i) for i in ids) if ids else "every site"
    return {"site_names": site_names}


def checked_sites():
    """Site ids ticked in the submitted form (only existing ones)."""
    known = set(sites.ids(get_db()))
    return [s for s in request.form.getlist("sites") if s in known]


def saved_and_deploy(message):
    """Flash ``message`` and, when enabled, push the Worker again because
    the routes, public paths or active sites may have changed."""
    conf = cfg()
    if not (conf["cf_auto_deploy"] and conf.cloudflare_ready):
        flash(f"{message} Deploy the Worker (Cloudflare tab) for route changes to take effect.", "success")
        return
    try:
        cloudflare.deploy(conf, get_db(), log=lambda _m: None)
        flash(f"{message} The Cloudflare Worker was updated.", "success")
    except cloudflare.CloudflareError as exc:
        cloudflare.write_status(conf, f"Deployment failed: {exc}")
        flash(f"{message} Updating the Cloudflare Worker failed: {exc}", "error")


def setup_checklist():
    conf = cfg()
    conn = get_db()
    active = sites.list_all(conn, active_only=True)
    items = [
        ("At least one site with routes", any(s.route_list for s in active), url_for("admin.sites_page")),
        ("Every active site has an offer or the code field",
         bool(active) and all(offers.list_all(conn, site_id=s.id, active_only=True) or s.show_codes for s in active),
         url_for("admin.offers_page")),
    ]
    if conf["payment_provider"] == "stripe":
        items += [
            ("Stripe secret key set", bool(conf["stripe_secret_key"]), None),
            ("Stripe webhook secret set", bool(conf["stripe_webhook_secret"]), None),
        ]
    items.append(("Cloudflare API token and account ID set", conf.cloudflare_ready, None))
    return items


# ---------------------------------------------------------------- overview

@bp.get("/")
def dashboard():
    conn = get_db()
    stats = access.stats(conn)
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        checklist=setup_checklist(),
        cf_status=cloudflare.read_status(cfg()),
        site_rows=[
            (s, len(offers.list_all(conn, site_id=s.id, active_only=True)), stats["per_site"].get(s.id, 0))
            for s in sites.list_all(conn)
        ],
        recent=conn.execute(
            """SELECT e.*, c.email, (SELECT GROUP_CONCAT(site_id, ',') FROM entitlement_sites es
                                     WHERE es.entitlement_id = e.id) AS site_list
               FROM entitlements e JOIN customers c ON c.id = e.customer_id
               ORDER BY e.created_at DESC LIMIT 15"""
        ).fetchall(),
        now=access.now(),
    )


# ------------------------------------------------------------------- sites

@bp.get("/sites")
def sites_page():
    conn = get_db()
    return render_template(
        "admin/sites.html",
        sites=sites.list_all(conn),
        offer_counts={s.id: len(offers.list_all(conn, site_id=s.id)) for s in sites.list_all(conn)},
    )


def _site_form(site, values, linked_offers, create):
    return render_template(
        "admin/site_form.html", site=site, values=values, create=create,
        offers=offers.list_all(get_db()), linked_offers=[str(o) for o in linked_offers],
    )


@bp.get("/sites/new")
def new_site():
    copy = sites.get(get_db(), request.args.get("copy", ""))
    values = dict(copy) if copy else {
        "title": "Members only", "accent_color": sites.DEFAULT_ACCENT, "bypass_paths": sites.DEFAULT_BYPASS,
        "recheck_hours": 24, "show_codes": 1, "active": 1,
    }
    if copy:
        values.update(id="", name=f"{copy.name} (copy)", routes="")
    linked = [p.id for p in offers.list_all(get_db(), site_id=copy.id)] if copy else []
    return _site_form(None, values, linked, create=True)


@bp.get("/sites/<site_id>")
def edit_site(site_id):
    conn = get_db()
    site = sites.get(conn, site_id)
    if site is None:
        abort(404)
    return _site_form(site, dict(site), [p.id for p in offers.list_all(conn, site_id=site.id)], create=False)


@bp.post("/sites")
@bp.post("/sites/<site_id>")
def save_site(site_id=None):
    conn = get_db()
    create = site_id is None
    values = request.form.to_dict()
    if not create:
        values["id"] = site_id
    linked = request.form.getlist("offers")
    try:
        with db.transaction(conn):
            site = sites.save(conn, values, cfg()["domain"], offer_ids=linked, create=create)
    except sites.SiteError as exc:
        flash(str(exc), "error")
        return _site_form(sites.get(conn, site_id) if site_id else None, values, linked, create), 400
    saved_and_deploy(f"Site '{site.name}' saved.")
    return redirect(url_for("admin.sites_page"))


@bp.post("/sites/<site_id>/delete")
def delete_site(site_id):
    conn = get_db()
    site = sites.get(conn, site_id)
    if site is None:
        abort(404)
    with db.transaction(conn):
        sites.delete(conn, site_id)
    saved_and_deploy(f"Site '{site.name}' deleted.")
    return redirect(url_for("admin.sites_page"))


# ------------------------------------------------------------------ offers

@bp.get("/offers")
def offers_page():
    conn = get_db()
    all_offers = offers.list_all(conn)
    return render_template(
        "admin/offers.html",
        offers=all_offers,
        holders={p.id: offers.holder_count(conn, p.id) for p in all_offers},
        price=lambda p: plans.price_label(p, cfg()),
        describe=lambda p: plans.describe(p, cfg()),
        has_sites=bool(sites.ids(conn)),
    )


def _offer_values(plan):
    def money(amount):
        if amount is None:
            return ""
        return str(amount) if cfg().zero_decimal else f"{amount / 100:.2f}"

    return {
        "name": plan.name, "kind": plan.kind, "price": money(plan.amount), "suggested": money(plan.suggested),
        "interval": plan.interval or "month", "trial_days": plan.trial_days,
        "access_days": (plan.access_seconds // plans.DAY) if plan.access_seconds and plan.kind != "pass" else "",
        "pass_hours": (plan.access_seconds // 3600) if plan.kind == "pass" and plan.access_seconds else 24,
        "active": plan.active, "position": plan.position,
    }


def _offer_form(plan, values, linked_sites):
    return render_template(
        "admin/offer_form.html", plan=plan, values=values, linked_sites=linked_sites,
        sites=sites.list_all(get_db()), kinds=offers.KINDS, intervals=offers.INTERVALS,
    )


@bp.get("/offers/new")
def new_offer():
    preset = request.args.getlist("site")
    return _offer_form(None, {"kind": "one_time", "interval": "month", "pass_hours": 24, "active": True}, preset)


@bp.get("/offers/<int:offer_id>")
def edit_offer(offer_id):
    plan = offers.get(get_db(), offer_id)
    if plan is None:
        abort(404)
    return _offer_form(plan, _offer_values(plan), plan.site_ids)


@bp.post("/offers")
@bp.post("/offers/<int:offer_id>")
def save_offer(offer_id=None):
    conn = get_db()
    values = request.form.to_dict()
    linked = checked_sites()
    try:
        with db.transaction(conn):
            plan = offers.save(conn, cfg(), values, linked, offer_id=offer_id,
                               extend_holders=bool(request.form.get("extend_holders")))
    except offers.OfferError as exc:
        flash(str(exc), "error")
        return _offer_form(offers.get(conn, offer_id) if offer_id else None, values, linked), 400
    names = {s.id: s.name for s in sites.list_all(conn)}
    flash(f"Offer '{plan.name}' saved. It is sold on and opens: {', '.join(names[s] for s in plan.site_ids)}.",
          "success")
    return redirect(url_for("admin.offers_page"))


@bp.post("/offers/<int:offer_id>/delete")
def delete_offer(offer_id):
    offers.delete(get_db(), offer_id)
    flash("Offer deleted. People who bought it keep their access.", "success")
    return redirect(url_for("admin.offers_page"))


# ------------------------------------------------------------------- codes

@bp.get("/codes")
def codes_page():
    conn = get_db()
    all_offers = offers.list_all(conn)
    return render_template(
        "admin/codes.html",
        codes=codes.list_all(conn),
        code_sites=codes.sites_map(conn),
        offers=all_offers,
        offer_names={p.id: p.name for p in all_offers},
        sites=sites.list_all(conn),
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
        except Exception:  # noqa: BLE001
            value = 0
    linked = checked_sites()
    conn = get_db()
    try:
        count = max(1, min(500, _int_or_none(form.get("count")) or 1))
        created = []
        with db.transaction(conn):
            for _ in range(count):
                row = codes.create(
                    conn,
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
                    sites=linked,
                )
                created.append(row["code"])
    except codes.CodeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.codes_page"))
    names = {s.id: s.name for s in sites.list_all(conn)}
    where = ", ".join(names[s] for s in linked) if linked else "every site"
    flash(f"Created {len(created)} code(s) for {where}: {', '.join(created[:20])}{'…' if len(created) > 20 else ''}",
          "success")
    return redirect(url_for("admin.codes_page"))


@bp.post("/codes/<code>/sites")
def update_code_sites(code):
    if codes.get(get_db(), code) is None:
        abort(404)
    codes.set_sites(get_db(), code, checked_sites())
    flash(f"Sites updated for {code}.", "success")
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
    conn = get_db()
    linked = codes.sites_map(conn)
    lines = ["code,kind,value,offers,sites,uses,max_uses,expires,active,note"]
    for row in codes.list_all(conn):
        note = (row["note"] or "").replace('"', '""')
        lines.append(
            f'{row["code"]},{row["kind"]},{row["value"]},"{row["plans"]}","{",".join(linked.get(row["code"], []))}",'
            f'{row["uses"]},{row["max_uses"] or ""},{format_ts(row["expires_at"]) if row["expires_at"] else ""},'
            f'{row["active"]},"{note}"'
        )
    return Response("\n".join(lines) + "\n", mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=paywall-codes.csv"})


# --------------------------------------------------------------- customers

@bp.get("/customers")
def customers_page():
    conn = get_db()
    query = request.args.get("q", "")
    return render_template("admin/customers.html", customers=access.search(conn, query), query=query,
                           sites=sites.list_all(conn))


@bp.get("/customers/<customer_id>")
def customer_page(customer_id):
    conn = get_db()
    customer = access.get_customer(conn, customer_id)
    if customer is None:
        abort(404)
    return render_template(
        "admin/customer.html", customer=customer, entitlements=access.entitlements_for(conn, customer_id),
        open_sites=access.access_by_site(conn, customer_id), sites=sites.list_all(conn), now=access.now(),
    )


@bp.post("/grant")
def grant():
    conn = get_db()
    email = request.form.get("email", "").strip()
    customer_id = request.form.get("customer_id", "").strip()
    days = _int_or_none(request.form.get("days"))
    note = (request.form.get("note") or "").strip() or f"Granted by {g.admin_user}"
    linked = checked_sites()
    back = request.referrer or url_for("admin.customers_page")
    if not linked:
        flash("Tick at least one site to give access to.", "error")
        return redirect(back)
    with db.transaction(conn):
        customer = access.get_customer(conn, customer_id) or access.find_by_email(conn, email)
        if customer is None:
            if "@" not in email:
                flash("Enter a valid email address.", "error")
                return redirect(back)
            customer = access.create_customer(conn, email)
        expires = access.now() + days * plans.DAY if days else None
        access.grant(conn, customer["id"], "manual", expires, note=note, sites=linked)
    names = {s.id: s.name for s in sites.list_all(conn)}
    flash(f"{customer['email'] or customer['id']} can now open {', '.join(names[s] for s in linked)}.", "success")
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


# -------------------------------------------------------------- cloudflare

@bp.get("/cloudflare")
def cloudflare_page():
    conf = cfg()
    conn = get_db()
    return render_template(
        "admin/cloudflare.html",
        status=cloudflare.read_status(conf),
        worker_vars=cloudflare.worker_vars(conf, conn),
        site_routes=[(s, s.route_list) for s in sites.list_all(conn, active_only=True)],
        has_routes=bool(cloudflare.all_routes(conn)),
    )


@bp.post("/cloudflare/deploy")
def deploy_worker():
    messages = []
    try:
        cloudflare.deploy(cfg(), get_db(), log=messages.append)
        flash("Worker deployed: " + " / ".join(messages), "success")
    except cloudflare.CloudflareError as exc:
        cloudflare.write_status(cfg(), f"Deployment failed: {exc}")
        flash(f"Deployment failed: {exc}", "error")
    return redirect(url_for("admin.cloudflare_page"))


@bp.get("/cloudflare/paywall-worker.js")
def download_worker():
    return Response(cloudflare.worker_source(), mimetype="application/javascript",
                    headers={"Content-Disposition": "attachment; filename=paywall-worker.js"})


@bp.get("/cloudflare/wrangler.toml")
def download_wrangler():
    return Response(cloudflare.wrangler_toml(cfg(), get_db()), mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=wrangler.toml"})
