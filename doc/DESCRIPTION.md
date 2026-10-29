Paywall Worker puts a payment wall in front of any website proxied by Cloudflare: a blog, a newsletter archive, a course, a self-hosted app... Visitors must pay (or enter a valid code) before the site loads.

### How it works

One paywall serves as many sites as you want. Each site has its own paywall page (title, text, button color, support email), its own routes and public paths, and its own access: paying for one site does not open another, unless the offer is linked to both.

- A small **Cloudflare Worker** runs in front of the protected site(s). It checks a signed access cookie at the edge and sends everyone else to the paywall. No request reaches your site without access.
- The **paywall** (this app, hosted on your YunoHost) shows your offers, takes payments with **Stripe Checkout** and sends paying visitors back to the page they wanted.
- The Worker is **deployed automatically** through the Cloudflare API, or you can download it and deploy it yourself with `wrangler`.

### Features

- **Several sites**, each with its own settings, managed from the admin pages
- **Link anything to several sites at once**: an offer linked to two sites is a bundle that opens both; a code or a manual grant can cover any set of sites

- **One-time payment** (lifetime access, or a fixed number of days)
- **Subscriptions** (daily, weekly, monthly or yearly, with an optional free trial). Customers manage or cancel them from the Stripe customer portal
- **Time passes** (e.g. a 24-hour day pass)
- **Pay what you want**, with a minimum and suggested price
- **Codes**: percentage or fixed-amount discounts (first payment, N months or forever for subscriptions), and **free access codes** that skip payment entirely. Set limits on uses, expiry dates and which offers a code applies to. Bulk-generate codes and export them to CSV
- **Codes only** mode: no online payment, people get in with codes or manual grants
- Customers can restore access on a new device with an emailed sign-in link
- Admin pages (YunoHost SSO): dashboard, codes, customers, manual grants and revocations, Cloudflare deployment
- Sites, offers and codes are managed from the admin pages; currency, Stripe and Cloudflare credentials from the YunoHost config panel
