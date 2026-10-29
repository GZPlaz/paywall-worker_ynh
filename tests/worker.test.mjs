// Run with: node --test tests/worker.test.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import worker, { signToken, verifyToken } from "../src/worker/paywall-worker.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const env = {
  PAYWALL_URL: "https://pay.example.com",
  PAYWALL_SECRET: "s3cret-signing-key",
  ORIGIN_SECRET: "origin-secret",
  COOKIE_NAME: "__Host-paywall",
  CALLBACK_PREFIX: "/__paywall",
  BYPASS_PATHS: "/robots.txt\n/.well-known/*",
};

let forwarded = [];
globalThis.fetch = async (request) => {
  forwarded.push(request);
  return new Response("origin content", { status: 200 });
};

function pythonHandoff(returnUrl, accessUntil) {
  const code = `
import sys
from paywall import tokens
print(tokens.handoff(sys.argv[1], "c_python", int(sys.argv[3]), sys.argv[2]))`;
  return execFileSync("python3", ["-c", code, env.PAYWALL_SECRET, returnUrl, String(accessUntil)], {
    env: { ...process.env, PYTHONPATH: join(root, "src") },
  })
    .toString()
    .trim();
}

const now = () => Math.floor(Date.now() / 1000);

test("visitors without a cookie are sent to the paywall", async () => {
  forwarded = [];
  const res = await worker.fetch(new Request("https://blog.example.com/post?id=1"), env);
  assert.equal(res.status, 302);
  assert.equal(
    res.headers.get("Location"),
    "https://pay.example.com/?return=" + encodeURIComponent("https://blog.example.com/post?id=1"),
  );
  assert.equal(forwarded.length, 0);
});

test("non-GET requests get 402", async () => {
  const res = await worker.fetch(new Request("https://blog.example.com/api", { method: "POST" }), env);
  assert.equal(res.status, 402);
});

test("bypass paths reach the origin", async () => {
  forwarded = [];
  const res = await worker.fetch(new Request("https://blog.example.com/.well-known/acme"), env);
  assert.equal(res.status, 200);
  assert.equal(forwarded[0].headers.get("X-Paywall-Origin-Key"), "origin-secret");
});

test("a handoff token minted by the Python paywall sets the access cookie", async () => {
  const until = now() + 3600;
  const token = pythonHandoff("https://blog.example.com/post/1?x=y", until);
  const res = await worker.fetch(new Request(`https://blog.example.com/__paywall/auth?t=${token}`), env);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("Location"), "/post/1?x=y");
  const cookie = res.headers.get("Set-Cookie");
  assert.match(cookie, /^__Host-paywall=[^;]+; Path=\/; Max-Age=\d+; HttpOnly; Secure; SameSite=Lax$/);
  const value = cookie.split(";")[0].split("=").slice(1).join("=");
  const access = await verifyToken(value, env.PAYWALL_SECRET, "access");
  assert.equal(access.sub, "c_python");
  assert.equal(access.exp, until);

  forwarded = [];
  const ok = await worker.fetch(
    new Request("https://blog.example.com/post/1", { headers: { Cookie: `theme=dark; __Host-paywall=${value}` } }),
    env,
  );
  assert.equal(ok.status, 200);
  const sent = forwarded[0];
  assert.equal(sent.headers.get("Cookie"), "theme=dark");
  assert.equal(sent.headers.get("X-Paywall-User"), "c_python");
  assert.equal(sent.headers.get("X-Paywall-Origin-Key"), "origin-secret");
});

test("handoff to another origin falls back to /", async () => {
  const token = pythonHandoff("https://evil.example.net/", now() + 60);
  const res = await worker.fetch(new Request(`https://blog.example.com/__paywall/auth?t=${token}`), env);
  assert.equal(res.headers.get("Location"), "/");
});

test("forged, expired or wrong-type tokens are rejected", async () => {
  const forged = await signToken({ v: 1, typ: "access", sub: "x", exp: now() + 60 }, "wrong-secret");
  const expired = await signToken({ v: 1, typ: "access", sub: "x", exp: now() - 1 }, env.PAYWALL_SECRET);
  const handoff = await signToken({ v: 1, typ: "handoff", sub: "x", exp: now() + 60, aexp: now() + 60 }, env.PAYWALL_SECRET);
  for (const value of [forged, expired, handoff, "garbage", "a.b"]) {
    const res = await worker.fetch(
      new Request("https://blog.example.com/", { headers: { Cookie: `__Host-paywall=${value}` } }),
      env,
    );
    assert.equal(res.status, 302, value);
  }
  const res = await worker.fetch(new Request("https://blog.example.com/__paywall/auth?t=garbage"), env);
  assert.equal(res.headers.get("Location"), "https://pay.example.com/");
});

test("spoofed paywall headers from the client are dropped", async () => {
  forwarded = [];
  await worker.fetch(
    new Request("https://blog.example.com/robots.txt", { headers: { "X-Paywall-User": "admin" } }),
    env,
  );
  assert.equal(forwarded[0].headers.get("X-Paywall-User"), null);
});

test("logout clears the cookie", async () => {
  const res = await worker.fetch(new Request("https://blog.example.com/__paywall/logout"), env);
  assert.match(res.headers.get("Set-Cookie"), /Max-Age=0/);
});
