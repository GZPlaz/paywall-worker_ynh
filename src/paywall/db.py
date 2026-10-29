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
]


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
