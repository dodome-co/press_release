"""Stripe との通信を1か所にまとめる。テストでは、この関数を差し替える。"""
import time

import stripe
from flask import current_app


class PaymentError(Exception):
    """決済の開始などに失敗したとき（利用者には、詳しい理由を見せない）。"""


class PaymentRejected(PaymentError):
    """Stripe が、処理を実行しなかったことが分かっている失敗（入力の誤りなど）。やり直してよい。"""


class PaymentUncertain(PaymentError):
    """通信の途切れなどで、Stripe で処理されたかどうか分からない失敗。結果を確かめるまで、やり直してはいけない。"""


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


def _is_definitely_not_executed(error: Exception) -> bool:
    """Stripe が、処理を実行しなかったと言える種類のエラーか（それ以外は、結果が分からないものとして扱う）。"""
    return isinstance(
        error,
        (
            stripe.InvalidRequestError,
            stripe.AuthenticationError,
            stripe.PermissionError,
            stripe.RateLimitError,
        ),
    )


def create_refund(payment_intent_id: str, amount_yen: int, idempotency_key: str, metadata: dict | None = None) -> dict:
    """返金を行う。必要な値だけの普通の dict（id と status）を返す。

    失敗したとき：実行されなかったと分かれば PaymentRejected、結果が分からなければ PaymentUncertain。
    """
    if payment_intent_id.startswith("pi_demo_") and current_app.config.get("DEMO_MODE"):
        # デモ用のサンプルの注文（本物の支払いはない）。デモ環境でだけ、返金を模擬する
        return {"id": f"re_demo_{idempotency_key}", "status": "succeeded"}
    _configure()
    try:
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=amount_yen,
            reason="requested_by_customer",
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )
        return {"id": refund["id"], "status": refund["status"]}
    except stripe.StripeError as e:
        current_app.logger.error("返金に失敗しました（支払い=%s, 金額=%s）：%s", payment_intent_id, amount_yen, type(e).__name__)
        if _is_definitely_not_executed(e):
            raise PaymentRejected("返金が拒否されました") from e
        raise PaymentUncertain("返金の結果が確認できませんでした") from e


def find_refund(payment_intent_id: str, refund_id: int) -> dict | None:
    """この支払いの返金から、アプリの返金の行（metadata の refund_id）に対応するものを探す。なければ None。

    Stripe に問い合わせられなかったときは、PaymentUncertain。
    """
    _configure()
    try:
        for refund in stripe.Refund.list(payment_intent=payment_intent_id, limit=100).auto_paging_iter():
            # Stripe のオブジェクトは dict の .get() が使えないので、[] で読む
            try:
                metadata = refund["metadata"]
                found = metadata["refund_id"] == str(refund_id)
            except KeyError:
                continue
            if found:
                return {"id": refund["id"], "status": refund["status"]}
    except stripe.StripeError as e:
        raise PaymentUncertain("返金の状況を確認できませんでした") from e
    return None


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
