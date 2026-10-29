// Run with: node --test tests/worker.test.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import worker, { matchSite, signToken, verifyToken } from "../src/worker/paywall-worker.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const SITES = [
  { id: "blog", routes: ["blog.example.com/*"], bypass: ["/robots.txt", "/.well-known/*"], cookie: "__Host-paywall-blog" },
  { id: "news", routes: ["news.example.org/premium/*"], bypass: [], cookie: "__Host-paywall-news" },
];
const env = {
  PAYWALL_URL: "https://pay.example.com",
  PAYWALL_SECRET: "s3cret-signing-key",
  ORIGIN_SECRET: "origin-secret",
  CALLBACK_PREFIX: "/__paywall",
  SITES: JSON.stringify(SITES),
};

let forwarded = [];
globalThis.fetch = async (request) => {
  forwarded.push(request);
  return new Response("origin content", { status: 200 });
};

function pythonHandoff(site, returnUrl, accessUntil) {
  const code = `
import sys
from paywall import tokens
print(tokens.handoff(sys.argv[1], "c_python", sys.argv[2], int(sys.argv[4]), sys.argv[3]))`;
  return execFileSync("python3", ["-c", code, env.PAYWALL_SECRET, site, returnUrl, String(accessUntil)], {
    env: { ...process.env, PYTHONPATH: join(root, "src") },
  })
    .toString()
    .trim();
}

const now = () => Math.floor(Date.now() / 1000);
const cookieValue = (setCookie) => setCookie.split(";")[0].split("=").slice(1).join("=");

test("requests are matched to the most specific site", () => {
  const sites = SITES.map((s) => ({ ...s, routes: s.routes.map((r) => ({ host: r.split("/")[0], path: "/" + r.split("/").slice(1).join("/") })) }));
  assert.equal(matchSite(new URL("https://blog.example.com/a"), sites).site.id, "blog");
  const news = matchSite(new URL("https://news.example.org/premium/x"), sites);
  assert.equal(news.site.id, "news");
  assert.equal(news.base, "/premium");
  assert.equal(matchSite(new URL("https://news.example.org/free"), sites), null);
});

test("visitors without a cookie go to their site's paywall page", async () => {
  forwarded = [];
  const res = await worker.fetch(new Request("https://news.example.org/premium/story?id=1"), env);
  assert.equal(res.status, 302);
  assert.equal(
    res.headers.get("Location"),
    "https://pay.example.com/s/news?return=" + encodeURIComponent("https://news.example.org/premium/story?id=1"),
  );
  assert.equal(forwarded.length, 0);
});

test("non-GET requests get 402", async () => {
  const res = await worker.fetch(new Request("https://blog.example.com/api", { method: "POST" }), env);
  assert.equal(res.status, 402);
});

test("public paths are per site", async () => {
  forwarded = [];
  const res = await worker.fetch(new Request("https://blog.example.com/.well-known/acme"), env);
  assert.equal(res.status, 200);
  assert.equal(forwarded[0].headers.get("X-Paywall-Origin-Key"), "origin-secret");
  const news = await worker.fetch(new Request("https://news.example.org/premium/robots.txt"), env);
  assert.equal(news.status, 302);
});

test("a Python handoff for a path-scoped site sets that site's cookie", async () => {
  const until = now() + 3600;
  const token = pythonHandoff("news", "https://news.example.org/premium/story?x=y", until);
  const res = await worker.fetch(new Request(`https://news.example.org/premium/__paywall/auth?t=${token}`), env);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("Location"), "/premium/story?x=y");
  const cookie = res.headers.get("Set-Cookie");
  assert.match(cookie, /^__Host-paywall-news=[^;]+; Path=\/; Max-Age=\d+; HttpOnly; Secure; SameSite=Lax$/);
  const access = await verifyToken(cookieValue(cookie), env.PAYWALL_SECRET, "access");
  assert.equal(access.sub, "c_python");
  assert.equal(access.site, "news");
  assert.equal(access.exp, until);

  forwarded = [];
  const ok = await worker.fetch(
    new Request("https://news.example.org/premium/story", {
      headers: { Cookie: `theme=dark; __Host-paywall-news=${cookieValue(cookie)}` },
    }),
    env,
  );
  assert.equal(ok.status, 200);
  const sent = forwarded[0];
  assert.equal(sent.headers.get("Cookie"), "theme=dark");
  assert.equal(sent.headers.get("X-Paywall-User"), "c_python");
  assert.equal(sent.headers.get("X-Paywall-Site"), "news");
  assert.equal(sent.headers.get("X-Paywall-Origin-Key"), "origin-secret");
});

