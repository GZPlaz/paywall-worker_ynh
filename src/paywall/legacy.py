"""One-time import of the single-site settings used by 1.0.0~ynh1.

Back then the page texts, routes and prices lived in the config panel and
every purchase opened every protected route. On first start with the
multi-site schema they become a site called "main" with its offers, and
existing purchases and codes are linked to it."""

import logging

from . import db, sites
from .plans import DAY, to_minor

log = logging.getLogger(__name__)

MAIN_SITE = "main"


def _routes(text, domain):
    good = []
    for line in (text or "").splitlines():
        try:
            good += [p for p, _, _ in sites.parse_routes(line, domain)]
        except sites.SiteError as exc:
            log.warning("Skipping legacy route: %s", exc)
    return "\n".join(good)


def import_settings(conn, cfg):
    """Create the first site from legacy settings. Returns the site id, or
    None when there was nothing to import. Runs at most once."""
    if db.get_meta(conn, "legacy_imported"):
        return None
    legacy = cfg["legacy"]
    has_data = conn.execute(
        "SELECT (SELECT COUNT(*) FROM entitlements) + (SELECT COUNT(*) FROM codes)"
    ).fetchone()[0]
    if conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] or not (legacy["protected_routes"].strip() or has_data):
        db.set_meta(conn, "legacy_imported", "1")
        return None

    with db.transaction(conn):
        conn.execute(
            """INSERT INTO sites (id, name, title, description, accent_color, support_email, routes,
                                  bypass_paths, recheck_hours, show_codes, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, strftime('%s', 'now'))""",
            (
                MAIN_SITE, "Main site", legacy["paywall_title"], legacy["paywall_description"],
                legacy["accent_color"] if sites._COLOR_RE.match(legacy["accent_color"] or "") else sites.DEFAULT_ACCENT,
                legacy["support_email"], _routes(legacy["protected_routes"], cfg["domain"]),
                legacy["bypass_paths"], max(1, legacy["access_recheck_hours"]),
                1 if legacy["show_codes_field"] else 0,
            ),
        )
        offer_ids = {}
        specs = [
            ("one_time", "one_time", legacy["one_time_name"], legacy["one_time_price"], {
                "access_seconds": legacy["one_time_access_days"] * DAY or None}),
            ("subscription", "subscription", legacy["subscription_name"], legacy["subscription_price"], {
                "interval": legacy["subscription_interval"], "trial_days": legacy["subscription_trial_days"]}),
            ("pass", "pass", legacy["pass_name"], legacy["pass_price"], {
                "access_seconds": max(1, legacy["pass_hours"]) * 3600}),
            ("pwyw", "pwyw", legacy["pwyw_name"], legacy["pwyw_min_price"], {
                "access_seconds": legacy["pwyw_access_days"] * DAY or None,
                "suggested": to_minor(legacy["pwyw_suggested_price"], cfg)}),
        ]
        for position, (old_id, kind, name, price, extra) in enumerate(specs):
            if not legacy[f"enable_{old_id}"]:
                continue
            row = {"interval": None, "trial_days": 0, "access_seconds": None, "suggested": None, **extra}
            cur = conn.execute(
                """INSERT INTO offers (name, kind, amount, suggested, interval, trial_days, access_seconds,
                                       active, position, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, strftime('%s', 'now'))""",
                (name, kind, to_minor(price, cfg), row["suggested"], row["interval"], row["trial_days"],
                 row["access_seconds"], position),
            )
            offer_ids[old_id] = cur.lastrowid
            conn.execute("INSERT INTO offer_sites (offer_id, site_id) VALUES (?, ?)", (cur.lastrowid, MAIN_SITE))

        # Everything sold so far opened the whole paywall, i.e. this site.
        conn.execute(
            "INSERT OR IGNORE INTO entitlement_sites (entitlement_id, site_id) SELECT id, ? FROM entitlements",
            (MAIN_SITE,),
        )
        for old_id, new_id in offer_ids.items():
            conn.execute("UPDATE entitlements SET offer_id = ? WHERE kind = ? AND offer_id IS NULL", (new_id, old_id))
        for row in conn.execute("SELECT code, plans FROM codes WHERE plans != ''").fetchall():
            mapped = [str(offer_ids[p]) for p in row["plans"].split(",") if p in offer_ids]
            # "0" matches no offer: a code limited to disabled offers must not start applying to all
            conn.execute("UPDATE codes SET plans = ? WHERE code = ?", (",".join(mapped) or "0", row["code"]))
        db.set_meta(conn, "legacy_imported", "1")
    log.info("Imported the single-site settings as site '%s' with %d offer(s)", MAIN_SITE, len(offer_ids))
    return MAIN_SITE
