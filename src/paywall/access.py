"""Customers and entitlements (who has access, and until when)."""

import secrets
import time

SUBSCRIPTION_GRACE = 6 * 3600  # tolerate late renewal webhooks


def now():
    return int(time.time())


def new_customer_id():
    return "c_" + secrets.token_hex(8)


def get_customer(db, customer_id):
    if not customer_id:
        return None
    return db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()


def find_by_email(db, email):
    if not email:
        return None
    return db.execute("SELECT * FROM customers WHERE email = ?", (email.strip(),)).fetchone()


def create_customer(db, email=None, stripe_customer_id=None):
    cid = new_customer_id()
    db.execute(
        "INSERT INTO customers (id, email, stripe_customer_id, created_at) VALUES (?, ?, ?, ?)",
        (cid, email.strip() if email else None, stripe_customer_id, now()),
    )
    return get_customer(db, cid)


def attach_details(db, customer_id, email=None, stripe_customer_id=None):
    """Remember an email / Stripe customer id for a customer that has none.

    Emails typed at checkout are not verified, so an email that already
    belongs to another customer is never used to pick or merge accounts;
    only a sign-in link (proof of ownership) can do that, see ``merge``."""
    customer = get_customer(db, customer_id)
    if customer is None:
        return None
    if email and not customer["email"] and find_by_email(db, email) is None:
        db.execute("UPDATE customers SET email = ? WHERE id = ?", (email.strip(), customer_id))
    if stripe_customer_id and not customer["stripe_customer_id"]:
        db.execute("UPDATE customers SET stripe_customer_id = ? WHERE id = ?", (stripe_customer_id, customer_id))
    return get_customer(db, customer_id)


def merge(db, source_id, target_id):
    """Move everything from an email-less customer into ``target_id``."""
    source = get_customer(db, source_id)
    if source is None or source_id == target_id or source["email"]:
        return False
    db.execute("UPDATE entitlements SET customer_id = ? WHERE customer_id = ?", (target_id, source_id))
    db.execute("UPDATE redemptions SET customer_id = ? WHERE customer_id = ?", (target_id, source_id))
    if source["stripe_customer_id"]:
        db.execute(
            "UPDATE customers SET stripe_customer_id = COALESCE(stripe_customer_id, ?) WHERE id = ?",
            (source["stripe_customer_id"], target_id),
        )
    db.execute("DELETE FROM customers WHERE id = ?", (source_id,))
    return True


def link_sites(db, entitlement_id, site_ids):
    db.executemany(
        "INSERT OR IGNORE INTO entitlement_sites (entitlement_id, site_id) "
        "SELECT ?, id FROM sites WHERE id = ?",
        [(entitlement_id, s) for s in site_ids],
    )


