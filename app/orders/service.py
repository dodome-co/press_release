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
    StripeEvent,
    User,
    utcnow,
)
from ..releases import service as release_service
from ..security import external_url
from ..services_catalog import MIN_PAYMENT_YEN, RELEASE_CREATION, get_service
from ..textutil import clean_text


class OrderError(Exception):
    """利用者に見せてよいメッセージを持つエラー。"""


def _fail(message: str):
    db.session.rollback()
    raise OrderError(message)


def _valid_url(url: str) -> bool:
    return url.lower().startswith(("http://", "https://")) and len(url) <= 500 and " " not in url


# ---- 申込み ----
def create_order(user: User, service_key: str, materials_url: str | None = None) -> Order:
    """未払いの注文を作る（金額は、サービスの一覧から取る。画面の値は使わない）。"""
    service = get_service(service_key)
    if service is None:
        raise OrderError("サービスが見つかりません。")
    if service.price_yen < MIN_PAYMENT_YEN:
        raise OrderError("このサービスは、いまはお申し込みできません。")

    materials = clean_text(materials_url) if service.needs_materials_url else ""
    if service.needs_materials_url:
        if not materials:
            raise OrderError("素材（写真・動画）の共有リンクを入力してください。")
        if not _valid_url(materials):
            raise OrderError("素材の共有リンクは、http:// または https:// で始まる URL を入力してください。")

    if service.key == RELEASE_CREATION:
        if release_service.can_create(user):
            raise OrderError("無料の枠がまだ残っているので、いまは購入できません。")
        # 未払いの注文があれば、新しく作らずに、それを使う
        existing = Order.query.filter_by(
            user_id=user.id, service_type=RELEASE_CREATION, status=ORDER_UNPAID
        ).first()
        if existing is not None:
            return existing

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


def start_checkout(order: Order, attempt: int = 1) -> str:
    """Stripe の支払い画面の URL を返す。"""
    if order.status != ORDER_UNPAID:
        raise OrderError("この注文は、すでにお支払い済みか、取り消されています。")
    success_url = external_url("orders.success", order_id=order.id) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = external_url("orders.cancel", order_id=order.id)
    try:
        session = payments.create_checkout_session(order, order.user.email, success_url, cancel_url, attempt)
    except payments.PaymentError as e:
        raise OrderError("決済を開始できませんでした。しばらくしてから、もう一度お試しください。") from e
    order.stripe_session_id = session["id"]
    db.session.commit()
    return session["url"]


def cancel_unpaid(order: Order) -> bool:
    """未払いの注文だけを取り消す。取り消せたら True。"""
    session_id = order.stripe_session_id
    rows = db.session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == ORDER_UNPAID)
        .values(status=ORDER_CANCELED, updated_at=utcnow())
    ).rowcount
    db.session.commit()
    db.session.refresh(order)
    if rows == 1 and session_id:
        payments.expire_checkout_session(session_id)  # 取り消したあとに支払われないようにする
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
        from .notifications import notify_order_paid

        notify_order_paid(db.session.get(Order, paid_order_id))
    return result


def _find_order(obj) -> Order | None:
    raw = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
    if not raw or not str(raw).isdigit():
        return None
    return db.session.get(Order, int(raw))


def _on_paid(session) -> tuple[str, int | None]:
    if session.get("payment_status") != "paid":
        return "not-paid-yet", None  # 後払いなど。確定したら、別のイベントが来る
    order = _find_order(session)
    if order is None:
        current_app.logger.error("支払いの通知に、対応する注文がありません（セッション=%s）", session.get("id"))
        return "no-order", None
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
