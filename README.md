# Paywall Worker for YunoHost

[![Install Paywall Worker with YunoHost](https://install-app.yunohost.org/install-with-yunohost.svg)](https://install-app.yunohost.org/?app=paywall_worker)

Put a paywall in front of any Cloudflare-proxied site: one-time payments, subscriptions, time passes, pay-what-you-want, discount codes and free access codes.

**Shipped version:** 1.0.0~ynh1

## Overview

- A **Cloudflare Worker** sits in front of the protected site(s). It checks a signed access cookie at the edge and redirects everyone else to the paywall.
- The **paywall** runs on your YunoHost. It lists your offers, takes payments with **Stripe Checkout** and redirects paying visitors back to the page they wanted.
- Everything is configured from the YunoHost **config panel**. Codes, customers and Worker deployment are managed from the app's **/admin** page (protected by the YunoHost SSO).

### Pricing options (mix and match)

| Option | What the visitor gets |
| --- | --- |
| One-time payment | Lifetime access, or N days |
| Subscription | Access while subscribed (daily/weekly/monthly/yearly, optional free trial) |
| Time pass | N hours of access (e.g. a day pass) |
| Pay what you want | Visitor picks a price above your minimum |
| Codes only | No online payment, access through codes or manual grants |

### Codes

- Percentage or fixed-amount **discounts**. For subscriptions they apply to the first payment, N months, or every payment
- **Free access codes** that bypass payment for N days or forever
- Per-code limits: number of uses, expiry date, which offers it applies to
- Bulk generation and CSV export

## Repository layout

| Path | Contents |
| --- | --- |
| `manifest.toml`, `config_panel.toml`, `scripts/`, `conf/` | YunoHost packaging |
| `src/paywall/` | Paywall web app (Python / Flask, SQLite) |
| `src/worker/paywall-worker.js` | Cloudflare Worker |
| `tests/` | Python tests (`pytest tests`) and Worker tests (`node --test tests/worker.test.mjs`) |
| `doc/` | Description, pre/post-install notes and admin guide |

## Install

```bash
sudo yunohost app install https://github.com/GZPlaz/paywall-worker_ynh
```

Then follow [doc/POST_INSTALL.md](doc/POST_INSTALL.md) and read [doc/ADMIN.md](doc/ADMIN.md).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r src/requirements.txt pytest
.venv/bin/pytest -q tests
node --test tests/worker.test.mjs
```
