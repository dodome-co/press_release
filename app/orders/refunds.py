"""返金（管理者）。基準の計算、Stripe への返金、記録、通知（Webhook）との突き合わせ。

返金額の合計が支払額を超えないよう、先にデータベースで「返金できる分」を1回の UPDATE で確保して
から、Stripe の返金 API を呼ぶ。Stripe が失敗したら、確保した分を戻す。
"""
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app
from sqlalchemy import func, update

from .. import payments
from ..extensions import db
from ..models import (
    ORDER_PAID,
    ORDER_PARTIALLY_REFUNDED,
    ORDER_REFUNDED,
    PROGRESS_IN_PROGRESS,
    PROGRESS_RECEIVED,
    REFUND_FAILED,
    REFUND_PENDING,
    REFUND_REASON_EXTERNAL,
    REFUND_REASON_NO_MEDIA,
    REFUND_REASON_POLICY,
    REFUND_REASON_SLOT_UNUSED,
    REFUND_SUCCEEDED,
    Order,
    OrderRefund,
    User,
    utcnow,
)
from ..releases import service as release_service
from ..services_catalog import INTERVIEW_PLANNING, RELEASE_CREATION
from ..textutil import clean_text

REFUNDABLE = (ORDER_PAID, ORDER_PARTIALLY_REFUNDED)
REASON_LABELS = {
    REFUND_REASON_POLICY: "返金の基準どおり",
    REFUND_REASON_NO_MEDIA: "当日、取材するメディアが一件もなかった（企画支援）",
    REFUND_REASON_SLOT_UNUSED: "使っていない枠の返金",
    REFUND_REASON_EXTERNAL: "Stripe の管理画面で直接返金",
}
NOTE_MAX_LENGTH = 500


class RefundError(Exception):
    """管理者に見せてよいメッセージを持つエラー。"""


@dataclass(frozen=True)
class RefundLimit:
    total_yen: int  # この注文で、合計いくらまで返金できるか（すでに返金した分を含む）
    remaining_yen: int  # あと、いくら返金できるか
    rule: str  # 適用される基準の説明


def slot_unused(order: Order) -> bool:
    """「リリース作成の追加」の枠が、まだ使われていないか。

    使用済みの数 U ≤ 無料の枠 F ＋ 支払済みの枠 P − 1 なら、この1つを返金しても、使用済みの数を賄える。
    """
    if order.user_id is None or order.status != ORDER_PAID:
        return False
    used = release_service.count_first_submitted(order.user_id)
    return used <= release_service.free_limit() + release_service.count_purchased_slots(order.user_id) - 1


def refund_limit(order: Order, reason: str = REFUND_REASON_POLICY) -> RefundLimit:
    amount = order.amount_yen
    if order.service_type == RELEASE_CREATION:
        if slot_unused(order):
            total, rule = amount, "使っていない枠は、全額を返金できます。"
        else:
            total, rule = 0, "すでに書類の提出に使った枠は、返金できません。"
    elif reason == REFUND_REASON_NO_MEDIA and order.service_type == INTERVIEW_PLANNING:
        total, rule = amount, "当日、取材するメディアが一件もなかったので、全額を返金できます。"
    elif order.progress == PROGRESS_RECEIVED:
        total, rule = amount, "進捗が「受付」なので、全額を返金できます。"
    elif order.progress == PROGRESS_IN_PROGRESS:
        total, rule = amount * 50 // 100, "進捗が「着手」なので、50%を返金できます。"
    else:
        total, rule = 0, "進捗が「納品済み」のため、返金できません。"
    return RefundLimit(total, max(0, total - order.refunded_amount), rule)


def _fail(message: str):
    db.session.rollback()
    raise RefundError(message)


def _status_after(order: Order) -> str:
    return ORDER_REFUNDED if order.refunded_amount >= order.amount_yen else ORDER_PARTIALLY_REFUNDED


def _refresh_status(order: Order) -> None:
    """返金額の合計に合わせて、注文の状態を整える（返金なし=支払い済み／一部／全額）。"""
    # 先に、ここまでの変更を書き込む。refresh は、関連する返金の行の、書き込み前の変更を捨ててしまうため
    db.session.flush()
    db.session.refresh(order)
    if order.status in (ORDER_PAID, ORDER_PARTIALLY_REFUNDED, ORDER_REFUNDED):
        order.status = ORDER_PAID if order.refunded_amount <= 0 else _status_after(order)


def _release_reservation(refund: OrderRefund) -> None:
    """確保した返金額を、元に戻す（1回の UPDATE。同時の操作と、数字が食い違わない）。"""
    db.session.execute(
        update(Order).where(Order.id == refund.order_id).values(refunded_amount=Order.refunded_amount - refund.amount_yen)
    )


