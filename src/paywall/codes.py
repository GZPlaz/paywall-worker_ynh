"""Discount and bypass codes.

Kinds:
  * ``percent`` - percentage off the price (100 % on a one-off plan = free)
  * ``fixed``   - fixed amount off (smallest currency unit)
  * ``free``    - skips payment entirely and grants access for ``grant_days``
                  days (or forever)
"""

import re
import secrets
import time

KINDS = ("percent", "fixed", "free")
DURATIONS = ("once", "forever", "repeating")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")


class CodeError(ValueError):
    pass


def normalize(code):
    return (code or "").strip().upper()


def generate(prefix="", length=10):
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return normalize(f"{prefix}{body}")


def create(db, code=None, kind="percent", value=0, plans="", max_uses=None, expires_at=None,
           grant_days=None, duration="once", duration_months=None, note=None):
    code = normalize(code) or generate()
    if not _CODE_RE.match(code):
        raise CodeError("Codes must be 3-64 characters: letters, digits, '-' or '_'.")
    if kind not in KINDS:
        raise CodeError(f"Unknown code type '{kind}'.")
    value = int(value or 0)
    if kind == "percent" and not 1 <= value <= 100:
        raise CodeError("Percentage must be between 1 and 100.")
    if kind == "fixed" and value <= 0:
        raise CodeError("Amount off must be greater than zero.")
    if kind == "free":
        value = 0
    if duration not in DURATIONS:
        raise CodeError("Duration must be once, forever or repeating.")
    if duration == "repeating" and not duration_months:
        raise CodeError("Repeating discounts need a number of months.")
    plan_list = ",".join(p.strip() for p in (plans or "").split(",") if p.strip())
    if db.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone():
        raise CodeError(f"Code {code} already exists.")
    db.execute(
        """INSERT INTO codes (code, kind, value, plans, max_uses, expires_at, grant_days, duration,
                              duration_months, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (code, kind, value, plan_list, max_uses or None, expires_at or None, grant_days or None,
         duration, duration_months or None, note, int(time.time())),
    )
    return get(db, code)


def get(db, code):
    return db.execute("SELECT * FROM codes WHERE code = ?", (normalize(code),)).fetchone()


def list_all(db):
    return db.execute("SELECT * FROM codes ORDER BY created_at DESC").fetchall()


def set_active(db, code, active):
    db.execute("UPDATE codes SET active = ? WHERE code = ?", (1 if active else 0, normalize(code)))


def delete(db, code):
    db.execute("DELETE FROM codes WHERE code = ?", (normalize(code),))


def plan_ids(row):
    return [p for p in (row["plans"] or "").split(",") if p]


def check(db, code, plan_id=None, now=None):
    """Return the code row if usable (optionally for ``plan_id``), else raise CodeError."""
    now = now or int(time.time())
    row = get(db, code)
    if row is None or not row["active"]:
        raise CodeError("This code is not valid.")
    if row["expires_at"] and row["expires_at"] <= now:
        raise CodeError("This code has expired.")
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        raise CodeError("This code has already been used the maximum number of times.")
    if plan_id and plan_ids(row) and plan_id not in plan_ids(row):
        raise CodeError("This code does not apply to that option.")
    return row


def discounted(row, amount):
    """Amount after applying a percent/fixed code (never below zero)."""
    if row is None:
        return amount
    if row["kind"] == "free":
        return 0
    if row["kind"] == "percent":
        return max(0, amount - (amount * row["value"] + 50) // 100)
    return max(0, amount - row["value"])


def record_use(db, code, customer_id, context):
    """Count a use of ``code``. Idempotent per ``context``. Returns True the
    first time a given context is recorded."""
    cur = db.execute(
        "INSERT OR IGNORE INTO redemptions (code, customer_id, context, created_at) VALUES (?, ?, ?, ?)",
        (normalize(code), customer_id, context, int(time.time())),
    )
    if cur.rowcount:
        db.execute("UPDATE codes SET uses = uses + 1 WHERE code = ?", (normalize(code),))
        return True
    return False


def already_redeemed(db, code, customer_id):
    if not customer_id:
        return False
    return db.execute(
        "SELECT 1 FROM redemptions WHERE code = ? AND customer_id = ?", (normalize(code), customer_id)
    ).fetchone() is not None


def summary(row, fmt_money):
    if row["kind"] == "free":
        days = row["grant_days"]
        return f"Free access for {days} day{'s' if days != 1 else ''}" if days else "Free access, forever"
    off = f"{row['value']}% off" if row["kind"] == "percent" else f"{fmt_money(row['value'])} off"
    if row["duration"] == "forever":
        off += " (subscriptions: every payment)"
    elif row["duration"] == "repeating":
        off += f" (subscriptions: first {row['duration_months']} months)"
    return off
