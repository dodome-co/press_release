"""注文と決済の規則。状態の変更は、必ずここを通す。"""
from flask import current_app, url_for
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from .. import payments
from ..extensions import db
from ..models import (
    ORDER_CANCELED,
    ORDER_PAID,
    ORDER_UNPAID,
    PROGRESS_RECEIVED,
    Order,
    OrderCheckoutSession,
    StripeEvent,
    User,
    utcnow,
)
from ..releases import service as release_service
from ..security import external_url
from ..services_catalog import MIN_PAYMENT_YEN, RELEASE_CREATION, get_service
from ..textutil import is_safe_http_url


class OrderError(Exception):
    """利用者に見せてよいメッセージを持つエラー。"""


def _fail(message: str):
    db.session.rollback()
    raise OrderError(message)


def _is_checkout_url(url) -> bool:
    """Stripe の支払い画面の URL か（https の stripe.com のドメインだけを受け付ける）。"""
    from urllib.parse import urlsplit

    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "stripe.com" or host.endswith(".stripe.com"))


# ---- 申込み ----
def create_order(user: User, service_key: str, materials_url: str | None = None) -> Order:
    """未払いの注文を作る（金額は、サービスの一覧から取る。画面の値は使わない）。"""
    service = get_service(service_key)
    if service is None:
        raise OrderError("サービスが見つかりません。")
    if service.price_yen < MIN_PAYMENT_YEN:
        raise OrderError("このサービスは、いまはお申し込みできません。")

    materials = (materials_url or "").strip() if service.needs_materials_url else ""
    if service.needs_materials_url:
        if not materials:
            raise OrderError("素材（写真・動画）の共有リンクを入力してください。")
        if not is_safe_http_url(materials):
            raise OrderError(
                "素材の共有リンクは、http:// または https:// で始まる URL を、空白や改行を含めずに入力してください。"
            )

    if service.key == RELEASE_CREATION:
        if release_service.can_create(user):
            raise OrderError("無料の枠がまだ残っているので、いまは購入できません。")
        # 未払いの注文があれば、新しく作らずに、それを使う
        existing = Order.query.filter_by(
            user_id=user.id, service_type=RELEASE_CREATION, status=ORDER_UNPAID
        ).first()
        if existing is not None:
            return existing

    # 申込みの連打で、未払いの注文が増え続けないようにする
    max_unpaid = current_app.config["MAX_UNPAID_ORDERS"]
    if Order.query.filter_by(user_id=user.id, status=ORDER_UNPAID).count() >= max_unpaid:
        raise OrderError("お支払い前の注文が多すぎます。不要な注文を取り消してから、お申し込みください。")

    order = Order(
        user_id=user.id,
        service_type=service.key,
        service_name=service.name,
        amount_yen=service.price_yen,
        materials_url=materials or None,
    )
    db.session.add(order)
    db.session.commit()
    return order


def _expire_sessions(order: Order) -> None:
    """この注文の、これまでの支払い画面をすべて無効にする（古い画面で、あとから二重に支払われないように）。"""
    for row in OrderCheckoutSession.query.filter_by(order_id=order.id).all():
        payments.expire_checkout_session(row.stripe_session_id)


def start_checkout(order: Order) -> str:
    """Stripe の支払い画面の URL を返す。呼ぶたびに、前の支払い画面は無効になる。"""
    if order.status != ORDER_UNPAID:
        raise OrderError("この注文は、すでにお支払い済みか、取り消されています。")

    # 支払い画面を作った回数を、1回の UPDATE で加える（この値が、Stripe への識別キーの一部になる）
    rows = db.session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == ORDER_UNPAID)
        .values(checkout_attempts=Order.checkout_attempts + 1, updated_at=utcnow())
    ).rowcount
    db.session.commit()
    if rows != 1:
        db.session.refresh(order)
        raise OrderError("この注文は、すでにお支払い済みか、取り消されています。")
    db.session.refresh(order)
    attempt = order.checkout_attempts

    _expire_sessions(order)
    success_url = external_url("orders.success", order_id=order.id) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = external_url("orders.cancel", order_id=order.id)
    try:
        session = payments.create_checkout_session(order, order.user.email, success_url, cancel_url, attempt)
    except payments.PaymentError as e:
        raise OrderError("決済を開始できませんでした。しばらくしてから、もう一度お試しください。") from e

    if not _is_checkout_url(session.get("url")) or not session.get("id"):
        current_app.logger.error("Stripe の応答が想定と違います（注文ID=%s）", order.id)
        raise OrderError("決済を開始できませんでした。しばらくしてから、もう一度お試しください。")

    # 作っている間に、すでに支払い・取り消しされていたら、この新しい支払い画面は使わない
    db.session.add(OrderCheckoutSession(order_id=order.id, stripe_session_id=session["id"]))
    rows = db.session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == ORDER_UNPAID)
        .values(stripe_session_id=session["id"], updated_at=utcnow())
    ).rowcount
    db.session.commit()
    if rows != 1:
        payments.expire_checkout_session(session["id"])
        db.session.refresh(order)
        raise OrderError("この注文は、すでにお支払い済みか、取り消されています。")
    return session["url"]


