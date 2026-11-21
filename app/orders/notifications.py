from flask import current_app

from ..mail import send_mail
from ..models import ROLE_ADMIN, User
from ..security import external_url
from ..services_catalog import refund_policy_text, support_contact


def notify_order_paid(order) -> None:
    """注文確認メール（F18）。送れなくても、支払いの確定は取り消さない。"""
    if order is None or order.user is None:
        return
    link = external_url("orders.success", order_id=order.id)
    body = (
        f"ご注文ありがとうございます。お支払いを確認しました。\n\n"
        f"サービス：{order.service_name}\n金額：{order.amount_yen:,}円（税込）\n注文番号：{order.id}\n\n"
        f"{link}\n\n{refund_policy_text()}\nお問い合わせ先：{support_contact()}\n"
    )
    try:
        send_mail(order.user.email, "【ねたもと。】ご注文を承りました", body)
    except Exception:
        current_app.logger.exception("注文確認メールの送信に失敗しました（注文ID=%s）", order.id)


def notify_duplicate_payment(order, payment_intent) -> None:
    """同じ注文に、別の支払いが届いたことを、管理者に知らせる（二重請求の可能性。人が確認して返金する）。"""
    body = (
        "同じ注文に対して、2回目の支払いが届きました。二重に請求された可能性があります。\n"
        f"注文番号：{order.id}\nサービス：{order.service_name}\n金額：{order.amount_yen:,}円\n"
        f"2回目の支払い（Stripe の PaymentIntent）：{payment_intent}\n\n"
        "Stripe の管理画面で確認し、必要なら2回目の支払いを返金してください。"
    )
    for admin in User.query.filter_by(role=ROLE_ADMIN).all():
        try:
            send_mail(admin.email, "【要確認】二重に支払われた可能性があります", body)
        except Exception:
            current_app.logger.exception("二重払いの通知メールの送信に失敗しました（注文ID=%s）", order.id)


def notify_delivered(order) -> None:
    """納品したことを、クライアントに知らせる。送れなくても、納品は取り消さない。"""
    if order is None or order.user is None:
        return
    link = external_url("mypage.order_detail", order_id=order.id)
    body = (
        f"「{order.service_name}」を納品しました。マイページからご確認ください。\n{link}\n\n"
        "修正のご依頼は、お問い合わせ先までご連絡ください。\n"
        f"お問い合わせ先：{support_contact()}\n"
    )
    try:
        send_mail(order.user.email, "【ねたもと。】納品が完了しました", body)
    except Exception:
        current_app.logger.exception("納品のメールの送信に失敗しました（注文ID=%s）", order.id)


def _notify_admins(subject: str, body: str, order_id) -> None:
    for admin in User.query.filter_by(role=ROLE_ADMIN).all():
        try:
            send_mail(admin.email, subject, body)
        except Exception:
            current_app.logger.exception("管理者への通知メールの送信に失敗しました（注文ID=%s）", order_id)


def notify_dispute(order) -> None:
    """カード会社への異議申し立てがあったことを、管理者に知らせる（期限内に Stripe の管理画面で対応が必要）。"""
    link = external_url("admin_orders.detail", order_id=order.id)
    _notify_admins(
        "【要対応】カード会社への異議申し立てがありました",
        f"注文番号：{order.id}\nサービス：{order.service_name}\n金額：{order.amount_yen:,}円\n\n"
        f"Stripe の管理画面で、期限内に証拠を提出するなどの対応が必要です。\n{link}",
        order.id,
    )


def notify_external_refund(order) -> None:
    """Stripe の管理画面で直接返金されたことを、管理者に知らせる（アプリの記録は、通知で合わせた）。"""
    link = external_url("admin_orders.detail", order_id=order.id)
    _notify_admins(
        "Stripe の管理画面で、返金されました",
        f"注文番号：{order.id}\nサービス：{order.service_name}\n返金額の合計：{order.refunded_amount:,}円\n\n"
        f"アプリの記録は、Stripe の通知に合わせて更新しました。\n{link}",
        order.id,
    )


def notify_refunded(order, amount_yen: int) -> None:
    """返金したことを、クライアントに知らせる。送れなくても、返金は取り消さない。"""
    if order is None or order.user is None:
        return
    body = (
        f"「{order.service_name}」について、{amount_yen:,}円を返金しました。\n"
        "カードへの反映には、カード会社により、数日〜数週間かかることがあります。\n\n"
        f"お問い合わせ先：{support_contact()}\n"
    )
    try:
        send_mail(order.user.email, "【ねたもと。】返金のお知らせ", body)
    except Exception:
        current_app.logger.exception("返金のメールの送信に失敗しました（注文ID=%s）", order.id)


def notify_unmatched_event(event_type: str, payment_intent) -> None:
    """注文に結びつかない返金・異議申し立ての通知を、管理者に知らせる（Stripe の管理画面で確認が必要）。"""
    _notify_admins(
        "【要確認】注文に結びつかない通知を受けました",
        f"Stripe から「{event_type}」の通知を受けましたが、対応する注文が見つかりません。\n"
        f"支払い（PaymentIntent）：{payment_intent}\n\nStripe の管理画面で、内容を確認してください。",
        None,
    )


def notify_refund_failed(order) -> None:
    """返金が、あとから失敗したことを、管理者に知らせる（返金額は、注文から戻した）。"""
    link = external_url("admin_orders.detail", order_id=order.id)
    _notify_admins(
        "【要確認】返金が失敗しました",
        f"注文番号：{order.id}\nサービス：{order.service_name}\n\n"
        f"Stripe の返金が失敗したため、アプリの返金額を戻しました。状況を確認してください。\n{link}",
        order.id,
    )
