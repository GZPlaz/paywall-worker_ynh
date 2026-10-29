"""Deploy the paywall Worker to Cloudflare and bind it to the protected routes.

The API token needs these permissions:
  * Account - Workers Scripts - Edit
  * Zone - Workers Routes - Edit (for the zones of the protected sites)
  * Zone - Zone - Read
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import sites

API_BASE = "https://api.cloudflare.com/client/v4"
COMPATIBILITY_DATE = "2024-09-23"
WORKER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker", "paywall-worker.js")
STATUS_FILE = "cloudflare_status.txt"


class CloudflareError(Exception):
    pass


class Cloudflare:
    def __init__(self, token, timeout=30):
        if not token:
            raise CloudflareError("Cloudflare API token is not configured")
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body=None, content_type="application/json"):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(API_BASE + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except Exception:
                raise CloudflareError(f"{method} {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CloudflareError(f"Could not reach Cloudflare: {exc}") from exc
        if not payload.get("success", False):
            errors = "; ".join(e.get("message", str(e)) for e in payload.get("errors", [])) or "unknown error"
            raise CloudflareError(f"{method} {path}: {errors}")
        return payload.get("result")

    def zone_id_for(self, host):
        """Find the zone containing ``host`` by trying each parent domain."""
        labels = host.lstrip("*.").split(".")
        for i in range(len(labels) - 1):
            name = ".".join(labels[i:])
            result = self.request("GET", f"/zones?name={name}")
            if result:
                return result[0]["id"]
        raise CloudflareError(f"No Cloudflare zone found for {host} (is the domain on this Cloudflare account?)")

    def upload_worker(self, account_id, name, script, bindings):
        boundary = uuid.uuid4().hex
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": COMPATIBILITY_DATE,
            "bindings": bindings,
        }
        parts = [
            ("metadata", "metadata.json", "application/json", json.dumps(metadata).encode()),
            ("worker.js", "worker.js", "application/javascript+module", script.encode()),
        ]
        body = b""
        for field, filename, ctype, content in parts:
            body += (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self.request("PUT", f"/accounts/{account_id}/workers/scripts/{name}", body,
                            f"multipart/form-data; boundary={boundary}")

    def delete_worker(self, account_id, name):
        return self.request("DELETE", f"/accounts/{account_id}/workers/scripts/{name}?force=true")

    def list_routes(self, zone_id):
        return self.request("GET", f"/zones/{zone_id}/workers/routes") or []

    def put_route(self, zone_id, pattern, script, route_id=None):
        body = {"pattern": pattern, "script": script}
        if route_id:
            return self.request("PUT", f"/zones/{zone_id}/workers/routes/{route_id}", body)
        return self.request("POST", f"/zones/{zone_id}/workers/routes", body)

    def delete_route(self, zone_id, route_id):
        return self.request("DELETE", f"/zones/{zone_id}/workers/routes/{route_id}")


def worker_source():
    with open(WORKER_FILE, encoding="utf-8") as fh:
        return fh.read()


def worker_vars(cfg, conn):
    """Plain-text settings for the Worker (secrets are handled separately)."""
    return {
        "PAYWALL_URL": cfg.public_url,
        "CALLBACK_PREFIX": cfg["callback_prefix"],
        "SITES": json.dumps(sites.worker_config(conn, cfg["cookie_name"]), separators=(",", ":")),
    }


def all_routes(conn):
    return [(pattern, host) for site in sites.list_all(conn, active_only=True) for pattern, host, _ in site.route_list]


def bindings(cfg, conn):
    result = [{"type": "plain_text", "name": k, "text": v} for k, v in worker_vars(cfg, conn).items()]
    result.append({"type": "secret_text", "name": "PAYWALL_SECRET", "text": cfg["signing_secret"]})
    if cfg["origin_secret"]:
        result.append({"type": "secret_text", "name": "ORIGIN_SECRET", "text": cfg["origin_secret"]})
    return result


def wrangler_toml(cfg, conn):
    """Configuration for people who prefer deploying with ``wrangler``."""
    lines = [
        f'name = "{cfg["worker_name"]}"',
        'main = "paywall-worker.js"',
        f'compatibility_date = "{COMPATIBILITY_DATE}"',
        "",
        "routes = [",
    ]
    for pattern, _ in all_routes(conn):
        lines.append(f'  {{ pattern = "{pattern}", zone_name = "<your zone, e.g. example.com>" }},')
    lines += ["]", "", "[vars]"]
    for key, value in worker_vars(cfg, conn).items():
        lines.append(f"{key} = {json.dumps(value)}")
    lines += [
        "",
        "# Secrets: run these once and paste the values from the paywall admin page",
        "#   wrangler secret put PAYWALL_SECRET",
        "#   wrangler secret put ORIGIN_SECRET",
        "",
    ]
    return "\n".join(lines)


def _zones_for_routes(cf, cfg, conn):
    zones = {}
    for pattern, host in all_routes(conn):
        zone_id = cfg["cf_zone_id"] or cf.zone_id_for(host)
        zones.setdefault(zone_id, []).append(pattern)
    return zones


def deploy(cfg, conn, log=print):
    """Upload the Worker with the current sites and make Cloudflare routes
    match them exactly (stale routes of this Worker are removed)."""
    if not cfg.cloudflare_ready:
        raise CloudflareError("Set the Cloudflare API token and account ID first")
    routes = all_routes(conn)
    if not routes:
        raise CloudflareError("No active site has routes yet")
    cf = Cloudflare(cfg["cf_api_token"])
    name = cfg["worker_name"]
    log(f"Uploading Worker '{name}'...")
    cf.upload_worker(cfg["cf_account_id"], name, worker_source(), bindings(cfg, conn))
    wanted_zones = _zones_for_routes(cf, cfg, conn)
    for zone_id, patterns in wanted_zones.items():
        existing = cf.list_routes(zone_id)
        by_pattern = {r["pattern"]: r for r in existing}
        for pattern in patterns:
            route = by_pattern.get(pattern)
            if route and route.get("script") == name:
                log(f"Route {pattern} already points to {name}")
                continue
            if route:
                log(f"Route {pattern} currently uses '{route.get('script') or 'no worker'}', switching it to {name}")
            else:
                log(f"Creating route {pattern}")
            cf.put_route(zone_id, pattern, name, route["id"] if route else None)
        for route in existing:
            if route.get("script") == name and route["pattern"] not in patterns:
                log(f"Removing stale route {route['pattern']}")
                cf.delete_route(zone_id, route["id"])
    write_status(cfg, f"Deployed '{name}' on {', '.join(p for p, _ in routes)}")
    log("Done.")


def undeploy(cfg, conn, log=print):
    if not cfg.cloudflare_ready:
        return
    cf = Cloudflare(cfg["cf_api_token"])
    name = cfg["worker_name"]
    zone_ids = {cfg["cf_zone_id"]} if cfg["cf_zone_id"] else set()
    if not cfg["cf_zone_id"]:
        for site in sites.list_all(conn):
            for _, host, _ in site.route_list:
                try:
                    zone_ids.add(cf.zone_id_for(host))
                except CloudflareError as exc:
                    log(str(exc))
    for zone_id in zone_ids:
        for route in cf.list_routes(zone_id):
            if route.get("script") == name:
                log(f"Removing route {route['pattern']}")
                cf.delete_route(zone_id, route["id"])
    log(f"Deleting Worker '{name}'")
    try:
        cf.delete_worker(cfg["cf_account_id"], name)
    except CloudflareError as exc:
        log(str(exc))
    write_status(cfg, "Not deployed")


def write_status(cfg, message):
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    try:
        with open(os.path.join(cfg["data_dir"], STATUS_FILE), "w", encoding="utf-8") as fh:
            fh.write(f"{message} ({stamp})\n")
    except OSError:
        pass


def read_status(cfg):
    try:
        with open(os.path.join(cfg["data_dir"], STATUS_FILE), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "Never deployed"
