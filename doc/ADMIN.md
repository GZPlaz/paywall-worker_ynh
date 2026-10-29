## Overview

```
visitor ──► blog.example.com ──► Cloudflare Worker ──(valid cookie)──► your site
                                       │
                                       └─(no cookie)──► https://__DOMAIN____PATH__ (this app)
                                                            │ pay with Stripe / enter a code
                                                            ▼
                         blog.example.com/__paywall/auth?t=<signed token> ──► cookie set, back to the page
```

The paywall and the Worker share a secret. The paywall signs a short-lived token. The Worker turns it into an access cookie on the protected site and checks that cookie on every request without calling back to this server.

The access cookie lasts at most *Re-check access every (hours)* (24 h by default). When it expires, the visitor is silently sent through the paywall again, which renews it if their access is still valid. This is how subscription cancellations and revocations reach the protected site.

## Settings

All settings are in **Applications → __APP__ → Configuration**. Saving restarts the app. If *Update the Worker automatically* is on, it also re-deploys the Worker.

| Tab | What you set |
| --- | --- |
| Paywall | Page texts and color, protected routes, public paths, cookie re-check interval |
| Pricing | Currency; one-time, subscription, time pass and pay-what-you-want offers |
| Payments | Stripe keys, or *No online payment* for codes-only mode; email sign-in links |
| Cloudflare | API token, account ID, Worker name, Deploy button |

## Codes

Create codes from **/admin/codes**:

- **Percentage / fixed discount**: applied at checkout. For subscriptions, choose whether it applies to the first payment, a number of months, or every payment. A 100 % code on a one-off offer grants it without payment.
- **Free access**: skips payment and grants access for N days, or forever.

Each code can be limited to some offers, a number of uses, and an expiry date. You can generate up to 500 random codes at once and export them to CSV.

## Command line

Everything is also available from the command line (run as root):

```bash
__INSTALL_DIR__/paywall-cli codes create --kind free --days 30 --count 10 --note "Giveaway"
__INSTALL_DIR__/paywall-cli codes create --code SPRING25 --kind percent --value 25 --expires 2026-06-01
__INSTALL_DIR__/paywall-cli codes list
__INSTALL_DIR__/paywall-cli grant friend@example.com --days 365
__INSTALL_DIR__/paywall-cli revoke someone@example.com
__INSTALL_DIR__/paywall-cli customers
__INSTALL_DIR__/paywall-cli deploy-worker
__INSTALL_DIR__/paywall-cli wrangler-toml
```

## Protect the origin

The Worker only sees traffic that goes through Cloudflare. If your site's origin server is reachable directly, someone who knows its IP can bypass the paywall. Do one of the following:

- Only accept connections from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) on the origin, or use Cloudflare Tunnel or Authenticated Origin Pulls.
- Or check the `X-Paywall-Origin-Key` header that the Worker adds to every request. The value and an nginx snippet are on the **/admin/cloudflare** page. For an app hosted on YunoHost, put the snippet in a separate file included from the app's nginx config, so that app upgrades do not overwrite it.

The Worker also passes `X-Paywall-User` (the customer id) to the origin, and removes any copy the visitor sent.

## Deploying the Worker manually

Download `paywall-worker.js` and `wrangler.toml` from **/admin/cloudflare**. Set the secrets with `wrangler secret put PAYWALL_SECRET` and `wrangler secret put ORIGIN_SECRET` (values shown on the same page), then run `wrangler deploy`.

## Uninstalling

If *Remove the Worker and its routes when this app is uninstalled* is on (the default), removing the app deletes the Worker and its routes, which makes the protected sites public again. Turn it off to keep the sites locked while the paywall is gone. Visitors will then be redirected to a paywall that no longer exists.
