"""Command line interface, used by the YunoHost scripts and by admins.

    python -m paywall <command> [options]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import access, cloudflare, codes, config, db, legacy, offers, plans, sites

SECRET_KEYS = {"signing_secret", "flask_secret", "origin_secret"}


def _read_pairs(stream):
    """Read NUL separated key/value pairs (so values may contain anything)."""
    parts = stream.buffer.read().decode("utf-8").split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) % 2:
        raise SystemExit("write-config: odd number of fields on stdin")
    return dict(zip(parts[::2], parts[1::2]))


def cmd_config_keys(_args):
    # Legacy keys are listed too, so an upgrade can import them once.
    for key in list(config.SCHEMA) + list(config.LEGACY_SCHEMA):
        if key not in config.DERIVED_KEYS:
            print(key)


def cmd_config_defaults(_args):
    out = sys.stdout.buffer
    for key, (kind, default) in config.SCHEMA.items():
        if key in config.DERIVED_KEYS or key in SECRET_KEYS or key in ("domain", "path", "port"):
            continue
        # Empty defaults are written too: YunoHost refuses to show a config
        # panel whose questions have no stored setting.
        value = ("1" if default else "0") if kind == "bool" else str(default)
        out.write(key.encode() + b"\0" + value.encode() + b"\0")


def cmd_write_config(args):
    values = _read_pairs(sys.stdin)
    try:
        conf = config.normalize(values)
    except config.ConfigError as exc:
        raise SystemExit(f"Invalid configuration: {exc}") from None
    fd = os.open(args.output + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(conf, fh, indent=2, sort_keys=True)
    os.replace(args.output + ".tmp", args.output)


def _load():
    return config.load()


def _db(conf):
    conn = db.connect(conf["data_dir"])
    db.migrate(conn)
    return conn


def cmd_check_sites(args):
    """Print a problem (and exit 0) if a site route would cover ``--domain``."""
    conf = _load()
    try:
        sites.validate_all_routes(_db(conf), args.domain or conf["domain"])
    except sites.SiteError as exc:
        print(str(exc))


def cmd_init_db(_args):
    conf = _load()
    conn = db.connect(conf["data_dir"])
    version = db.migrate(conn)
    imported = legacy.import_settings(conn, conf)
    print(f"Database ready (schema v{version})")
    if imported:
        print(f"Your previous settings are now the site '{imported}'; manage sites from the admin page.")


def cmd_backup_db(args):
    conf = _load()
    db.snapshot(_db(conf), args.destination)


def _date(value):
    if not value:
        return None
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86399


def _ts(value):
    return time.strftime("%Y-%m-%d", time.gmtime(value)) if value else "-"


def _site_list(conn, value):
    """Parse --sites a,b (or 'all'); exit with a clear message on typos."""
    known = sites.ids(conn)
    if not value:
        return []
    if value == "all":
        return known
    wanted = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise SystemExit(f"Unknown site(s): {', '.join(unknown)}. Known: {', '.join(known) or 'none'}")
    return wanted


def cmd_sites(_args):
    conf = _load()
    conn = _db(conf)
    for site in sites.list_all(conn):
        state = "on" if site.active else "off"
        routes = ", ".join(p for p, _, _ in site.route_list) or "-"
        print(f"{site.id:<16} {state:<4} {site.name:<24} {routes}")


def cmd_offers(_args):
    conf = _load()
    conn = _db(conf)
    for plan in offers.list_all(conn):
        state = "on sale" if plan.active else "off"
        print(f"{plan.id:<5} {plan.name:<28} {plans.price_label(plan, conf):<18} {state:<8} "
              f"sites={','.join(plan.site_ids) or '-'}")


def cmd_codes(args):
    conf = _load()
    conn = _db(conf)

    def fmt(amount):
        return plans.format_money(amount, conf)

    if args.action == "list":
        linked = codes.sites_map(conn)
        for row in codes.list_all(conn):
            state = "active" if row["active"] else "disabled"
            uses = f"{row['uses']}/{row['max_uses']}" if row["max_uses"] else str(row["uses"])
            where = ",".join(linked.get(row["code"].upper(), [])) or "all"
            print(f"{row['code']:<20} {codes.summary(row, fmt):<45} sites={where:<20} offers={row['plans'] or 'all':<10} "
                  f"uses={uses:<8} expires={_ts(row['expires_at']):<10} {state}")
    elif args.action == "create":
        value = args.value or 0
        if args.kind == "fixed":
            value = plans.to_minor(args.value or 0, conf)
        linked = _site_list(conn, args.sites)
        try:
            with db.transaction(conn):
                for _ in range(args.count):
                    row = codes.create(
                        conn, code=args.code if args.count == 1 else None, kind=args.kind, value=value,
                        plans=args.offers, max_uses=args.max_uses, expires_at=_date(args.expires),
                        grant_days=args.days, duration=args.duration, duration_months=args.months, note=args.note,
                        sites=linked,
                    )
                    print(row["code"])
        except codes.CodeError as exc:
            raise SystemExit(str(exc)) from None
    elif args.action in ("enable", "disable"):
        codes.set_active(conn, args.code, args.action == "enable")
    elif args.action == "delete":
        codes.delete(conn, args.code)


def cmd_grant(args):
    conf = _load()
    conn = _db(conf)
    linked = _site_list(conn, args.sites)
    if not linked:
        raise SystemExit("Choose the sites with --sites a,b (or --sites all)")
    with db.transaction(conn):
        customer = access.find_by_email(conn, args.email) or access.create_customer(conn, args.email)
        expires = access.now() + args.days * plans.DAY if args.days else None
        access.grant(conn, customer["id"], "manual", expires, note=args.note or "Granted from the command line",
                     sites=linked)
    print(f"Granted {args.email} access to {', '.join(linked)} until {_ts(expires) if expires else 'no end date'}")


def cmd_revoke(args):
    conf = _load()
    conn = _db(conf)
    customer = access.find_by_email(conn, args.email)
    if customer is None:
        raise SystemExit("No such customer")
    for ent in access.entitlements_for(conn, customer["id"]):
        if ent["status"] == "active":
            access.revoke(conn, ent["id"])
    print(f"Revoked all access for {args.email}")


def cmd_customers(args):
    conf = _load()
    conn = _db(conf)
    for row in access.search(conn, args.query or ""):
        by_site = access.access_by_site(conn, row["id"])
        state = ", ".join(f"{s} ({'until ' + _ts(u) if u else 'no end'})" for s, u in sorted(by_site.items()))
        print(f"{row['id']:<20} {row['email'] or '-':<40} {state or 'no access'}")


def cmd_deploy_worker(args):
    conf = _load()
    conn = _db(conf)
    if args.if_configured and not (conf.cloudflare_ready and cloudflare.all_routes(conn)):
        print("Cloudflare or site routes not set up yet, nothing to deploy.")
        return
    try:
        cloudflare.deploy(conf, conn)
    except cloudflare.CloudflareError as exc:
        cloudflare.write_status(conf, f"Deployment failed: {exc}")
        raise SystemExit(f"Cloudflare deployment failed: {exc}") from None


def cmd_undeploy_worker(_args):
    conf = _load()
    try:
        cloudflare.undeploy(conf, _db(conf))
    except cloudflare.CloudflareError as exc:
        raise SystemExit(f"Cloudflare cleanup failed: {exc}") from None


def cmd_worker_status(_args):
    print(cloudflare.read_status(_load()))


def cmd_wrangler(_args):
    conf = _load()
    print(cloudflare.wrangler_toml(conf, _db(conf)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="paywall")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config-keys").set_defaults(func=cmd_config_keys)
    sub.add_parser("config-defaults").set_defaults(func=cmd_config_defaults)
    p = sub.add_parser("write-config")
    p.add_argument("output")
    p.set_defaults(func=cmd_write_config)
    p = sub.add_parser("check-sites", help="check site routes against a paywall domain")
    p.add_argument("--domain", default=None)
    p.set_defaults(func=cmd_check_sites)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    p = sub.add_parser("backup-db")
    p.add_argument("destination")
    p.set_defaults(func=cmd_backup_db)

    sub.add_parser("sites", help="list sites").set_defaults(func=cmd_sites)
    sub.add_parser("offers", help="list offers").set_defaults(func=cmd_offers)

    p = sub.add_parser("codes", help="manage discount / access codes")
    csub = p.add_subparsers(dest="action", required=True)
    csub.add_parser("list")
    c = csub.add_parser("create")
    c.add_argument("--code", help="code text (random if omitted)")
    c.add_argument("--kind", choices=codes.KINDS, default="percent")
    c.add_argument("--value", type=float, help="percent off, or amount off for 'fixed' (e.g. 5.00)")
    c.add_argument("--sites", default="", help="comma separated site ids, or 'all' (default: every site)")
    c.add_argument("--offers", default="", help="comma separated offer ids it discounts (default: all)")
    c.add_argument("--max-uses", type=int)
    c.add_argument("--expires", help="YYYY-MM-DD")
    c.add_argument("--days", type=int, help="free codes: days of access (no end date if omitted)")
    c.add_argument("--duration", choices=codes.DURATIONS, default="once")
    c.add_argument("--months", type=int, help="months, for --duration repeating")
    c.add_argument("--count", type=int, default=1, help="generate several random codes")
    c.add_argument("--note")
    for action in ("enable", "disable", "delete"):
        csub.add_parser(action).add_argument("code")
    p.set_defaults(func=cmd_codes)

    p = sub.add_parser("grant", help="give someone access")
    p.add_argument("email")
    p.add_argument("--sites", required=True, help="comma separated site ids, or 'all'")
    p.add_argument("--days", type=int)
    p.add_argument("--note")
    p.set_defaults(func=cmd_grant)
    p = sub.add_parser("revoke", help="remove all access of a customer")
    p.add_argument("email")
    p.set_defaults(func=cmd_revoke)
    p = sub.add_parser("customers")
    p.add_argument("query", nargs="?")
    p.set_defaults(func=cmd_customers)

    p = sub.add_parser("deploy-worker")
    p.add_argument("--if-configured", action="store_true", help="do nothing (and succeed) if not set up yet")
    p.set_defaults(func=cmd_deploy_worker)
    sub.add_parser("undeploy-worker").set_defaults(func=cmd_undeploy_worker)
    sub.add_parser("worker-status").set_defaults(func=cmd_worker_status)
    sub.add_parser("wrangler-toml").set_defaults(func=cmd_wrangler)

    args = parser.parse_args(argv)
    if getattr(args, "kind", None) == "percent" and args.value is not None:
        args.value = int(args.value)
    args.func(args)


if __name__ == "__main__":
    main()