test("a handoff for one site is refused by another", async () => {
  const token = pythonHandoff("blog", "https://news.example.org/premium/", now() + 60);
  const res = await worker.fetch(new Request(`https://news.example.org/premium/__paywall/auth?t=${token}`), env);
  assert.equal(res.headers.get("Location"), "https://pay.example.com/s/news");
  assert.equal(res.headers.get("Set-Cookie"), null);
});

test("an access cookie for one site does not open another", async () => {
  const blogAccess = await signToken({ v: 1, typ: "access", sub: "x", site: "blog", exp: now() + 60 }, env.PAYWALL_SECRET);
  // even when copied under the other site's cookie name
  const res = await worker.fetch(
    new Request("https://news.example.org/premium/", { headers: { Cookie: `__Host-paywall-news=${blogAccess}` } }),
    env,
  );
  assert.equal(res.status, 302);
  const ok = await worker.fetch(
    new Request("https://blog.example.com/", { headers: { Cookie: `__Host-paywall-blog=${blogAccess}` } }),
    env,
  );
  assert.equal(ok.status, 200);
});

test("handoff to another origin falls back to the site's base path", async () => {
  const token = pythonHandoff("news", "https://evil.example.net/", now() + 60);
  const res = await worker.fetch(new Request(`https://news.example.org/premium/__paywall/auth?t=${token}`), env);
  assert.equal(res.headers.get("Location"), "/premium/");
});

test("forged, expired or wrong-type tokens are rejected", async () => {
  const forged = await signToken({ v: 1, typ: "access", sub: "x", site: "blog", exp: now() + 60 }, "wrong-secret");
  const expired = await signToken({ v: 1, typ: "access", sub: "x", site: "blog", exp: now() - 1 }, env.PAYWALL_SECRET);
  const handoff = await signToken(
    { v: 1, typ: "handoff", sub: "x", site: "blog", exp: now() + 60, aexp: now() + 60 },
    env.PAYWALL_SECRET,
  );
  for (const value of [forged, expired, handoff, "garbage", "a.b"]) {
    const res = await worker.fetch(
      new Request("https://blog.example.com/", { headers: { Cookie: `__Host-paywall-blog=${value}` } }),
      env,
    );
    assert.equal(res.status, 302, value);
  }
});

test("spoofed paywall headers from the client are dropped", async () => {
  forwarded = [];
  await worker.fetch(
    new Request("https://blog.example.com/robots.txt", { headers: { "X-Paywall-User": "admin", "X-Paywall-Site": "blog" } }),
    env,
  );
  assert.equal(forwarded[0].headers.get("X-Paywall-User"), null);
  assert.equal(forwarded[0].headers.get("X-Paywall-Site"), null);
});

test("logout clears only that site's cookie", async () => {
  const res = await worker.fetch(new Request("https://news.example.org/premium/__paywall/logout"), env);
  assert.match(res.headers.get("Set-Cookie"), /^__Host-paywall-news=; .*Max-Age=0/);
  assert.equal(res.headers.get("Location"), "https://pay.example.com/s/news");
});

test("requests no site claims fail closed", async () => {
  forwarded = [];
  const res = await worker.fetch(new Request("https://unknown.example.com/"), env);
  assert.equal(res.status, 302);
  assert.equal(forwarded.length, 0);
});
