/**
 * Paywall Worker
 *
 * Runs on Cloudflare in front of the protected sites. Each request is matched
 * to one site (most specific route wins); visitors need a valid access cookie
 * for that site, everyone else is sent to that site's paywall page on
 * YunoHost. Tokens are HMAC-SHA256 signed with PAYWALL_SECRET (same scheme as
 * src/paywall/tokens.py), so no call to the paywall server is needed to
 * check access.
 *
 * Bindings (set automatically when deployed from the YunoHost app):
 *   PAYWALL_URL      public URL of the paywall, e.g. https://pay.example.com
 *   PAYWALL_SECRET   secret shared with the paywall (secret binding)
 *   ORIGIN_SECRET    optional, sent to the origin as X-Paywall-Origin-Key
 *   CALLBACK_PREFIX  reserved path segment on the sites (default "/__paywall")
 *   SITES            JSON list of {id, routes, bypass, cookie}
 */

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const keyCache = new Map();
let sitesCache = { raw: null, parsed: [] };

function b64urlToBytes(value) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function bytesToB64url(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function hmacKey(secret) {
  if (!keyCache.has(secret)) {
    keyCache.set(
      secret,
      crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
        "sign",
        "verify",
      ]),
    );
  }
  return keyCache.get(secret);
}

export async function signToken(payload, secret) {
  const body = bytesToB64url(encoder.encode(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign("HMAC", await hmacKey(secret), encoder.encode(body));
  return `${body}.${bytesToB64url(signature)}`;
}

export async function verifyToken(token, secret, type, now = Date.now() / 1000) {
  if (!token || !secret) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  try {
    const valid = await crypto.subtle.verify("HMAC", await hmacKey(secret), b64urlToBytes(parts[1]), encoder.encode(parts[0]));
    if (!valid) return null;
    const payload = JSON.parse(decoder.decode(b64urlToBytes(parts[0])));
    if (!payload || payload.v !== 1 || payload.typ !== type) return null;
    if (typeof payload.exp !== "number" || payload.exp < now) return null;
    return payload;
  } catch {
    return null;
  }
}

function parseRoute(pattern) {
  const slash = pattern.indexOf("/");
  const host = slash === -1 ? pattern : pattern.slice(0, slash);
  const path = slash === -1 ? "/*" : pattern.slice(slash);
  return { host, path };
}

function hostMatches(host, pattern) {
  if (pattern.startsWith("*.")) return host === pattern.slice(2) || host.endsWith(pattern.slice(1));
  return host === pattern;
}

function pathMatches(path, pattern) {
  return pattern.endsWith("*") ? path.startsWith(pattern.slice(0, -1)) : path === pattern;
}

function siteList(env) {
  const raw = env.SITES || "[]";
  if (sitesCache.raw !== raw) {
    let parsed = [];
    try {
      parsed = JSON.parse(raw).map((site) => ({ ...site, routes: (site.routes || []).map(parseRoute) }));
    } catch {
      parsed = [];
    }
    sitesCache = { raw, parsed };
  }
  return sitesCache.parsed;
}

/** The site covering this URL and the base path of the matching route. */
export function matchSite(url, sites) {
  let best = null;
  for (const site of sites) {
    for (const route of site.routes) {
      if (!hostMatches(url.hostname, route.host) || !pathMatches(url.pathname, route.path)) continue;
      const score = route.path.length * 2 + (route.host.startsWith("*.") ? 0 : 1);
      if (!best || score > best.score) {
        best = { site, score, base: route.path.replace(/\*$/, "").replace(/\/$/, "") };
      }
    }
  }
  return best;
}

function settings(env) {
  return {
    paywallUrl: (env.PAYWALL_URL || "").replace(/\/+$/, ""),
    secret: env.PAYWALL_SECRET || "",
    originSecret: env.ORIGIN_SECRET || "",
    prefix: env.CALLBACK_PREFIX || "/__paywall",
    sites: siteList(env),
  };
}

function readCookie(header, name) {
  for (const part of (header || "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0 && part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return null;
}

function withoutPaywallCookies(header, sites) {
  const names = new Set(sites.map((site) => site.cookie));
  return (header || "")
    .split(";")
    .filter((part) => !names.has(part.split("=")[0].trim()))
    .join(";")
    .trim();
}

function isBypassed(path, patterns) {
  return (patterns || []).some((pattern) =>
    pattern.endsWith("*") ? path.startsWith(pattern.slice(0, -1)) : path === pattern,
  );
}

function redirect(location, extraHeaders = {}) {
  return new Response(null, {
    status: 302,
    headers: { Location: location, "Cache-Control": "no-store", ...extraHeaders },
  });
}

function paywallPage(cfg, site, url) {
  const base = `${cfg.paywallUrl}/s/${encodeURIComponent(site.id)}`;
  return url ? `${base}?return=${encodeURIComponent(url.href)}` : base;
}

function forward(request, cfg, user) {
  const headers = new Headers(request.headers);
  const cookies = withoutPaywallCookies(headers.get("Cookie"), cfg.sites);
  if (cookies) headers.set("Cookie", cookies);
  else headers.delete("Cookie");
  headers.delete("X-Paywall-User");
  headers.delete("X-Paywall-Site");
  headers.delete("X-Paywall-Origin-Key");
  if (user) {
    headers.set("X-Paywall-User", user.sub);
    headers.set("X-Paywall-Site", user.site);
  }
  if (cfg.originSecret) headers.set("X-Paywall-Origin-Key", cfg.originSecret);
  return fetch(new Request(request, { headers }));
}

async function handleAuth(url, cfg, match) {
  const handoff = await verifyToken(url.searchParams.get("t"), cfg.secret, "handoff");
  if (!handoff || handoff.site !== match.site.id || typeof handoff.aexp !== "number") {
    return redirect(paywallPage(cfg, match.site));
  }
  let target = `${match.base}/`;
  try {
    const wanted = new URL(handoff.r);
    const sameSite = matchSite(wanted, cfg.sites);
    if (wanted.origin === url.origin && sameSite && sameSite.site.id === match.site.id && !wanted.pathname.includes(`${cfg.prefix}/`)) {
      target = wanted.pathname + wanted.search + wanted.hash;
    }
  } catch {
    // keep the site's base path
  }
  const now = Math.floor(Date.now() / 1000);
  const maxAge = Math.floor(handoff.aexp - now);
  if (maxAge <= 0) return redirect(paywallPage(cfg, match.site));
  const access = await signToken(
    { v: 1, typ: "access", sub: handoff.sub, site: match.site.id, exp: handoff.aexp, iat: now },
    cfg.secret,
  );
  const cookie = `${match.site.cookie}=${access}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
  return redirect(target, { "Set-Cookie": cookie });
}

export default {
  async fetch(request, env) {
    const cfg = settings(env);
    const url = new URL(request.url);

    if (!cfg.paywallUrl || !cfg.secret) {
      return new Response("Paywall is not configured", { status: 503 });
    }
    const match = matchSite(url, cfg.sites);
    if (!match) {
      // A route points here but no active site claims it: fail closed.
      return redirect(`${cfg.paywallUrl}/`);
    }
    if (url.pathname === `${match.base}${cfg.prefix}/auth`) {
      return handleAuth(url, cfg, match);
    }
    if (url.pathname === `${match.base}${cfg.prefix}/logout`) {
      return redirect(paywallPage(cfg, match.site), {
        "Set-Cookie": `${match.site.cookie}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
      });
    }
    if (isBypassed(url.pathname, match.site.bypass)) {
      return forward(request, cfg, null);
    }

    const access = await verifyToken(readCookie(request.headers.get("Cookie"), match.site.cookie), cfg.secret, "access");
    if (access && access.site === match.site.id) {
      return forward(request, cfg, access);
    }

    if (request.method === "GET" || request.method === "HEAD") {
      return redirect(paywallPage(cfg, match.site, url));
    }
    return new Response(JSON.stringify({ error: "payment_required", paywall: paywallPage(cfg, match.site, url) }), {
      status: 402,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  },
};
