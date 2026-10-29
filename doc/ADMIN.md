## Overview

```
visitor ──► news.example.org/premium/story ──► Cloudflare Worker ──(cookie for "news")──► your site
                                                     │
                                                     └─(no cookie)──► https://__DOMAIN____PATH__/s/news (this app)
                                                                         │ pay with Stripe / enter a code
                                                                         ▼
           news.example.org/premium/__paywall/auth?t=<signed token> ──► cookie set, back to the story
```

One paywall serves several **sites**. The Worker works out which site a request belongs to: the most specific route wins. Visitors are sent to that site's own paywall page at `/s/<site>`. Access is per site. Each site has its own cookie, and the paywall signs a token for one site only. The Worker checks the cookie on every request without calling back to this server.

The cookie lasts at most the site's *re-check interval* (24 h by default). When it expires, the visitor is silently sent through the paywall again, which renews it if their access is still valid. This is how cancellations and revocations reach the site.

## Where settings live

| Where | What |
| --- | --- |
| **/admin → Sites** | Per site: routes, public paths, page heading and text, button color, support email, code field, re-check interval, on/off |
| **/admin → Offers** | What visitors can buy, and which sites each offer is sold on and opens |
| **/admin → Codes** | Discount and free-access codes, and the sites they work on |
| **/admin → Customers** | Search, grant access to one or more sites, revoke |
| **Config panel** (Applications → __APP__ → Configuration) | Shared by all sites: currency, payment provider and Stripe keys, email sign-in, Cloudflare credentials and Worker deployment |

## Linking one thing to several sites

- **Offers.** Tick every site the offer should be sold on. Buying it opens all of them, so an offer on two sites is a bundle. Each site's page shows "Also opens …" for such offers. When you add a site to an existing offer, people who already bought it get the new site too, unless you untick that option.
- **Codes.** Tick the sites a code works on, or none for every site. A free-access code opens all of its sites. A discount code can also be limited to some offers. You can change a code's sites later from the codes list.
- **Grants.** In the grant form on the Customers page or a customer's page, tick the sites to open.
- **Sites.** The site form also lists every offer, so you can attach existing offers while creating a site. **Duplicate** copies a site's settings and offers to start a new one.

## Sites

- Each route belongs to exactly one site, and no route may cover the paywall's own domain.
- Path-scoped routes such as `example.com/premium/*` work. The Worker's callback then lives under that path, at `/premium/__paywall/auth`.
- Unticking *Protect this site* removes its routes from the Worker the next time it is deployed. With *Update the Worker automatically* on (config panel, Cloudflare tab), that happens as soon as you save the site.

## Codes

- **Percentage / fixed discount**: applied at checkout. For subscriptions, choose whether it applies to the first payment, a number of months, or every payment. A 100 % code on a one-off offer grants it without payment.
- **Free access**: skips payment and opens the code's sites for N days, or with no end date.

Each code can also be limited to a number of uses and an expiry date. You can generate up to 500 random codes at once and export them to CSV.

## Command line

Everything is also available from the command line (run as root):

```bash
__INSTALL_DIR__/paywall-cli sites
__INSTALL_DIR__/paywall-cli offers
__INSTALL_DIR__/paywall-cli codes create --kind free --days 30 --count 10 --sites blog,news --note "Giveaway"
__INSTALL_DIR__/paywall-cli codes create --code SPRING25 --kind percent --value 25 --sites blog --expires 2026-06-01
__INSTALL_DIR__/paywall-cli codes list
__INSTALL_DIR__/paywall-cli grant friend@example.com --sites all --days 365
__INSTALL_DIR__/paywall-cli revoke someone@example.com
__INSTALL_DIR__/paywall-cli customers
__INSTALL_DIR__/paywall-cli deploy-worker
__INSTALL_DIR__/paywall-cli wrangler-toml
```

## Upgrading from 1.0

Version 1.0 had a single set of page texts, routes and prices in the config panel. The first start of 1.1 turns them into a site called `main` with matching offers:

- existing purchases and grants are linked to `main`
- codes limited to an old offer point to the new one

Check the result in **/admin → Sites**, then add your other sites.

## Protect the origin

The Worker only sees traffic that goes through Cloudflare. If your site's origin server is reachable directly, someone who knows its IP can bypass the paywall. Do one of the following:

- Only accept connections from [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) on the origin, or use Cloudflare Tunnel or Authenticated Origin Pulls.
- Or check the `X-Paywall-Origin-Key` header that the Worker adds to every request. The value and an nginx snippet are on the **/admin/cloudflare** page. For an app hosted on YunoHost, put the snippet in a separate file included from the app's nginx config, so that app upgrades do not overwrite it.

The Worker also passes `X-Paywall-User` (the customer id) and `X-Paywall-Site` to the origin, and removes any copy the visitor sent.

## Deploying the Worker manually

Download `paywall-worker.js` and `wrangler.toml` from **/admin/cloudflare**. Set the secrets with `wrangler secret put PAYWALL_SECRET` and `wrangler secret put ORIGIN_SECRET` (values shown on the same page), then run `wrangler deploy`. Download a fresh `wrangler.toml` after changing sites: the `SITES` variable lists them.

## Uninstalling

If *Remove the Worker and its routes when this app is uninstalled* is on (the default), removing the app deletes the Worker and its routes, which makes the protected sites public again. Turn it off to keep the sites locked while the paywall is gone. Visitors will then be redirected to a paywall that no longer exists.
