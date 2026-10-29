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


def grant(db, customer_id, kind, expires_at=None, source_ref=None, amount=None,
          currency=None, code=None, note=None, starts_at=None):
    """Create an entitlement. Idempotent when ``source_ref`` is given."""
    ts = now()
    db.execute(
        """INSERT OR IGNORE INTO entitlements
           (customer_id, kind, source_ref, status, starts_at, expires_at, amount, currency, code, note, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, kind, source_ref, starts_at or ts, expires_at, amount, currency, code, note, ts, ts),
    )
    if source_ref:
        return db.execute("SELECT * FROM entitlements WHERE source_ref = ?", (source_ref,)).fetchone()
    return db.execute("SELECT * FROM entitlements WHERE id = last_insert_rowid()").fetchone()


def upsert_subscription(db, customer_id, subscription_id, status, expires_at, amount=None, currency=None, code=None):
    existing = db.execute("SELECT * FROM entitlements WHERE source_ref = ?", (subscription_id,)).fetchone()
    if existing is None:
        if status != "active" or customer_id is None:
            return None
        return grant(db, customer_id, "subscription", expires_at, subscription_id, amount, currency, code)
    if existing["status"] == "revoked":
        return existing
    db.execute(
        "UPDATE entitlements SET status = ?, expires_at = ?, updated_at = ? WHERE id = ?",
        (status, expires_at, now(), existing["id"]),
    )
    return db.execute("SELECT * FROM entitlements WHERE id = ?", (existing["id"],)).fetchone()


def entitlements_for(db, customer_id):
    return db.execute(
        "SELECT * FROM entitlements WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)
    ).fetchall()


def access_until(db, customer_id, at=None):
    """Return (has_access, until) where ``until`` is None for unlimited access."""
    at = at or now()
    rows = db.execute(
        "SELECT expires_at FROM entitlements WHERE customer_id = ? AND status = 'active' "
        "AND starts_at <= ? AND (expires_at IS NULL OR expires_at > ?)",
        (customer_id, at, at),
    ).fetchall()
    if not rows:
        return False, None
    if any(row["expires_at"] is None for row in rows):
        return True, None
    return True, max(row["expires_at"] for row in rows)


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
    return {
        "customers": db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "active": db.execute(
            "SELECT COUNT(DISTINCT customer_id) FROM entitlements WHERE status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)", (ts,)
        ).fetchone()[0],
        "subscriptions": db.execute(
            "SELECT COUNT(*) FROM entitlements WHERE kind = 'subscription' AND status = 'active' "
            "AND expires_at > ?", (ts,)
        ).fetchone()[0],
        "revenue": db.execute(
            "SELECT currency, SUM(amount) AS total FROM entitlements WHERE amount IS NOT NULL "
            "AND kind != 'subscription' GROUP BY currency"
        ).fetchall(),
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
