"""Billing service: credit balances, reservations, ledger, and credit-pack payments.

Saga 4.1: catalog reserves synchronously; ai's listing.generated spends the
reservation, listing.failed releases it, and a sweeper releases any reservation
nobody settled in time — so a credit is never lost.

Payments (contracts/README.md §5): the seller picks a pack, we create a `payments` row and
a hosted checkout (Stripe or Safepay), and credits are added exactly once when the provider
confirms — by webhook, by Safepay's signed redirect, or by asking Stripe on return.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import text
from sqlalchemy.orm import Session

from lagent_common.bus import EventBus
from lagent_common.db import make_db, mark_processed
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service

from app.providers import STRIPE_API, ProviderError, Safepay, Stripe, safepay_webhook_result, stripe_minor_units

log = logging.getLogger("lagent.billing")


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    SIGNUP_FREE_CREDITS: int = 10
    RESERVATION_MINUTES: int = 30
    SWEEP_EVERY_SECONDS: int = 300

    # Credit packs. Prices are placeholders — set your own in .env (JSON, one line).
    # "PKR" is what Safepay charges; the Stripe price is looked up by STRIPE_CURRENCY.
    CREDIT_PACKS: str = json.dumps([
        {"id": "starter", "name": "Starter", "credits": 50, "prices": {"PKR": 999, "USD": 4}},
        {"id": "growth", "name": "Growth", "credits": 200, "prices": {"PKR": 2999, "USD": 12}},
        {"id": "pro", "name": "Pro", "credits": 600, "prices": {"PKR": 7499, "USD": 29}},
    ])
    APP_URL: str = "http://localhost:3000"      # the web app, where buyers come back to
    PUBLIC_BASE_URL: str = ""                   # the gateway's public address (empty = local)
    GATEWAY_PORT: int = 8000

    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_CURRENCY: str = "usd"
    STRIPE_API_URL: str = STRIPE_API

    SAFEPAY_ENVIRONMENT: str = "sandbox"
    SAFEPAY_API_KEY: str = ""
    SAFEPAY_V1_SECRET: str = ""
    SAFEPAY_WEBHOOK_SECRET: str = ""
    SAFEPAY_API_URL: str = ""
    SAFEPAY_CHECKOUT_URL: str = ""

    @property
    def api_base(self) -> str:
        return (self.PUBLIC_BASE_URL or f"http://localhost:{self.GATEWAY_PORT}").rstrip("/")


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
bus = EventBus(settings.REDIS_URL, "billing")


def _packs() -> list[dict]:
    try:
        packs = json.loads(settings.CREDIT_PACKS)
        return [p for p in packs if p.get("id") and int(p.get("credits", 0)) > 0 and p.get("prices")]
    except (ValueError, TypeError, AttributeError):
        log.error("CREDIT_PACKS isn't valid JSON — no packs on sale")
        return []


def _stripe() -> Stripe | None:
    if not settings.STRIPE_SECRET_KEY:
        return None
    return Stripe(settings.STRIPE_SECRET_KEY, settings.STRIPE_WEBHOOK_SECRET, settings.STRIPE_API_URL)


def _safepay() -> Safepay | None:
    if not (settings.SAFEPAY_API_KEY and settings.SAFEPAY_V1_SECRET):
        return None
    return Safepay(settings.SAFEPAY_API_KEY, settings.SAFEPAY_V1_SECRET, settings.SAFEPAY_WEBHOOK_SECRET,
                   settings.SAFEPAY_ENVIRONMENT, settings.SAFEPAY_API_URL, settings.SAFEPAY_CHECKOUT_URL)


def _ledger(db: Session, user_id, delta: int, reason: str, ref_id=None) -> None:
    db.execute(text("INSERT INTO credit_transactions (user_id, delta, reason, ref_id) VALUES (:u, :d, :r, :ref)"),
               {"u": str(user_id), "d": delta, "r": reason, "ref": str(ref_id) if ref_id else None})


def _settle(db: Session, reservation_id: str, outcome: str) -> None:
    """outcome 'spent' keeps the credit gone; 'released' gives it back. Only settles once."""
    row = db.execute(text(
        "UPDATE credit_reservations SET status = :s, settled_at = now() AT TIME ZONE 'utc' "
        "WHERE id = :id AND status = 'reserved' RETURNING user_id, amount"
    ), {"s": outcome, "id": reservation_id}).first()
    if row and outcome == "released":
        db.execute(text("UPDATE credit_accounts SET balance = balance + :a, updated_at = now() AT TIME ZONE 'utc' "
                        "WHERE user_id = :u"), {"a": row.amount, "u": row.user_id})
        _ledger(db, row.user_id, row.amount, "released", reservation_id)


# ---------------- events ----------------

async def on_user_registered(event: dict) -> None:
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        user_id = event["data"]["user_id"]
        created = db.execute(text("INSERT INTO credit_accounts (user_id, balance) VALUES (:u, :b) "
                                  "ON CONFLICT DO NOTHING"), {"u": user_id, "b": settings.SIGNUP_FREE_CREDITS})
        if created.rowcount:
            _ledger(db, user_id, settings.SIGNUP_FREE_CREDITS, "signup_bonus")
        db.commit()


async def on_listing_generated(event: dict) -> None:
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        _settle(db, event["data"]["reservation_id"], "spent")
        db.commit()


async def on_listing_failed(event: dict) -> None:
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        _settle(db, event["data"]["reservation_id"], "released")
        db.commit()


bus.subscribe("user.registered", on_user_registered)
bus.subscribe("listing.generated", on_listing_generated)
bus.subscribe("listing.failed", on_listing_failed)


async def sweep_expired(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                ids = [r.id for r in db.execute(text(
                    "SELECT id FROM credit_reservations WHERE status = 'reserved' "
                    "AND expires_at < now() AT TIME ZONE 'utc' LIMIT 500"))]
                for rid in ids:
                    _settle(db, str(rid), "released")
                db.commit()
                if ids:
                    log.warning("released %d expired reservation(s)", len(ids))
        except Exception:  # noqa: BLE001
            log.exception("reservation sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.SWEEP_EVERY_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------- HTTP ----------------

class ReserveIn(BaseModel):
    user_id: uuid.UUID
    amount: int = Field(gt=0)
    reason: str
    ref_id: uuid.UUID


router = APIRouter(dependencies=[Depends(require_internal)])  # the gateway adds the token to public routes too


@router.get("/credits")
async def my_credits(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    # A buyer may never come back from the payment page (closed tab, no redirect): each time the
    # balance is loaded, ask the provider about this user's recent unconfirmed payments.
    recent = db.execute(text(
        "SELECT * FROM payments WHERE user_id = :u AND status = 'pending' AND provider_ref IS NOT NULL "
        "AND created_at > (now() AT TIME ZONE 'utc') - interval '2 hours' ORDER BY created_at DESC LIMIT 3"),
        {"u": str(user_id)}).fetchall()
    for row in recent:
        await _confirm_pending(db, row)
    balance = db.execute(text("SELECT balance FROM credit_accounts WHERE user_id = :u"), {"u": str(user_id)}).scalar()
    history = db.execute(text("SELECT delta, reason, created_at FROM credit_transactions WHERE user_id = :u "
                              "ORDER BY created_at DESC LIMIT 50"), {"u": str(user_id)})
    return {"balance": balance or 0,
            "history": [{"delta": h.delta, "reason": h.reason, "at": h.created_at.isoformat()} for h in history]}


@router.post("/internal/reservations", status_code=201)
def reserve(data: ReserveIn, db: Session = Depends(get_db)):
    existing = db.execute(text("SELECT id FROM credit_reservations WHERE ref_id = :r"), {"r": str(data.ref_id)}).scalar()
    if existing:
        return {"reservation_id": str(existing)}  # same job asked twice -> same reservation
    # Atomic: only succeeds if the balance covers it (and the CHECK keeps it >= 0 regardless).
    taken = db.execute(text("UPDATE credit_accounts SET balance = balance - :a, updated_at = now() AT TIME ZONE 'utc' "
                            "WHERE user_id = :u AND balance >= :a"), {"a": data.amount, "u": str(data.user_id)})
    if taken.rowcount == 0:
        db.rollback()
        raise HTTPException(402, "Not enough credits.")
    rid = db.execute(text(
        "INSERT INTO credit_reservations (user_id, amount, reason, ref_id, expires_at) "
        "VALUES (:u, :a, :reason, :ref, :exp) RETURNING id"
    ), {"u": str(data.user_id), "a": data.amount, "reason": data.reason, "ref": str(data.ref_id),
        "exp": datetime.utcnow() + timedelta(minutes=settings.RESERVATION_MINUTES)}).scalar()
    _ledger(db, data.user_id, -data.amount, "reserved", rid)
    db.commit()
    return {"reservation_id": str(rid)}


# ---------------- payments ----------------

class CheckoutIn(BaseModel):
    pack_id: str
    provider: str = Field(pattern="^(stripe|safepay)$")


def _payment_out(row) -> dict:
    return {"id": str(row.id), "status": row.status, "provider": row.provider, "pack_id": row.pack_id,
            "credits": row.credits, "amount": float(row.amount), "currency": row.currency, "error": row.error}


async def _credit_payment(db: Session, payment_id: str) -> bool:
    """Marks a pending payment paid and adds its credits — once, however many times it's confirmed."""
    row = db.execute(text(
        "UPDATE payments SET status = 'paid', paid_at = now() AT TIME ZONE 'utc', error = NULL "
        "WHERE id = :id AND status <> 'paid' RETURNING user_id, credits"), {"id": payment_id}).first()
    if not row:
        db.rollback()
        return False
    balance = db.execute(text(
        "INSERT INTO credit_accounts (user_id, balance) VALUES (:u, :c) "
        "ON CONFLICT (user_id) DO UPDATE SET balance = credit_accounts.balance + :c, "
        "updated_at = now() AT TIME ZONE 'utc' RETURNING balance"), {"u": str(row.user_id), "c": row.credits}).scalar()
    _ledger(db, row.user_id, row.credits, "purchase", payment_id)
    db.commit()
    log.info("payment %s paid: +%d credits", payment_id, row.credits)
    await bus.publish("credits.purchased", {"user_id": str(row.user_id), "amount": row.credits,
                                            "balance": balance, "payment_id": payment_id})
    return True


