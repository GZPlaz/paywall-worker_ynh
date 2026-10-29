The paywall is installed at https://__DOMAIN____PATH__ and its admin pages are at https://__DOMAIN____PATH__/admin.

To finish the setup:

1. In **Applications → __APP__ → Configuration** (YunoHost web admin):
   - **Payments**: paste your Stripe secret key. In Stripe, create a webhook endpoint for `https://__DOMAIN____PATH__/webhook/stripe` with the events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `customer.subscription.updated`, `customer.subscription.deleted` and `invoice.paid`, then paste its signing secret.
   - **Cloudflare**: paste your API token and account ID.
2. In the admin pages (`/admin`):
   - **Sites**: add one site per website to protect, with its routes (e.g. `blog.example.com/*`), page text and button color.
   - **Offers**: create what visitors can buy, and tick every site each offer should be sold on and open.
   - **Codes**: create discount or free-access codes, for one site or several.
   - **Cloudflare**: press **Deploy the Worker**. After that, saving a site redeploys it automatically.
