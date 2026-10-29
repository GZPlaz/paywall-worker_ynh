The paywall is installed at https://__DOMAIN____PATH__ and its admin pages are at https://__DOMAIN____PATH__/admin.

To finish the setup, open **Applications → __APP__ → Configuration** in the YunoHost web admin:

1. **Paywall → Protected sites**: list the routes to protect, e.g. `blog.example.com/*`.
2. **Pricing**: enable and price the offers you want.
3. **Payments**: paste your Stripe secret key. In Stripe, create a webhook endpoint for `https://__DOMAIN____PATH__/webhook/stripe` with the events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `customer.subscription.updated`, `customer.subscription.deleted` and `invoice.paid`, then paste its signing secret.
4. **Cloudflare**: paste your API token and account ID, save, then press **Deploy**.

Create discount and free-access codes from the admin page (`/admin/codes`).