def _fail_payment(db: Session, payment_id: str, reason: str) -> None:
    db.execute(text("UPDATE payments SET status = 'failed', error = :e WHERE id = :id AND status = 'pending'"),
               {"id": payment_id, "e": reason[:300]})
    db.commit()


@router.get("/packs")
def list_packs():
    stripe_cur = settings.STRIPE_CURRENCY.upper()
    return {
        "packs": [{"id": p["id"], "name": p.get("name") or p["id"].title(), "credits": int(p["credits"]),
                   "prices": {k.upper(): float(v) for k, v in p["prices"].items()}} for p in _packs()],
        "providers": {"safepay": _safepay() is not None, "stripe": _stripe() is not None},
        "stripe_currency": stripe_cur,
    }


@router.post("/checkout", status_code=201)
async def start_checkout(data: CheckoutIn, user_id: uuid.UUID = Depends(current_user_id),
                         db: Session = Depends(get_db)):
    pack = next((p for p in _packs() if p["id"] == data.pack_id), None)
    if pack is None:
        raise HTTPException(404, "That pack isn't on sale.")
    prices = {k.upper(): float(v) for k, v in pack["prices"].items()}
    if data.provider == "stripe":
        provider, currency = _stripe(), settings.STRIPE_CURRENCY.upper()
        label = "Card payments"
    else:
        provider, currency = _safepay(), "PKR"
        label = "Safepay (JazzCash / EasyPaisa)"
    if provider is None:
        raise HTTPException(503, f"{label} aren't set up on this server yet.")
    if currency not in prices:
        raise HTTPException(422, f"This pack has no {currency} price.")
    amount = prices[currency]

    payment_id = db.execute(text(
        "INSERT INTO payments (user_id, pack_id, credits, provider, amount, currency) "
        "VALUES (:u, :p, :c, :prov, :a, :cur) RETURNING id"),
        {"u": str(user_id), "p": pack["id"], "c": int(pack["credits"]), "prov": data.provider,
         "a": amount, "cur": currency}).scalar()
    db.commit()
    pid = str(payment_id)
    back = f"{settings.APP_URL.rstrip('/')}/credits?payment={pid}"
    name = f"Listing Agent — {pack.get('name') or pack['id']} ({int(pack['credits'])} credits)"
    try:
        if data.provider == "stripe":
            co = await provider.create_checkout(payment_id=pid, name=name, amount=amount, currency=currency,
                                                success_url=back, cancel_url=back + "&cancelled=1")
        else:
            co = await provider.create_checkout(
                payment_id=pid, amount=amount, currency=currency,
                redirect_url=f"{settings.api_base}/api/billing/payments/safepay/return?payment={pid}",
                cancel_url=back + "&cancelled=1")
    except ProviderError as exc:
        _fail_payment(db, pid, str(exc))
        raise HTTPException(502, str(exc))
    db.execute(text("UPDATE payments SET provider_ref = :r WHERE id = :id"), {"r": co.ref, "id": pid})
    db.commit()
    return {"payment_id": pid, "checkout_url": co.url}


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id),
                      db: Session = Depends(get_db)):
    """Also confirms a pending payment by asking Stripe/Safepay, so it works where webhooks can't reach (local dev)."""
    q = text("SELECT * FROM payments WHERE id = :id AND user_id = :u")
    row = db.execute(q, {"id": str(payment_id), "u": str(user_id)}).first()
    if not row:
        raise HTTPException(404, "Payment not found.")
    if await _confirm_pending(db, row):
        row = db.execute(q, {"id": str(payment_id), "u": str(user_id)}).first()
    return _payment_out(row)


