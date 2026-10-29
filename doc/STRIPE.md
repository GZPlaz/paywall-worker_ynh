## Stripe integration plan: Payments, Invoicing, Tax

For mindaccel.org (selling access to learning content), built on Stripe's best-practice guidance for API version `2026-10-28.endive`.

### How the paywall uses Stripe

| Need | Stripe feature | In the paywall |
| --- | --- | --- |
| Take payments | Hosted **Checkout Sessions**, with dynamic payment methods (cards, wallets and local methods, chosen in the Dashboard) | Every offer: one-time, pass, pay-what-you-want, subscription |
| Recurring billing | Billing subscriptions created by Checkout (`mode: subscription`) and the **Customer Portal** | Subscription offers; "Manage subscription" on the account page |
| Invoices | Subscriptions always produce invoices. One-time payments get one with `invoice_creation` | Config panel: *Create an invoice for one-time payments* (on by default) |
| Sales tax / VAT | **Stripe Tax** with `automatic_tax`, a product tax code and a tax behavior | Config panel: *Tax and invoices* (off until you add registrations) |
| B2B VAT numbers | `tax_id_collection` | Config panel option |
| Discounts | Coupons attached as `discounts` | Discount codes from `/admin/codes` |
| Access | Webhooks, verified with the signing secret | `/webhook/stripe` grants, renews, ends and revokes access |

Each offer becomes one Stripe **Product**, created the first time someone buys it. This keeps invoices and tax reports readable and carries the product tax code. Prices are sent with each Checkout Session, so changing a price in `/admin/offers` needs no Stripe change.

### Review of the existing integration

Already in line with the guidance:

- Checkout Sessions, never the Charges API. `payment_method_types` is never passed.
- Fulfillment runs from verified webhooks, on both `checkout.session.completed` and `checkout.session.async_payment_succeeded`, and only once `payment_status` is paid. The success page uses the same idempotent code, so a payment is never counted twice.
- Subscription lifecycle events, plus a fallback that asks Stripe directly when a renewal webhook is late.
- Customer Portal for self-service cancellation and card updates.
- The API key lives in the YunoHost settings (root-only) and `config.json` (readable only by the app user), never in code.

Improved in 1.2.0:

1. API version upgraded from `2024-06-20` to `2026-10-28.endive`. Webhook events arrive in your endpoint's version, the latest one for new endpoints, so requests now match. The paywall already handled the Basil changes: subscription periods moved to subscription items, and invoices point to their subscription through `parent`.
2. Checkout Sessions carry an `integration_identifier` (`paywall-worker-` plus 8 letters), so you can filter them in the Dashboard.
3. Stripe Tax, tax-inclusive or exclusive prices, product tax code and tax ID collection, all optional.
4. Invoices for one-time payments.
5. A full refund or a dispute of a one-time purchase now removes that access. Partial refunds keep it. For subscriptions, cancel the subscription in Stripe and access ends through `customer.subscription.deleted`.
6. A failed delayed payment (`checkout.session.async_payment_failed`) is logged and grants nothing.

### Setup, step by step

1. **Test in a sandbox first.** In the Stripe Dashboard, create a [sandbox](https://docs.stripe.com/sandboxes.md). It is a separate test environment with its own keys, settings and tax registrations.
2. **Create a restricted API key** (`rk_...`), not a secret key. Give it these permissions:
   - *Write*: Checkout Sessions, Products, Coupons, Customer portal
   - *Read*: Subscriptions

   Paste it in **Applications → Paywall Worker → Configuration → Payments**. Never commit it anywhere or paste it into chats.
3. **Create the webhook endpoint** for `https://<paywall domain>/webhook/stripe` with these events:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.async_payment_failed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `charge.refunded`
   - `charge.dispute.created`

   Paste its signing secret (`whsec_...`) in the same config panel tab. Use **Send test event** in Stripe: a `200` response means it works.
4. **Customer portal**: in the Dashboard (Settings → Billing → Customer portal), save the portal configuration once. The "Manage subscription" button needs it.
5. **Invoices**: they're on by default for one-time payments. Stripe charges a small per-invoice fee for these; turn the option off if you don't need them. In Dashboard → Settings → Billing → Invoices, add your business details and turn on emailed receipts.
6. **Stripe Tax** (only after you know where you must collect):
   1. Dashboard → Tax → Settings: set your **head office address**. Without it, tax settings stay `pending` and nothing is calculated.
   2. Choose your **preset product tax code** there. Alternatively, set one in the config panel. Pick it from Stripe's [tax code list](https://docs.stripe.com/tax/tax-codes) ([Tax Codes API](https://docs.stripe.com/api/tax_codes.md)). For digital content, [tax codes for digital products](https://docs.stripe.com/tax/digital-products.md) lists the specific options. Avoid the generic electronically-supplied-services code for US sales. Confirm the choice with your tax advisor.
   3. Dashboard → Tax → Locations: **add a registration** for each place you are already registered with the tax authority. Adding it in Stripe does not register you with the authority.
   4. Only then, turn on *Collect sales tax / VAT with Stripe Tax* in the config panel. Also choose whether your prices include tax.
   5. Check one test purchase in the sandbox. On the Checkout Session, the line item taxes must not show `taxability_reason: not_collecting`. That value means a missing registration, or a Nontaxable tax code.
   6. Watch Dashboard → Tax → Locations → *Needs attention* for places where you may need to register as sales grow. Discuss them with your tax advisor.
7. **Go live**: repeat steps 2–6 in live mode, since keys, webhooks, tax settings and registrations are separate. Then go through Stripe's [go-live checklist](https://docs.stripe.com/get-started/checklist/go-live.md).

### Notes

- With Stripe Tax on, Stripe collects nothing, and shows no error, in places where you have no active registration. Leave it off rather than assume tax is being collected.
- Turning on tax for existing subscriptions is not automatic. New checkouts use the new setting. Existing subscriptions keep their previous tax setup until changed in Stripe.
- If a key is ever exposed, roll it immediately in the Dashboard (Developers → API keys) and paste the new one in the config panel.