def create_refund(order: Order, admin: User, amount_yen: int, reason: str, note: str | None = None) -> OrderRefund:
    """返金する。成功すると、返金の履歴（OrderRefund）を返す。"""
    if reason not in (REFUND_REASON_POLICY, REFUND_REASON_NO_MEDIA):
        raise RefundError("返金の理由が正しくありません。")
    if reason == REFUND_REASON_NO_MEDIA and order.service_type != INTERVIEW_PLANNING:
        raise RefundError("「メディアが一件もなかった」は、取材会の企画支援だけに使えます。")
    if order.service_type == RELEASE_CREATION:
        reason = REFUND_REASON_SLOT_UNUSED
    note = clean_text(note)
    if len(note) > NOTE_MAX_LENGTH:
        raise RefundError(f"メモは{NOTE_MAX_LENGTH}文字以内にしてください。")
    if not isinstance(amount_yen, int) or amount_yen <= 0:
        raise RefundError("返金額は、1円以上の整数で入力してください。")
    if not order.stripe_payment_intent_id:
        raise RefundError("この注文には、Stripe の支払いの記録がありません。")

    if order.service_type == RELEASE_CREATION and order.user_id is not None:
        # 同じ人の書類の提出と、同時に走らないようにする（枠が使われた直後に返金してしまわないように）
        db.session.execute(update(User).where(User.id == order.user_id).values(updated_at=utcnow()))
    db.session.refresh(order)
    if order.status not in REFUNDABLE:
        _fail("この注文は、返金できる状態ではありません。")
    if order.disputed_at is not None:
        _fail("カード会社への異議申し立て中の注文は、アプリからは返金できません。Stripe の管理画面で対応してください。")
    limit = refund_limit(order, reason)
    if amount_yen > limit.remaining_yen:
        _fail(f"返金できるのは、あと{limit.remaining_yen:,}円までです。（{limit.rule}）")
    if order.service_type == RELEASE_CREATION and amount_yen != order.amount_yen:
        _fail("「リリース作成の追加」は、全額（金額どおり）の返金だけできます。")

    # 返金できる分を、先に確保する（同時に2回押されても、合計が上限を超えない）
    progress_is_same = Order.progress.is_(None) if order.progress is None else Order.progress == order.progress
    rows = db.session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_(REFUNDABLE),
            Order.disputed_at.is_(None),
            progress_is_same,
            Order.refunded_amount + amount_yen <= limit.total_yen,
        )
        .values(refunded_amount=Order.refunded_amount + amount_yen, updated_at=utcnow())
    ).rowcount
    if rows != 1:
        _fail("同時に別の操作があったため、返金できませんでした。画面を開き直して、もう一度お試しください。")
    refund = OrderRefund(order_id=order.id, amount_yen=amount_yen, reason=reason, note=note or None,
                         status=REFUND_PENDING, created_by=admin.id)
    db.session.add(refund)
    db.session.commit()  # ここまでの確保を確定してから、Stripe を呼ぶ（待たせている間、ロックを持たない）

    try:
        result = payments.create_refund(
            order.stripe_payment_intent_id, amount_yen, f"refund-{order.id}-{refund.id}",
            {"order_id": str(order.id), "refund_id": str(refund.id)},
        )
    except payments.PaymentUncertain as e:
        # 通信の途切れなどで、Stripe で返金されたかどうか分からない。確保した分は、そのまま残す
        # （戻してやり直すと、二重に返金されるおそれがあるため）。あとで、Stripe に問い合わせて確定する
        raise RefundError(
            "Stripe からの応答を確認できませんでした。返金は「処理中」として残しています"
            "（二重に返金されないよう、返金額は確保したままです）。"
            "しばらくしてから、「処理中の返金を確認する」を押してください。"
        ) from e
    except payments.PaymentError as e:  # 拒否された（実行されていないことが分かっている）
        _release_reservation(refund)
        refund.status = REFUND_FAILED
        _refresh_status(order)
        db.session.commit()
        raise RefundError("Stripe での返金に失敗しました。もう一度お試しください。") from e

    if result["status"] not in ("succeeded", "pending"):
        # failed / canceled など。実際には返金されていない
        current_app.logger.error("Stripe の返金が成功しませんでした（注文ID=%s, 状態=%s）", order.id, result["status"])
        _release_reservation(refund)
        refund.status = REFUND_FAILED
        refund.stripe_refund_id = result["id"]
        _refresh_status(order)
        db.session.commit()
        raise RefundError("Stripe での返金に失敗しました。もう一度お試しください。")

    refund.stripe_refund_id = result["id"]
    refund.status = REFUND_SUCCEEDED if result["status"] == "succeeded" else REFUND_PENDING
    db.session.flush()
    _refresh_status(order)
    if note:
        order.refund_reason = note
    db.session.commit()
    return refund