async def _confirm_pending(db: Session, row) -> bool:
    """Asks the provider about one pending payment and applies the answer. True if anything changed."""
    if row.status != "pending" or not row.provider_ref:
        return False
    if row.provider == "stripe" and (stripe := _stripe()):
        try:
            session = await stripe.get_session(row.provider_ref)
        except ProviderError as exc:
            log.warning("couldn't check Stripe session: %s", exc)
            return False
        await _apply_stripe_session(db, session)
        return True
    if row.provider == "safepay" and (safepay := _safepay()):
        if await safepay.is_paid(row.provider_ref):
            return await _credit_payment(db, str(row.id))
    return False


async def _apply_stripe_session(db: Session, session: dict) -> None:
    payment_id = (session.get("metadata") or {}).get("payment_id") or session.get("client_reference_id")
    if not payment_id:
        return
    row = db.execute(text("SELECT * FROM payments WHERE id = :id AND provider = 'stripe'"), {"id": payment_id}).first()
    if not row or row.provider_ref != session.get("id"):
        log.warning("Stripe session %s doesn't match payment %s", session.get("id"), payment_id)
        return
    if session.get("payment_status") == "paid":
        expected = stripe_minor_units(float(row.amount), row.currency)
        if session.get("amount_total") != expected or str(session.get("currency", "")).upper() != row.currency:
            log.error("Stripe amount mismatch on %s: %s %s", payment_id, session.get("amount_total"), session.get("currency"))
            _fail_payment(db, payment_id, "Amount paid doesn't match the pack price.")
            return
        await _credit_payment(db, payment_id)
    elif session.get("status") == "expired":
        _fail_payment(db, payment_id, "The checkout page expired.")


