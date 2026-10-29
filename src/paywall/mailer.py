"""Outgoing email (sign-in links) through the local YunoHost mail server."""

import smtplib
from email.message import EmailMessage


def send_login_link(cfg, to, link, site_title="the paywall"):
    msg = EmailMessage()
    msg["Subject"] = f"Your sign-in link for {site_title}"
    msg["From"] = cfg["mail_from"]
    msg["To"] = to
    msg.set_content(
        f"Hello,\n\nUse this link to restore your access to {site_title}:\n\n{link}\n\n"
        "The link is valid for 30 minutes and can be used once.\n"
        "If you did not request it, you can ignore this email.\n"
    )
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=20) as smtp:
        smtp.send_message(msg)
