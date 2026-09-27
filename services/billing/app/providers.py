"""Payment providers, called over plain HTTPS (no SDKs).

Stripe   — international cards. Checkout Sessions API; webhook signature per
           https://docs.stripe.com/webhooks#verify-manually (HMAC-SHA256 of "t.payload").
Safepay  — Pakistani cards, JazzCash, EasyPaisa (PKR). Follows Safepay's own SDKs and
           WooCommerce plugin (github.com/getsafepay):
             * POST {api}/order/v1/init {client, amount, currency, environment} -> data.token (the "tracker")
               amount is in RUPEES (the plugin sends the order total as a float), not paisa.
             * the buyer is sent to {checkout}?beacon=<tracker>&order_id=..&redirect_url=..&cancel_url=..
             * after paying, Safepay sends the browser back to redirect_url with tracker + sig,
               sig = HMAC-SHA256(tracker, v1 secret)
             * webhook header X-SFPY-Signature = HMAC-SHA512(JSON of body["data"], webhook secret)
Nothing here touches the database; main.py decides what a verified result means.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ProviderError(Exception):
    """Message is safe to show the seller."""


@dataclass
class Checkout:
    ref: str   # Stripe session id / Safepay tracker
    url: str   # where to send the buyer


# ------------------------------------------------------------------ Stripe

STRIPE_API = "https://api.stripe.com/v1"
# Currencies Stripe charges in whole units (no x100). PKR and USD are not in this list.
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}


def stripe_minor_units(amount: float, currency: str) -> int:
    return int(round(amount)) if currency.lower() in ZERO_DECIMAL else int(round(amount * 100))


class Stripe:
    def __init__(self, secret_key: str, webhook_secret: str = "", base_url: str = STRIPE_API):
        self.key = secret_key
        self.webhook_secret = webhook_secret
        self.base = base_url.rstrip("/")

    async def _call(self, method: str, path: str, data: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.request(method, f"{self.base}{path}", data=data,
                                         headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError:
            raise ProviderError("Couldn't reach Stripe. Try again in a moment.")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 401:
            raise ProviderError("Stripe rejected the secret key (STRIPE_SECRET_KEY).")
        if r.status_code >= 400:
            msg = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
            raise ProviderError(f"Stripe: {msg}")
        return body

    async def create_checkout(self, *, payment_id: str, name: str, amount: float, currency: str,
                              success_url: str, cancel_url: str, email: str | None = None) -> Checkout:
        data = {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(stripe_minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": name,
            "client_reference_id": payment_id,
            "metadata[payment_id]": payment_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if email:
            data["customer_email"] = email
        s = await self._call("POST", "/checkout/sessions", data)
        if not s.get("id") or not s.get("url"):
            raise ProviderError("Stripe didn't return a checkout page.")
        return Checkout(ref=s["id"], url=s["url"])

    async def get_session(self, session_id: str) -> dict:
        return await self._call("GET", f"/checkout/sessions/{session_id}")

    def verify_webhook(self, payload: bytes, header: str, tolerance: int = 300, now: float | None = None) -> dict:
        """Returns the event, or raises ProviderError. Header: 't=..,v1=..[,v1=..]'."""
        if not self.webhook_secret:
            raise ProviderError("STRIPE_WEBHOOK_SECRET isn't set.")
        parts: dict[str, list[str]] = {}
        for item in (header or "").split(","):
            k, _, v = item.strip().partition("=")
            parts.setdefault(k, []).append(v)
        try:
            ts = int(parts["t"][0])
        except (KeyError, ValueError, IndexError):
            raise ProviderError("Bad Stripe-Signature header.")
        expected = hmac.new(self.webhook_secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in parts.get("v1", [])):
            raise ProviderError("Stripe signature doesn't match.")
        if abs((now or time.time()) - ts) > tolerance:
            raise ProviderError("Stripe webhook is too old.")
        try:
            return json.loads(payload)
        except ValueError:
            raise ProviderError("Stripe webhook body isn't JSON.")


# ------------------------------------------------------------------ Safepay

SAFEPAY_API = {"production": "https://api.getsafepay.com", "sandbox": "https://sandbox.api.getsafepay.com"}
SAFEPAY_CHECKOUT = {"production": "https://getsafepay.com/checkout/pay",
                    "sandbox": "https://sandbox.api.getsafepay.com/checkout/pay"}


class Safepay:
    def __init__(self, api_key: str, v1_secret: str, webhook_secret: str = "", environment: str = "sandbox",
                 api_url: str = "", checkout_url: str = ""):
        if environment not in SAFEPAY_API:
            raise ProviderError("SAFEPAY_ENVIRONMENT must be 'sandbox' or 'production'.")
        self.key, self.v1_secret, self.webhook_secret, self.env = api_key, v1_secret, webhook_secret, environment
        self.api = (api_url or SAFEPAY_API[environment]).rstrip("/")
        self.checkout = checkout_url or SAFEPAY_CHECKOUT[environment]

    async def create_checkout(self, *, payment_id: str, amount: float, currency: str,
                              redirect_url: str, cancel_url: str) -> Checkout:
        body = {"client": self.key, "amount": float(amount), "currency": currency.upper(), "environment": self.env}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(f"{self.api}/order/v1/init", json=body)
        except httpx.HTTPError:
            raise ProviderError("Couldn't reach Safepay. Try again in a moment.")
        try:
            data = r.json()
        except ValueError:
            data = {}
        token = ((data.get("data") or {}).get("token")) if isinstance(data, dict) else None
        if r.status_code >= 400 or not token:
            errors = ((data.get("status") or {}).get("errors") if isinstance(data, dict) else None) or []
            detail = errors[0] if errors else f"HTTP {r.status_code}"
            raise ProviderError(f"Safepay: {detail}")
        query = urlencode({"env": self.env, "beacon": token, "source": "custom", "order_id": payment_id,
                           "redirect_url": redirect_url, "cancel_url": cancel_url, "webhooks": "true"})
        return Checkout(ref=token, url=f"{self.checkout}?{query}")

    def verify_return(self, tracker: str, sig: str) -> bool:
        if not (self.v1_secret and tracker and sig):
            return False
        expected = hmac.new(self.v1_secret.encode(), tracker.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig.strip().lower())

    def verify_webhook(self, raw_body: bytes, signature: str) -> dict | None:
        """Returns body["data"] when the X-SFPY-Signature matches, else None.

        Safepay signs JSON.stringify(body.data). We don't know byte-for-byte how their
        server serialises it, so we try the raw substring and the two compact forms."""
        if not (self.webhook_secret and signature):
            return None
        try:
            body = json.loads(raw_body)
        except ValueError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return None
        candidates = [_js_stringify(data).encode(),
                      json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode(),
                      json.dumps(data, separators=(",", ":")).encode()]
        raw = _raw_member(raw_body, "data")
        if raw:
            candidates.insert(0, raw)
        sig = signature.strip().lower()
        for c in candidates:
            if hmac.compare_digest(hmac.new(self.webhook_secret.encode(), c, hashlib.sha512).hexdigest(), sig):
                return data
        return None


def _js_stringify(value) -> str:
    """Same text as JavaScript's JSON.stringify for parsed JSON: compact, raw Unicode,
    and whole-number floats written without ".0" (JS has one number type)."""
    def js(v):
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e21:
            return int(v)
        if isinstance(v, dict):
            return {k: js(x) for k, x in v.items()}
        if isinstance(v, list):
            return [js(x) for x in v]
        return v
    return json.dumps(js(value), separators=(",", ":"), ensure_ascii=False)


def _raw_member(raw: bytes, key: str) -> bytes | None:
    """The exact bytes of a top-level object member, e.g. {"data": {...}} -> b'{...}'."""
    text = raw.decode("utf-8", "replace")
    dec = json.JSONDecoder()
    i = text.find(f'"{key}"')
    while i != -1:
        j = text.find(":", i) + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        try:
            _, end = dec.raw_decode(text, j)
            return text[j:end].encode()
        except ValueError:
            i = text.find(f'"{key}"', i + 1)
    return None


SAFEPAY_PAID_STATES = {"PAID", "TRACKER_ENDED", "COMPLETED", "CAPTURED", "SUCCEEDED"}


def safepay_webhook_result(data: dict) -> tuple[str | None, bool]:
    """(tracker, paid) from a verified webhook's data. Tolerant of a few field names."""
    tracker = data.get("tracker") or data.get("token") or (data.get("notification") or {}).get("tracker")
    if isinstance(tracker, dict):
        tracker = tracker.get("token")
    state = str(data.get("state") or (data.get("notification") or {}).get("state") or "").upper()
    return (tracker if isinstance(tracker, str) else None), state in SAFEPAY_PAID_STATES