def cancel_unpaid(order: Order) -> bool:
    """未払いの注文だけを取り消す。取り消せたら True。"""
    rows = db.session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == ORDER_UNPAID)
        .values(status=ORDER_CANCELED, updated_at=utcnow())
    ).rowcount
    db.session.commit()
    db.session.refresh(order)
    if rows == 1:
        _expire_sessions(order)  # 取り消したあとに、どの画面からも支払われないようにする
    return rows == 1


# ---- Webhook ----
HANDLED_PAID = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


def handle_event(event) -> str:
    """Stripe のイベントを処理する。結果の説明（ログ・テスト用）を返す。

    同じイベントは2回処理しない。処理に失敗したら、例外を投げる（Stripe が後で再送する）。
    """
    event_id, event_type = event["id"], event["type"]
    db.session.add(StripeEvent(event_id=event_id, event_type=event_type))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return "duplicate"

    obj = event["data"]["object"]
    result = "ignored"
    paid_order_id = None
    if event_type in HANDLED_PAID:
        result, paid_order_id = _on_paid(obj)
    elif event_type == "checkout.session.expired":
        result = _on_expired(obj)
    db.session.commit()  # イベントの記録と、注文の更新を、まとめて確定する

    if paid_order_id is not None:
        from .notifications import notify_duplicate_payment, notify_order_paid

        order = db.session.get(Order, paid_order_id)
        if result == "paid":
            notify_order_paid(order)
        elif result == "duplicate-payment":
            notify_duplicate_payment(order, obj.get("payment_intent"))
    return result


def _find_order(obj) -> Order | None:
    raw = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
    if not raw or not str(raw).isdigit() or len(str(raw)) > 12:
        return None
    return db.session.get(Order, int(raw))


def _on_paid(session) -> tuple[str, int | None]:
    if session.get("payment_status") != "paid":
        return "not-paid-yet", None  # 後払いなど。確定したら、別のイベントが来る
    order = _find_order(session)
    if order is None:
        current_app.logger.error("支払いの通知に、対応する注文がありません（セッション=%s）", session.get("id"))
        return "no-order", None
    # この注文のために作った支払い画面のものか（同じ Stripe アカウントの、別の用途の支払いを取り込まない）
    known = OrderCheckoutSession.query.filter_by(
        order_id=order.id, stripe_session_id=session.get("id")
    ).first()
    if known is None:
        current_app.logger.error(
            "この注文のものではない支払い画面の通知です（注文ID=%s, セッション=%s）", order.id, session.get("id")
        )
        return "session-mismatch", None
    if session.get("amount_total") != order.amount_yen or (session.get("currency") or "").lower() != "jpy":
        # 金額が合わないものは、確定しない（人が確認する）
        current_app.logger.error(
            "支払いの金額が注文と合いません（注文ID=%s, 注文=%s円, 通知=%s %s）",
            order.id, order.amount_yen, session.get("amount_total"), session.get("currency"),
        )
        return "amount-mismatch", None

    service = get_service(order.service_type)
    values = {
        "status": ORDER_PAID,
        "paid_at": utcnow(),
        "stripe_session_id": session.get("id"),
        "stripe_payment_intent_id": session.get("payment_intent"),
        "updated_at": utcnow(),
    }
    if service is None or service.has_delivery:
        values["progress"] = PROGRESS_RECEIVED
    # 「未払い」か「取り消し済み」のときだけ確定する（取り消した後に支払われた場合も、お金は受け取っているので記録する）
    rows = db.session.execute(
        update(Order).where(Order.id == order.id, Order.status.in_((ORDER_UNPAID, ORDER_CANCELED))).values(**values)
    ).rowcount
    if rows != 1:
        # すでに支払い済み。別の支払いなら、二重に請求された可能性がある（人が確認して、返金する）
        db.session.refresh(order)
        if order.stripe_payment_intent_id and session.get("payment_intent") != order.stripe_payment_intent_id:
            current_app.logger.error(
                "二重に支払われた可能性があります（注文ID=%s, 支払い済み=%s, 今回=%s）",
                order.id, order.stripe_payment_intent_id, session.get("payment_intent"),
            )
            return "duplicate-payment", order.id
        return "already-paid", None
    return "paid", order.id


def _on_expired(session) -> str:
    order = _find_order(session)
    if order is None:
        return "no-order"
    rows = db.session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == ORDER_UNPAID, Order.stripe_session_id == session.get("id"))
        .values(status=ORDER_CANCELED, updated_at=utcnow())
    ).rowcount
    return "canceled" if rows == 1 else "ignored"
