"""Signed tokens shared with the Cloudflare Worker.

Format: ``base64url(json payload) + "." + base64url(HMAC-SHA256(secret, first part))``.
The Worker (src/worker/paywall-worker.js) implements the exact same scheme
with WebCrypto, so tokens minted here can be verified at the edge without
calling back to this server.

Token types (``typ``):
  * ``handoff`` - short lived, minted by the paywall after a successful
    payment/login and passed in the URL to ``<site><prefix>/auth``.
  * ``access``  - minted by the Worker itself and stored as a cookie on the
    protected site.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

VERSION = 1
HANDOFF_TTL = 120


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _mac(secret: str, body: str) -> bytes:
    return hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()


def sign(payload: dict, secret: str) -> str:
    if not secret:
        raise ValueError("signing secret is empty")
    body = b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{b64e(_mac(secret, body))}"


def verify(token: str, secret: str, typ: str, now=None):
    """Return the payload if ``token`` is valid, unexpired and of type ``typ``."""
    if not token or not secret or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    try:
        if not hmac.compare_digest(b64d(sig), _mac(secret, body)):
            return None
        payload = json.loads(b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != VERSION or payload.get("typ") != typ:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < (now if now is not None else time.time()):
        return None
    return payload


def handoff(secret, customer_id, access_until, return_url, now=None):
    now = int(now if now is not None else time.time())
    return sign(
        {
            "v": VERSION,
            "typ": "handoff",
            "sub": customer_id,
            "aexp": int(access_until),
            "exp": now + HANDOFF_TTL,
            "r": return_url,
            "n": secrets.token_urlsafe(6),
        },
        secret,
    )


def random_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
