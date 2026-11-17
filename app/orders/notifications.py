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
        send_mail(order.user.email, "【プレスリリースサポート】ご注文を承りました", body)
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
