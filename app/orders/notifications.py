from flask import current_app

from ..mail import send_mail
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
