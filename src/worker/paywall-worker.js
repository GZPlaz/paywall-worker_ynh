/**
 * Paywall Worker
 *
 * Runs on Cloudflare in front of the protected site(s). Visitors need a valid
 * access cookie; everyone else is sent to the paywall hosted on YunoHost.
 * Tokens are HMAC-SHA256 signed with PAYWALL_SECRET (same scheme as
 * src/paywall/tokens.py), so no call to the paywall server is needed to
 * check access.
 *
 * Bindings (set automatically when deployed from the YunoHost app):
 *   PAYWALL_URL      public URL of the paywall, e.g. https://pay.example.com
 *   PAYWALL_SECRET   secret shared with the paywall (secret binding)
 *   ORIGIN_SECRET    optional, sent to the origin as X-Paywall-Origin-Key
 *   COOKIE_NAME      access cookie name (default "__Host-paywall")
 *   CALLBACK_PREFIX  reserved path prefix on the site (default "/__paywall")
 *   BYPASS_PATHS     newline separated paths that stay public ("*" suffix = prefix match)
 */

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const keyCache = new Map();

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

function settings(env) {
  return {
    paywallUrl: (env.PAYWALL_URL || "").replace(/\/+$/, ""),
    secret: env.PAYWALL_SECRET || "",
    originSecret: env.ORIGIN_SECRET || "",
    cookieName: env.COOKIE_NAME || "__Host-paywall",
    prefix: env.CALLBACK_PREFIX || "/__paywall",
    bypass: (env.BYPASS_PATHS || "")
      .split(/[\n,]/)
      .map((p) => p.trim())
      .filter(Boolean),
  };
}

function readCookie(header, name) {
  for (const part of (header || "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0 && part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return null;
}

function withoutCookie(header, name) {
  return (header || "")
    .split(";")
    .filter((part) => part.split("=")[0].trim() !== name)
    .join(";")
    .trim();
}

function isBypassed(path, patterns) {
  return patterns.some((pattern) =>
    pattern.endsWith("*") ? path.startsWith(pattern.slice(0, -1)) : path === pattern,
  );
}

function redirect(location, extraHeaders = {}) {
  return new Response(null, {
    status: 302,
    headers: { Location: location, "Cache-Control": "no-store", ...extraHeaders },
  });
}

function toPaywall(url, cfg) {
  return `${cfg.paywallUrl}/?return=${encodeURIComponent(url.href)}`;
}

function forward(request, cfg, user) {
  const headers = new Headers(request.headers);
  const cookies = withoutCookie(headers.get("Cookie"), cfg.cookieName);
  if (cookies) headers.set("Cookie", cookies);
  else headers.delete("Cookie");
  headers.delete("X-Paywall-User");
  headers.delete("X-Paywall-Origin-Key");
  if (user) headers.set("X-Paywall-User", user);
  if (cfg.originSecret) headers.set("X-Paywall-Origin-Key", cfg.originSecret);
  return fetch(new Request(request, { headers }));
}

async function handleAuth(url, cfg) {
  const handoff = await verifyToken(url.searchParams.get("t"), cfg.secret, "handoff");
  if (!handoff || typeof handoff.aexp !== "number") return redirect(`${cfg.paywallUrl}/`);
  let target = "/";
  try {
    const wanted = new URL(handoff.r);
    if (wanted.origin === url.origin && !wanted.pathname.startsWith(cfg.prefix + "/")) {
      target = wanted.pathname + wanted.search + wanted.hash;
    }
  } catch {
    // keep "/"
  }
  const now = Math.floor(Date.now() / 1000);
  const maxAge = Math.floor(handoff.aexp - now);
  if (maxAge <= 0) return redirect(`${cfg.paywallUrl}/`);
  const access = await signToken({ v: 1, typ: "access", sub: handoff.sub, exp: handoff.aexp, iat: now }, cfg.secret);
  const cookie = `${cfg.cookieName}=${access}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
  return redirect(target, { "Set-Cookie": cookie });
}

export default {
  async fetch(request, env) {
    const cfg = settings(env);
    const url = new URL(request.url);

    if (!cfg.paywallUrl || !cfg.secret) {
      return new Response("Paywall is not configured", { status: 503 });
    }
    if (url.pathname === `${cfg.prefix}/auth`) {
      return handleAuth(url, cfg);
    }
    if (url.pathname === `${cfg.prefix}/logout`) {
      return redirect(`${cfg.paywallUrl}/`, {
        "Set-Cookie": `${cfg.cookieName}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
      });
    }
    if (isBypassed(url.pathname, cfg.bypass)) {
      return forward(request, cfg, null);
    }

    const access = await verifyToken(readCookie(request.headers.get("Cookie"), cfg.cookieName), cfg.secret, "access");
    if (access) {
      return forward(request, cfg, access.sub);
    }

    if (request.method === "GET" || request.method === "HEAD") {
      return redirect(toPaywall(url, cfg));
    }
    return new Response(JSON.stringify({ error: "payment_required", paywall: toPaywall(url, cfg) }), {
      status: 402,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  },
};
