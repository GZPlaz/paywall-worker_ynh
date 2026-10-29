Before installing, you need:

- **A dedicated (sub)domain for the paywall**, e.g. `pay.example.com`, added to YunoHost. It must not be one of the sites you protect.
- **The site(s) to protect on Cloudflare**, with the DNS record proxied (orange cloud). They can live on this YunoHost server or anywhere else.
- To take payments, a **Stripe account**. You can start in test mode.
- For automatic Worker deployment, a **Cloudflare API token** with these permissions: *Account → Workers Scripts → Edit*, *Zone → Workers Routes → Edit*, *Zone → Zone → Read*.

Everything except the domain can be set after installation: credentials in the app's config panel, sites and offers in its admin pages.

The paywall domain itself is best left **DNS-only** (grey cloud) in Cloudflare. If you proxy it, set up nginx's `real_ip` module for Cloudflare's IP ranges. Otherwise every visitor appears to come from a Cloudflare IP and they all share the anti-brute-force limits on codes and sign-in emails.
