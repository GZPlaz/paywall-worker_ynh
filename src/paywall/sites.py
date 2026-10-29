"""Protected sites: each has its own paywall page settings and routes."""

import re
import time
from urllib.parse import urlsplit

DEFAULT_BYPASS = "/robots.txt\n/favicon.ico\n/.well-known/*"
DEFAULT_ACCENT = "#1b2a41"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ROUTE_RE = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z0-9-]+(/[^\s]*)?$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class SiteError(ValueError):
    pass


def parse_routes(text, paywall_domain=None):
    """Parse Cloudflare route patterns, one per line, into
    (pattern, host, path) tuples. Raises SiteError on bad input."""
    routes = []
    for line in (text or "").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        pattern = re.sub(r"^https?://", "", pattern).lower()
        if "/" not in pattern:
            pattern += "/*"
        if not _ROUTE_RE.match(pattern):
            raise SiteError(f"'{line.strip()}' is not a valid route (example: blog.example.com/*)")
        host, _, path = pattern.partition("/")
        if paywall_domain and host_matches(paywall_domain.lower(), host):
            raise SiteError(
                f"The route '{pattern}' would cover the paywall itself ({paywall_domain}). "
                "The paywall needs its own (sub)domain."
            )
        routes.append((pattern, host, "/" + path))
    return routes


def host_matches(host, pattern_host):
    if pattern_host.startswith("*."):
        return host.endswith(pattern_host[1:]) or host == pattern_host[2:]
    return host == pattern_host


def path_matches(path, pattern_path):
    if pattern_path.endswith("*"):
        return path.startswith(pattern_path[:-1])
    return path == pattern_path


def route_base(pattern_path):
    """Path prefix a route covers, without the trailing '*' or '/'."""
    return pattern_path.rstrip("*").rstrip("/")


def ink_for(color):
    """Text color that stays readable on a button of ``color``."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = sum(w * (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
              for w, c in zip((0.2126, 0.7152, 0.0722), (r, g, b)))
    return "#111827" if lum > 0.4 else "#ffffff"


class Site(dict):
    """A ``sites`` row plus helpers."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    @property
    def route_list(self):
        return parse_routes(self["routes"])

    @property
    def accent_ink(self):
        return ink_for(self["accent_color"])

    def match(self, url):
        """Return the route base path if ``url`` is covered by this site."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        host, path = parts.hostname.lower(), parts.path or "/"
        best = None
        for _, route_host, route_path in self.route_list:
            if host_matches(host, route_host) and path_matches(path, route_path):
                if best is None or len(route_path) > len(best):
                    best = route_path
        return None if best is None else route_base(best)

    def default_return(self):
        for _, host, path in self.route_list:
            if not host.startswith("*."):
                return f"https://{host}{route_base(path)}/"
        return None


def _site(row):
    return Site(dict(row)) if row is not None else None


def get(db, site_id):
    return _site(db.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone())


def list_all(db, active_only=False):
    sql = "SELECT * FROM sites" + (" WHERE active = 1" if active_only else "") + " ORDER BY name COLLATE NOCASE"
    return [_site(row) for row in db.execute(sql).fetchall()]


def ids(db, active_only=False):
    return [s.id for s in list_all(db, active_only)]


def for_url(db, url):
    """The active site covering ``url`` (most specific route wins)."""
    best, best_len = None, -1
    for site in list_all(db, active_only=True):
        base = site.match(url)
        if base is not None and len(base) > best_len:
            best, best_len = site, len(base)
    return best


def validate_all_routes(db, paywall_domain, changed=None):
    """Check that no route is used twice and none covers the paywall.

    ``changed`` is an unsaved site dict replacing the stored one with the
    same id. Returns the list of (site, pattern) for all active sites."""
    sites = {s.id: s for s in list_all(db)}
    if changed is not None:
        sites[changed["id"]] = Site(changed)
    seen = {}
    for site in sites.values():
        for pattern, _, _ in parse_routes(site["routes"], paywall_domain):
            if pattern in seen and seen[pattern] != site["id"]:
                raise SiteError(f"The route '{pattern}' is already used by the site '{seen[pattern]}'.")
            seen[pattern] = site["id"]
    return seen


def save(db, values, paywall_domain, offer_ids=None, create=False):
    """Insert or update a site from form-like ``values``. Returns the Site."""
    site_id = (values.get("id") or "").strip().lower()
    if not _ID_RE.match(site_id):
        raise SiteError("The short name must be 1-32 lowercase letters, digits or dashes, e.g. 'blog'.")
    existing = get(db, site_id)
    if create and existing is not None:
        raise SiteError(f"A site called '{site_id}' already exists.")
    if not create and existing is None:
        raise SiteError(f"No site called '{site_id}'.")
    accent = (values.get("accent_color") or DEFAULT_ACCENT).strip()
    if not _COLOR_RE.match(accent):
        raise SiteError("The button color must look like #1b2a41.")
    try:
        recheck = int(values.get("recheck_hours") or 24)
    except ValueError as exc:
        raise SiteError("Re-check interval must be a whole number of hours.") from exc
    row = {
        "id": site_id,
        "name": (values.get("name") or "").strip() or site_id,
        "title": (values.get("title") or "").strip() or "Members only",
        "description": (values.get("description") or "").replace("\r\n", "\n").strip(),
        "accent_color": accent.lower(),
        "support_email": (values.get("support_email") or "").strip(),
        "routes": "\n".join(p for p, _, _ in parse_routes(values.get("routes"), paywall_domain)),
        "bypass_paths": (values.get("bypass_paths") or "").replace("\r\n", "\n").strip(),
        "recheck_hours": max(1, min(8760, recheck)),
        "show_codes": 1 if values.get("show_codes") else 0,
        "active": 1 if values.get("active") else 0,
    }
    validate_all_routes(db, paywall_domain, row)
    if create:
        db.execute(
            """INSERT INTO sites (id, name, title, description, accent_color, support_email, routes,
                                  bypass_paths, recheck_hours, show_codes, active, created_at)
               VALUES (:id, :name, :title, :description, :accent_color, :support_email, :routes,
                       :bypass_paths, :recheck_hours, :show_codes, :active, :created_at)""",
            dict(row, created_at=int(time.time())),
        )
    else:
        db.execute(
            """UPDATE sites SET name = :name, title = :title, description = :description,
                   accent_color = :accent_color, support_email = :support_email, routes = :routes,
                   bypass_paths = :bypass_paths, recheck_hours = :recheck_hours,
                   show_codes = :show_codes, active = :active WHERE id = :id""",
            row,
        )
    if offer_ids is not None:
        db.execute("DELETE FROM offer_sites WHERE site_id = ?", (site_id,))
        db.executemany(
            "INSERT OR IGNORE INTO offer_sites (offer_id, site_id) VALUES (?, ?)",
            [(int(o), site_id) for o in offer_ids],
        )
    return get(db, site_id)


def delete(db, site_id):
    db.execute("DELETE FROM sites WHERE id = ?", (site_id,))


def worker_config(db, cookie_prefix):
    """What the Worker needs to know about each active site."""
    result = []
    for site in list_all(db, active_only=True):
        routes = [p for p, _, _ in site.route_list]
        if not routes:
            continue
        result.append({
            "id": site.id,
            "routes": routes,
            "bypass": [p.strip() for p in site.bypass_paths.splitlines() if p.strip()],
            "cookie": f"{cookie_prefix}-{site.id}",
        })
    return result
