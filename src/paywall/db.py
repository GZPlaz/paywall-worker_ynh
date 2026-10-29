"""SQLite storage."""

import os
import sqlite3
from contextlib import contextmanager

DB_FILENAME = "paywall.sqlite3"

# Each entry migrates the schema from version N to N+1.
MIGRATIONS = [
    """
    CREATE TABLE customers (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE COLLATE NOCASE,
        stripe_customer_id TEXT,
        created_at INTEGER NOT NULL
    );

    -- Every reason someone has access: a purchase, a subscription, a code...
    CREATE TABLE entitlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,              -- one_time | subscription | pass | pwyw | code | manual
        source_ref TEXT UNIQUE,          -- checkout session / subscription / redemption id
        status TEXT NOT NULL,            -- active | ended | revoked
        starts_at INTEGER NOT NULL,
        expires_at INTEGER,              -- NULL = never expires
        amount INTEGER,                  -- smallest currency unit
        currency TEXT,
        code TEXT,
        note TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX entitlements_customer ON entitlements(customer_id);

    CREATE TABLE codes (
        code TEXT PRIMARY KEY COLLATE NOCASE,
        kind TEXT NOT NULL,              -- percent | fixed | free
        value INTEGER NOT NULL DEFAULT 0,-- percent (1-100) or amount off (smallest unit)
        plans TEXT NOT NULL DEFAULT '',  -- comma separated plan ids, empty = all
        max_uses INTEGER,                -- NULL = unlimited
        uses INTEGER NOT NULL DEFAULT 0,
        expires_at INTEGER,              -- NULL = never
        grant_days INTEGER,              -- free codes: access length, NULL = forever
        duration TEXT NOT NULL DEFAULT 'once',  -- subscriptions: once | forever | repeating
        duration_months INTEGER,
        stripe_coupon_id TEXT,
        stripe_coupon_key TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        note TEXT,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL COLLATE NOCASE,
        customer_id TEXT,
        context TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE login_tokens (
        token_hash TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        email TEXT,                      -- set when the link attaches a new email
        expires_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE attempts (
        ip TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX attempts_lookup ON attempts(ip, kind, created_at);
    """,
    # v2: several sites, each with its own page settings, and offers / codes /
    # access that can be linked to any number of them.
    """
    CREATE TABLE sites (
        id TEXT PRIMARY KEY,             -- slug, also used in URLs and cookie names
        name TEXT NOT NULL,              -- internal label
        title TEXT NOT NULL,             -- paywall page heading
        description TEXT NOT NULL DEFAULT '',
        accent_color TEXT NOT NULL DEFAULT '#1b2a41',
        support_email TEXT NOT NULL DEFAULT '',
        routes TEXT NOT NULL DEFAULT '', -- Cloudflare route patterns, one per line
        bypass_paths TEXT NOT NULL DEFAULT '',
        recheck_hours INTEGER NOT NULL DEFAULT 24,
        show_codes INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,              -- one_time | subscription | pass | pwyw
        amount INTEGER NOT NULL,         -- price, or minimum for pwyw (smallest unit)
        suggested INTEGER,               -- pwyw only
        interval TEXT,                   -- subscriptions: day | week | month | year
        trial_days INTEGER NOT NULL DEFAULT 0,
        access_seconds INTEGER,          -- one-off offers; NULL = no end date
        active INTEGER NOT NULL DEFAULT 1,
        position INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE offer_sites (
        offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE ON UPDATE CASCADE,
        PRIMARY KEY (offer_id, site_id)
    );

    -- No rows for a code = it works on every site.
    CREATE TABLE code_sites (
        code TEXT NOT NULL COLLATE NOCASE REFERENCES codes(code) ON DELETE CASCADE,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE ON UPDATE CASCADE,
        PRIMARY KEY (code, site_id)
    );

    CREATE TABLE entitlement_sites (
        entitlement_id INTEGER NOT NULL REFERENCES entitlements(id) ON DELETE CASCADE,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE ON UPDATE CASCADE,
        PRIMARY KEY (entitlement_id, site_id)
    );
    CREATE INDEX entitlement_sites_site ON entitlement_sites(site_id);

    ALTER TABLE entitlements ADD COLUMN offer_id INTEGER;

    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v3: one Stripe Product per offer (for tax codes and readable invoices),
    # and the payment behind one-off purchases (to act on refunds/disputes).
    """
    ALTER TABLE offers ADD COLUMN stripe_product_id TEXT;
    ALTER TABLE offers ADD COLUMN stripe_product_key TEXT;
    ALTER TABLE entitlements ADD COLUMN payment_intent TEXT;
    CREATE INDEX entitlements_payment_intent ON entitlements(payment_intent);
    """,
]


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def connect(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, DB_FILENAME), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def migrate(conn):
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(MIGRATIONS)):
        conn.executescript("BEGIN;" + MIGRATIONS[index] + f"PRAGMA user_version = {index + 1}; COMMIT;")
    return len(MIGRATIONS)


def snapshot(conn, destination):
    """Consistent copy of the live database (used by the backup script)."""
    target = sqlite3.connect(destination)
    with target:
        conn.backup(target)
    target.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
