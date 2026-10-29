Paywall Worker puts a payment wall in front of any website proxied by Cloudflare: a blog, a newsletter archive, a course, a self-hosted app... Visitors must pay (or enter a valid code) before the site loads.

### How it works

- A small **Cloudflare Worker** runs in front of the protected site(s). It checks a signed access cookie at the edge and sends everyone else to the paywall. No request reaches your site without access.
- The **paywall** (this app, hosted on your YunoHost) shows your offers, takes payments with **Stripe Checkout** and sends paying visitors back to the page they wanted.
- The Worker is **deployed automatically** through the Cloudflare API, or you can download it and deploy it yourself with `wrangler`.

### Features

- **One-time payment** (lifetime access, or a fixed number of days)
- **Subscriptions** (daily, weekly, monthly or yearly, with an optional free trial). Customers manage or cancel them from the Stripe customer portal
- **Time passes** (e.g. a 24-hour day pass)
- **Pay what you want**, with a minimum and suggested price
- **Codes**: percentage or fixed-amount discounts (first payment, N months or forever for subscriptions), and **free access codes** that skip payment entirely. Set limits on uses, expiry dates and which offers a code applies to. Bulk-generate codes and export them to CSV
- **Codes only** mode: no online payment, people get in with codes or manual grants
- Customers can restore access on a new device with an emailed sign-in link
- Admin pages (YunoHost SSO): dashboard, codes, customers, manual grants and revocations, Cloudflare deployment
- All settings in the YunoHost config panel: prices, currency, page texts, protected routes, Stripe and Cloudflare credentials
