"""Offers (what visitors can buy), each linked to one or more sites.

Buying an offer gives access to every site it is linked to, so one offer
linked to several sites is a bundle."""

import time
from decimal import InvalidOperation

from .plans import DAY, Plan, to_minor

KINDS = {
    "one_time": "One-time payment",
    "subscription": "Subscription",
    "pass": "Time pass",
    "pwyw": "Pay what you want",
}
INTERVALS = ("day", "week", "month", "year")


class OfferError(ValueError):
    pass


def _site_map(db):
    result = {}
    for row in db.execute("SELECT offer_id, site_id FROM offer_sites ORDER BY site_id"):
        result.setdefault(row["offer_id"], []).append(row["site_id"])
    return result


def _plan(row, site_ids):
    kind = row["kind"]
    return Plan(
        id=str(row["id"]),
        name=row["name"],
        mode="subscription" if kind == "subscription" else "payment",
        amount=row["amount"],
        access_seconds=None if kind == "subscription" else row["access_seconds"],
        interval=row["interval"],
        trial_days=row["trial_days"] or 0,
        suggested=row["suggested"],
        custom_amount=kind == "pwyw",
        kind=kind,
        site_ids=site_ids,
        active=bool(row["active"]),
        position=row["position"],
    )


def list_all(db, site_id=None, active_only=False):
    site_map = _site_map(db)
    rows = db.execute("SELECT * FROM offers ORDER BY position, id").fetchall()
    plans = [_plan(row, site_map.get(row["id"], [])) for row in rows]
    if active_only:
        plans = [p for p in plans if p.active]
    if site_id is not None:
        plans = [p for p in plans if site_id in p.site_ids]
    return plans


def get(db, offer_id):
    try:
        offer_id = int(offer_id)
    except (TypeError, ValueError):
        return None
    row = db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if row is None:
        return None
    return _plan(row, _site_map(db).get(offer_id, []))


def _int(values, key, default=0, minimum=0):
    raw = str(values.get(key) or "").strip()
    if raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError as exc:
        raise OfferError(f"'{raw}' is not a whole number.") from exc


def _money(values, key, cfg, required=True):
    raw = str(values.get(key) or "").strip().replace(",", ".")
    if raw == "":
        if required:
            raise OfferError("Enter a price.")
        return None
    try:
        amount = to_minor(raw, cfg)
    except (InvalidOperation, ValueError) as exc:
        raise OfferError(f"'{raw}' is not a valid price.") from exc
    if amount < 0:
        raise OfferError("Prices cannot be negative.")
    return amount


def save(db, cfg, values, site_ids, offer_id=None, extend_holders=False):
    """Create (``offer_id`` None) or update an offer and link it to
    ``site_ids``. With ``extend_holders``, people who already bought it get
    access to newly linked sites too. Returns the Plan."""
    kind = values.get("kind", "one_time")
    if kind not in KINDS:
        raise OfferError("Unknown kind of offer.")
    name = (values.get("name") or "").strip()
    if not name:
        raise OfferError("Give the offer a name.")
    site_ids = sorted(set(site_ids))
    if not site_ids:
        raise OfferError("Link the offer to at least one site.")
    known = {r[0] for r in db.execute("SELECT id FROM sites")}
    if not set(site_ids) <= known:
        raise OfferError("Unknown site.")
    amount = _money(values, "price", cfg)
    if amount == 0:
        raise OfferError("Use a free access code instead of a free offer.")
    row = {
        "name": name, "kind": kind, "amount": amount, "suggested": None, "interval": None,
        "trial_days": 0, "access_seconds": None,
        "active": 1 if values.get("active") else 0,
        "position": _int(values, "position"),
    }
    if kind == "subscription":
        row["interval"] = values.get("interval") or "month"
        if row["interval"] not in INTERVALS:
            raise OfferError("Billing period must be day, week, month or year.")
        row["trial_days"] = min(730, _int(values, "trial_days"))
    elif kind == "pass":
        hours = _int(values, "pass_hours", default=24, minimum=1)
        row["access_seconds"] = hours * 3600
    else:
        days = _int(values, "access_days")
        row["access_seconds"] = days * DAY if days else None
        if kind == "pwyw":
            suggested = _money(values, "suggested", cfg, required=False)
            row["suggested"] = max(amount, suggested or amount)
    if offer_id is None:
        cur = db.execute(
            """INSERT INTO offers (name, kind, amount, suggested, interval, trial_days, access_seconds,
                                   active, position, created_at)
               VALUES (:name, :kind, :amount, :suggested, :interval, :trial_days, :access_seconds,
                       :active, :position, :created_at)""",
            dict(row, created_at=int(time.time())),
        )
        offer_id = cur.lastrowid
        previous = []
    else:
        offer_id = int(offer_id)
        previous = _site_map(db).get(offer_id, [])
        db.execute(
            """UPDATE offers SET name = :name, kind = :kind, amount = :amount, suggested = :suggested,
                   interval = :interval, trial_days = :trial_days, access_seconds = :access_seconds,
                   active = :active, position = :position WHERE id = :id""",
            dict(row, id=offer_id),
        )
    db.execute("DELETE FROM offer_sites WHERE offer_id = ?", (offer_id,))
    db.executemany("INSERT INTO offer_sites (offer_id, site_id) VALUES (?, ?)",
                   [(offer_id, s) for s in site_ids])
    added = [s for s in site_ids if s not in previous]
    if extend_holders and added:
        holders = db.execute(
            "SELECT id FROM entitlements WHERE offer_id = ? AND status = 'active'", (offer_id,)
        ).fetchall()
        db.executemany(
            "INSERT OR IGNORE INTO entitlement_sites (entitlement_id, site_id) VALUES (?, ?)",
            [(h["id"], s) for h in holders for s in added],
        )
    return get(db, offer_id)


def delete(db, offer_id):
    db.execute("DELETE FROM offers WHERE id = ?", (int(offer_id),))


def holder_count(db, offer_id):
    return db.execute(
        "SELECT COUNT(*) FROM entitlements WHERE offer_id = ? AND status = 'active'", (int(offer_id),)
    ).fetchone()[0]
