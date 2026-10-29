The paywall is installed at https://__DOMAIN____PATH__ and its admin pages are at https://__DOMAIN____PATH__/admin.

To finish the setup:

1. In **Applications → __APP__ → Configuration** (YunoHost web admin):
   - **Payments**: paste a Stripe restricted API key. In Stripe, create a webhook endpoint for `https://__DOMAIN____PATH__/webhook/stripe` with the events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `charge.refunded` and `charge.dispute.created`, then paste its signing secret. Tax and invoice options are in the same tab; the full Stripe setup (sandbox, key permissions, Stripe Tax) is in the app's `doc/STRIPE.md`.
   - **Cloudflare**: paste your API token and account ID.
2. In the admin pages (`/admin`):
   - **Sites**: add one site per website to protect, with its routes (e.g. `blog.example.com/*`), page text and button color.
   - **Offers**: create what visitors can buy, and tick every site each offer should be sold on and open.
   - **Codes**: create discount or free-access codes, for one site or several.
   - **Cloudflare**: press **Deploy the Worker**. After that, saving a site redeploys it automatically.
