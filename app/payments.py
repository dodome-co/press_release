"""Stripe との通信を1か所にまとめる。テストでは、この関数を差し替える。"""
import time

import stripe
from flask import current_app


class PaymentError(Exception):
    """決済の開始などに失敗したとき（利用者には、詳しい理由を見せない）。"""


def _configure() -> None:
    key = current_app.config["STRIPE_SECRET_KEY"]
    if not key:
        raise PaymentError("STRIPE_SECRET_KEY が設定されていません")
    stripe.api_key = key


def create_checkout_session(order, customer_email: str, success_url: str, cancel_url: str, attempt: int = 1):
    """Checkout のセッションを作る。金額とサービス名は、画面ではなく注文（サーバー側）の値を使う。"""
    _configure()
    # Stripe の Checkout は、有効時間を30分〜24時間にしか設定できない
    minutes = max(30, min(24 * 60, current_app.config["CHECKOUT_EXPIRES_MINUTES"]))
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "jpy",
                        "product_data": {"name": order.service_name},
                        "unit_amount": order.amount_yen,
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(order.id),
            metadata={"order_id": str(order.id)},
            customer_email=customer_email,
            expires_at=int(time.time()) + minutes * 60,
            idempotency_key=f"checkout-order-{order.id}-{attempt}",
        )
        # Stripe のオブジェクトは dict の .get() が使えないので、必要な値だけの普通の dict にして返す
        return {"id": session["id"], "url": session["url"]}
    except stripe.StripeError as e:
        current_app.logger.error("Checkout セッションを作れませんでした（注文ID=%s）：%s", order.id, type(e).__name__)
        raise PaymentError("決済を開始できませんでした") from e


def expire_checkout_session(session_id: str) -> None:
    """未払いのセッションを無効にする（失敗しても、注文の取り消しは止めない）。"""
    try:
        _configure()
        stripe.checkout.Session.expire(session_id)
    except Exception:  # noqa: BLE001
        current_app.logger.warning("Checkout セッションを無効にできませんでした（%s）", session_id)


def construct_event(payload: bytes, signature_header: str | None):
    """Webhook の署名を確認して、イベントを返す。署名が合わなければ例外。"""
    secret = current_app.config["STRIPE_WEBHOOK_SECRET"]
    if not secret:
        raise PaymentError("STRIPE_WEBHOOK_SECRET が設定されていません")
    event = stripe.Webhook.construct_event(payload, signature_header, secret)
    return event.to_dict()  # Stripe のオブジェクトは dict の .get() が使えないので、普通の dict にする


SignatureError = stripe.SignatureVerificationError