# --- public (the gateway lets these through without a login; each proves itself) ---

@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    stripe = _stripe()
    if stripe is None:
        raise HTTPException(404, "Stripe isn't set up.")
    try:
        event = stripe.verify_webhook(await request.body(), request.headers.get("stripe-signature", ""))
    except ProviderError as exc:
        raise HTTPException(400, str(exc))
    kind = event.get("type", "")
    session = (event.get("data") or {}).get("object") or {}
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.expired"):
        await _apply_stripe_session(db, session)
    elif kind == "checkout.session.async_payment_failed":
        pid = (session.get("metadata") or {}).get("payment_id")
        if pid:
            _fail_payment(db, pid, "The payment didn't go through.")
    return {"received": True}


@router.api_route("/payments/safepay/return", methods=["GET", "POST"])
async def safepay_return(request: Request, db: Session = Depends(get_db)):
    """Safepay sends the buyer's browser here after paying (form POST, or GET on some flows)."""
    fields = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    fields.update({k: v for k, v in request.query_params.items() if k not in fields})
    tracker = fields.get("tracker") or fields.get("token") or fields.get("beacon") or ""
    sig = fields.get("sig") or fields.get("signature") or ""
    order_id = fields.get("payment") or fields.get("order_id") or fields.get("orderId") or ""
    log.info("Safepay return: fields=%s", sorted(fields))  # names only, never values
    app_url = settings.APP_URL.rstrip("/")
    safepay = _safepay()
    row = None
    if tracker:
        row = db.execute(text("SELECT id, provider_ref FROM payments WHERE provider = 'safepay' AND provider_ref = :t"),
                         {"t": tracker}).first()
    if row is None and order_id:
        try:
            row = db.execute(text("SELECT id, provider_ref FROM payments WHERE provider = 'safepay' AND id = :id"),
                             {"id": str(uuid.UUID(order_id))}).first()
        except ValueError:
            row = None
    if row is None or safepay is None:
        log.warning("Safepay return for an unknown payment (order_id=%s)", order_id)
        return RedirectResponse(f"{app_url}/credits?failed=1", status_code=303)
    # 1) the signed redirect; 2) otherwise ask Safepay directly about OUR stored tracker.
    confirmed = bool(tracker) and tracker == row.provider_ref and safepay.verify_return(tracker, sig)
    if not confirmed:
        confirmed = bool(await safepay.is_paid(row.provider_ref))
        log.info("Safepay return: signature %s, Fetch Tracker says paid=%s",
                 "missing" if not sig else "didn't match", confirmed)
    if confirmed:
        await _credit_payment(db, str(row.id))
    # Not confirmed yet: the Credits page keeps asking (GET /payments/<id> checks Safepay again).
    return RedirectResponse(f"{app_url}/credits?payment={row.id}", status_code=303)


@router.post("/webhooks/safepay")
async def safepay_webhook(request: Request, db: Session = Depends(get_db)):
    safepay = _safepay()
    if safepay is None:
        raise HTTPException(404, "Safepay isn't set up.")
    data = safepay.verify_webhook(await request.body(), request.headers.get("x-sfpy-signature", ""))
    if data is None:
        raise HTTPException(400, "Invalid Safepay signature.")
    tracker, paid = safepay_webhook_result(data)
    if tracker and paid:
        row = db.execute(text("SELECT id FROM payments WHERE provider = 'safepay' AND provider_ref = :t"),
                         {"t": tracker}).first()
        if row:
            await _credit_payment(db, str(row.id))
        else:
            log.warning("Safepay webhook for unknown tracker %s", tracker)
    return {"received": True}


app = create_service("billing", routers=[router], bus=bus, engine=engine,
                     migrations_dir=Path(__file__).parent.parent / "migrations", background=[sweep_expired])