# ---- 「処理中」の返金を、Stripe に問い合わせて確定する ----
def _apply_stripe_result(refund: OrderRefund, order: Order, found: dict | None) -> str:
    """Stripe の返金の状況を、アプリの記録に反映する。"""
    if found is not None:
        refund.stripe_refund_id = found["id"]
    if found is not None and found["status"] == "succeeded":
        refund.status = REFUND_SUCCEEDED
        outcome = "succeeded"
    elif found is not None and found["status"] == "pending":
        outcome = "still-pending"
    else:  # Stripe にない、または failed / canceled
        _release_reservation(refund)
        refund.status = REFUND_FAILED
        outcome = "failed"
    _refresh_status(order)
    return outcome


def resolve_pending(order: Order) -> dict:
    """この注文の「処理中」の返金を、Stripe に問い合わせて、成功か失敗かを確定する。

    作ってから一定時間（設定 REFUND_PENDING_MINUTES）が過ぎたものだけを対象にする（作った直後は、
    まだ処理中かもしれないため）。Stripe に問い合わせられなければ、PaymentUncertain。
    """
    minutes = current_app.config["REFUND_PENDING_MINUTES"]
    cutoff = utcnow() - timedelta(minutes=minutes)
    summary = {"succeeded": 0, "failed": 0, "still-pending": 0, "too-new": 0}
    for refund in OrderRefund.query.filter_by(order_id=order.id, status=REFUND_PENDING).all():
        if refund.created_at > cutoff:
            summary["too-new"] += 1
            continue
        found = payments.find_refund(order.stripe_payment_intent_id, refund.id)  # 失敗すると PaymentUncertain
        summary[_apply_stripe_result(refund, order, found)] += 1
    db.session.commit()
    return summary


def on_refund_updated(stripe_refund: dict) -> tuple[str, Order | None]:
    """refund.updated / refund.failed などの通知で、返金の状態をアプリの記録に反映する。"""
    refund = OrderRefund.query.filter_by(stripe_refund_id=stripe_refund.get("id")).first()
    if refund is None:
        return "ignored", None
    status = stripe_refund.get("status")
    order = db.session.get(Order, refund.order_id)
    if refund.status == REFUND_PENDING and status in ("succeeded", "failed", "canceled"):
        return _apply_stripe_result(refund, order, {"id": refund.stripe_refund_id, "status": status}), order
    if refund.status == REFUND_SUCCEEDED and status in ("failed", "canceled"):
        # 成功と記録したあとで、失敗に変わった（銀行口座への返金などで起きうる）
        current_app.logger.error("返金が、あとから失敗しました（注文ID=%s, 返金ID=%s）", order.id, refund.id)
        return _apply_stripe_result(refund, order, {"id": refund.stripe_refund_id, "status": status}), order
    return "ignored", order


# ---- Stripe からの通知との突き合わせ ----
def reconcile_charge_refunded(charge: dict) -> tuple[str, Order | None]:
    """charge.refunded の通知で、アプリの返金の記録を、Stripe の実際の返金額に合わせる。

    アプリからの返金は、すでに記録済みなので、何もしない。Stripe の管理画面で直接返金された分は、
    「外部」の返金として足す。
    """
    payment_intent = charge.get("payment_intent")
    order = Order.query.filter_by(stripe_payment_intent_id=payment_intent).first() if payment_intent else None
    if order is None:
        current_app.logger.error("返金の通知に、対応する注文がありません（支払い=%s）", payment_intent)
        return "no-order", None
    stripe_total = charge.get("amount_refunded") or 0
    recorded = (
        db.session.query(func.coalesce(func.sum(OrderRefund.amount_yen), 0))
        .filter(OrderRefund.order_id == order.id, OrderRefund.status.in_((REFUND_PENDING, REFUND_SUCCEEDED)))
        .scalar()
    )
    if stripe_total < recorded:
        return "ignored", order  # まだ Stripe 側の反映が追いついていない
    # Stripe が受け付けた返金（Stripe の返金 ID を持つもの）で、処理中のものは、成功として確定する。
    # ID のないもの（Stripe に届いたか分からないもの）は、ここでは確定しない（resolve_pending で確かめる）
    for refund in OrderRefund.query.filter_by(order_id=order.id, status=REFUND_PENDING).all():
        if refund.stripe_refund_id:
            refund.status = REFUND_SUCCEEDED
    extra = min(stripe_total - recorded, order.amount_yen - order.refunded_amount)
    if extra > 0:
        db.session.add(OrderRefund(order_id=order.id, amount_yen=extra, reason=REFUND_REASON_EXTERNAL,
                                   status=REFUND_SUCCEEDED))
        db.session.execute(  # 読み書きの食い違いを避けるため、SQL で足す
            update(Order).where(Order.id == order.id).values(refunded_amount=Order.refunded_amount + extra)
        )
        current_app.logger.warning("Stripe の管理画面で、%s円が直接返金されました（注文ID=%s）", extra, order.id)
    db.session.flush()
    db.session.refresh(order)
    if order.status in REFUNDABLE and order.refunded_amount > 0:
        order.status = _status_after(order)
    return ("external-refund" if extra > 0 else "synced"), order
