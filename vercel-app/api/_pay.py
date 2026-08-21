"""Payments: Razorpay + PayU. Amounts come from PACKS only — never from the client."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from _auth import User, current_user
from _db import PACKS, sb_get, sb_patch, sb_post

router = APIRouter(prefix="/api/pay")


def verify_rzp_signature(key_secret: str, order_id: str, payment_id: str, signature: str) -> bool:
    expect = hmac.new(key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, signature or "")


def credit_order(order: dict) -> None:
    """Mark paid + add credits. Idempotent: ledger insert only when no ledger row exists."""
    if order["status"] == "paid":
        return
    sb_patch("orders", {"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()},
             {"id": f"eq.{order['id']}", "status": "neq.paid"})
    fresh = sb_get("orders", {"id": f"eq.{order['id']}", "select": "status"})
    ledger = sb_get("credits_ledger", {"order_id": f"eq.{order['id']}", "select": "id"})
    if fresh and fresh[0]["status"] == "paid" and not ledger:
        sb_post("credits_ledger", {"user_id": order["user_id"], "delta": order["leads"],
                                   "reason": "purchase", "order_id": order["id"]}, prefer="return=minimal")


class RzpOrderIn(BaseModel):
    pack: str


@router.post("/razorpay/order")
def rzp_order(body: RzpOrderIn, user: User = Depends(current_user)):
    if body.pack not in PACKS:
        raise HTTPException(400, "unknown pack")
    p = PACKS[body.pack]
    kid, ks = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    if not kid or not ks:
        raise HTTPException(503, "Razorpay not configured")
    r = httpx.post("https://api.razorpay.com/v1/orders", auth=(kid, ks), timeout=20,
                   json={"amount": p["amount_inr"], "currency": "INR",
                         "notes": {"user_id": user.id, "pack": body.pack}})
    if r.status_code >= 300:
        raise HTTPException(502, f"razorpay: {r.text[:200]}")
    oid = r.json()["id"]
    sb_post("orders", {"user_id": user.id, "pack": body.pack, "leads": p["leads"],
                       "amount_inr": p["amount_inr"], "gateway": "razorpay",
                       "gateway_order_id": oid, "raw": r.json()}, prefer="return=minimal")
    return {"key_id": kid, "order_id": oid, "amount": p["amount_inr"], "currency": "INR", "pack": body.pack}


class RzpVerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/razorpay/verify")
def rzp_verify(body: RzpVerifyIn, user: User = Depends(current_user)):
    if not verify_rzp_signature(os.getenv("RAZORPAY_KEY_SECRET", ""),
                                body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(400, "bad signature")
    rows = sb_get("orders", {"gateway_order_id": f"eq.{body.razorpay_order_id}",
                             "user_id": f"eq.{user.id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "no such order")
    credit_order(rows[0])
    bal = sb_get("credit_balances", {"user_id": f"eq.{user.id}", "select": "balance"})
    return {"ok": True, "balance": bal[0]["balance"] if bal else 0}


# ── PayU ─────────────────────────────────────────────────────────────────────
import secrets as _secrets  # noqa: E402

from fastapi import Form  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402


def payu_request_hash(key: str, salt: str, txnid: str, amount: str, productinfo: str,
                      firstname: str, email: str, udf1: str, udf2: str) -> str:
    seq = f"{key}|{txnid}|{amount}|{productinfo}|{firstname}|{email}|{udf1}|{udf2}|||||||||{salt}"
    return hashlib.sha512(seq.encode()).hexdigest()


def payu_response_hash(key: str, salt: str, status: str, txnid: str, amount: str, productinfo: str,
                       firstname: str, email: str, udf1: str, udf2: str) -> str:
    seq = f"{salt}|{status}||||||||{udf2}|{udf1}|{email}|{firstname}|{productinfo}|{amount}|{txnid}|{key}"
    return hashlib.sha512(seq.encode()).hexdigest()


class PayUIn(BaseModel):
    pack: str


@router.post("/payu/initiate")
def payu_initiate(body: PayUIn, user: User = Depends(current_user)):
    if body.pack not in PACKS:
        raise HTTPException(400, "unknown pack")
    key, salt = os.getenv("PAYU_KEY", ""), os.getenv("PAYU_SALT", "")
    base = os.getenv("PAYU_BASE", "https://test.payu.in").rstrip("/")
    app_base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not key or not salt or not app_base:
        raise HTTPException(503, "PayU not configured")
    p = PACKS[body.pack]
    txnid = "ws" + _secrets.token_hex(10)
    amount = f"{p['amount_inr'] / 100:.2f}"
    fields = {"key": key, "txnid": txnid, "amount": amount, "productinfo": body.pack,
              "firstname": "Member", "email": user.email or "member@leadfinder.local",
              "udf1": user.id, "udf2": body.pack,
              "surl": f"{app_base}/api/pay/payu/return", "furl": f"{app_base}/api/pay/payu/return"}
    fields["hash"] = payu_request_hash(key, salt, txnid, amount, body.pack,
                                       fields["firstname"], fields["email"], user.id, body.pack)
    sb_post("orders", {"user_id": user.id, "pack": body.pack, "leads": p["leads"],
                       "amount_inr": p["amount_inr"], "gateway": "payu",
                       "gateway_order_id": txnid}, prefer="return=minimal")
    return {"action": f"{base}/_payment", "fields": fields}


@router.post("/payu/return")
def payu_return(status: str = Form(""), txnid: str = Form(""), amount: str = Form(""),
                productinfo: str = Form(""), firstname: str = Form(""), email: str = Form(""),
                udf1: str = Form(""), udf2: str = Form(""), hash: str = Form("")):
    key, salt = os.getenv("PAYU_KEY", ""), os.getenv("PAYU_SALT", "")
    expect = payu_response_hash(key, salt, status, txnid, amount, productinfo, firstname, email, udf1, udf2)
    ok = hmac.compare_digest(expect, hash) and status == "success"
    if ok:
        rows = sb_get("orders", {"gateway_order_id": f"eq.{txnid}", "select": "*"})
        if rows:
            credit_order(rows[0])
        else:
            ok = False
    else:
        sb_patch("orders", {"status": "failed"}, {"gateway_order_id": f"eq.{txnid}", "status": "neq.paid"})
    return RedirectResponse(url=f"/#billing?status={'ok' if ok else 'failed'}", status_code=303)


@router.post("/razorpay/webhook")
async def rzp_webhook(request: Request, x_razorpay_signature: str = Header(default="")):
    body = await request.body()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "webhook secret not configured")
    expect = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, x_razorpay_signature):
        raise HTTPException(400, "bad signature")
    evt = json.loads(body)
    if evt.get("event") == "payment.captured":
        oid = evt["payload"]["payment"]["entity"].get("order_id")
        rows = sb_get("orders", {"gateway_order_id": f"eq.{oid}", "select": "*"}) if oid else []
        if rows:
            credit_order(rows[0])
    return {"ok": True}