def grant(db, customer_id, kind, expires_at=None, source_ref=None, amount=None,
          currency=None, code=None, note=None, starts_at=None, sites=(), offer_id=None):
    """Create an entitlement giving access to ``sites``. Idempotent when
    ``source_ref`` is given."""
    ts = now()
    db.execute(
        """INSERT OR IGNORE INTO entitlements
           (customer_id, kind, source_ref, status, starts_at, expires_at, amount, currency, code, note,
            offer_id, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, kind, source_ref, starts_at or ts, expires_at, amount, currency, code, note,
         offer_id, ts, ts),
    )
    if source_ref:
        row = db.execute("SELECT * FROM entitlements WHERE source_ref = ?", (source_ref,)).fetchone()
    else:
        row = db.execute("SELECT * FROM entitlements WHERE id = last_insert_rowid()").fetchone()
    link_sites(db, row["id"], sites)
    return row


def upsert_subscription(db, customer_id, subscription_id, status, expires_at, amount=None, currency=None,
                        code=None, sites=(), offer_id=None):
    existing = db.execute("SELECT * FROM entitlements WHERE source_ref = ?", (subscription_id,)).fetchone()
    if existing is None:
        if status != "active" or customer_id is None:
            return None
        return grant(db, customer_id, "subscription", expires_at, subscription_id, amount, currency, code,
                     sites=sites, offer_id=offer_id)
    if existing["status"] == "revoked":
        return existing
    db.execute(
        "UPDATE entitlements SET status = ?, expires_at = ?, updated_at = ? WHERE id = ?",
        (status, expires_at, now(), existing["id"]),
    )
    return db.execute("SELECT * FROM entitlements WHERE id = ?", (existing["id"],)).fetchone()


def entitlements_for(db, customer_id):
    rows = db.execute(
        """SELECT e.*, (SELECT GROUP_CONCAT(site_id, ',') FROM entitlement_sites es
                        WHERE es.entitlement_id = e.id) AS site_list
           FROM entitlements e WHERE e.customer_id = ? ORDER BY e.created_at DESC""",
        (customer_id,),
    ).fetchall()
    return rows


def access_until(db, customer_id, site_id, at=None):
    """Return (has_access, until) for ``site_id``; ``until`` is None for
    access with no end date."""
    at = at or now()
    rows = db.execute(
        """SELECT e.expires_at FROM entitlements e
           JOIN entitlement_sites es ON es.entitlement_id = e.id AND es.site_id = ?
           WHERE e.customer_id = ? AND e.status = 'active' AND e.starts_at <= ?
             AND (e.expires_at IS NULL OR e.expires_at > ?)""",
        (site_id, customer_id, at, at),
    ).fetchall()
    if not rows:
        return False, None
    if any(row["expires_at"] is None for row in rows):
        return True, None
    return True, max(row["expires_at"] for row in rows)


def access_by_site(db, customer_id):
    """{site_id: until-or-None} for every site the customer can open now."""
    ts = now()
    result = {}
    rows = db.execute(
        """SELECT es.site_id, e.expires_at FROM entitlements e
           JOIN entitlement_sites es ON es.entitlement_id = e.id
           WHERE e.customer_id = ? AND e.status = 'active' AND e.starts_at <= ?
             AND (e.expires_at IS NULL OR e.expires_at > ?)""",
        (customer_id, ts, ts),
    ).fetchall()
    for row in rows:
        site, exp = row["site_id"], row["expires_at"]
        if site in result and (result[site] is None or exp is None):
            result[site] = None
        else:
            result[site] = exp if site not in result else max(result[site], exp)
    return result


def lapsed_subscriptions(db, customer_id):
    """Subscriptions that look expired locally but may have been renewed
    (useful when a renewal webhook was missed)."""
    return db.execute(
        "SELECT * FROM entitlements WHERE customer_id = ? AND kind = 'subscription' "
        "AND status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
        (customer_id, now()),
    ).fetchall()


def revoke(db, entitlement_id):
    db.execute(
        "UPDATE entitlements SET status = 'revoked', updated_at = ? WHERE id = ?", (now(), entitlement_id)
    )


def stripe_customer_for(db, customer_id):
    row = get_customer(db, customer_id)
    return row["stripe_customer_id"] if row else None


def stats(db):
    ts = now()
    active = "e.status = 'active' AND (e.expires_at IS NULL OR e.expires_at > :ts)"
    return {
        "customers": db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "active": db.execute(
            f"SELECT COUNT(DISTINCT customer_id) FROM entitlements e WHERE {active}", {"ts": ts}
        ).fetchone()[0],
        "subscriptions": db.execute(
            "SELECT COUNT(*) FROM entitlements WHERE kind = 'subscription' AND status = 'active' "
            "AND expires_at > ?", (ts,)
        ).fetchone()[0],
        "revenue": db.execute(
            "SELECT currency, SUM(amount) AS total FROM entitlements WHERE amount IS NOT NULL "
            "AND kind != 'subscription' GROUP BY currency"
        ).fetchall(),
        "per_site": {
            row["site_id"]: row["people"]
            for row in db.execute(
                f"""SELECT es.site_id, COUNT(DISTINCT e.customer_id) AS people FROM entitlements e
                    JOIN entitlement_sites es ON es.entitlement_id = e.id WHERE {active}
                    GROUP BY es.site_id""",
                {"ts": ts},
            )
        },
    }


def search(db, query="", limit=200):
    ts = now()
    like = f"%{query.strip()}%"
    return db.execute(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM entitlements e WHERE e.customer_id = c.id AND e.status = 'active'
                     AND (e.expires_at IS NULL OR e.expires_at > :ts)) AS active_count
           FROM customers c
           WHERE (:q = '' OR c.email LIKE :like OR c.id LIKE :like)
           ORDER BY c.created_at DESC LIMIT :limit""",
        {"ts": ts, "q": query.strip(), "like": like, "limit": limit},
    ).fetchall()
