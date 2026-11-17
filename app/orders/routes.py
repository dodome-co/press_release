from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import payments
from ..auth.decorators import client_required
from ..extensions import csrf, db
from ..models import ORDER_UNPAID, Order
from ..releases import service as release_service
from ..services_catalog import REFUND_POLICY, SERVICE_NOTES, all_services, get_service, support_contact
from . import bp, service


def _own_order_or_404(order_id: int) -> Order:
    """自分の注文だけを返す。他人の注文は、存在の有無が分からないよう 404 にする。"""
    order = db.session.get(Order, order_id)
    if order is None or order.user_id != current_user.id:
        abort(404)
    return order


@bp.get("/services")
def services():
    """サービスのご案内。ログインしていなくても見られる。申し込めるのは、ログインしたクライアントだけ。"""
    can_order = current_user.is_authenticated and not current_user.is_admin
    return render_template(
        "orders/services.html",
        services=all_services(),
        can_order=can_order,
        can_create_release=can_order and release_service.can_create(current_user),
        refund_policy=REFUND_POLICY,
        notes=SERVICE_NOTES,
        support=support_contact(),
    )


@bp.post("/services/<key>/order")
@client_required
def order_service(key):
    if get_service(key) is None:
        abort(404)
    try:
        order = service.create_order(current_user, key, request.form.get("materials_url"), request.form.get("option"))
        url = service.start_checkout(order)
    except service.OrderError as e:
        flash(str(e), "error")
        return redirect(url_for("orders.services"))
    return redirect(url, code=303)  # Stripe の支払い画面へ


@bp.get("/orders/<int:order_id>/success")
@client_required
def success(order_id):
    """支払いの完了ページ。ここでは支払いを確定しない（確定は Webhook だけ）。"""
    order = _own_order_or_404(order_id)
    return render_template("orders/success.html", order=order, support=support_contact())


@bp.get("/orders/<int:order_id>/cancel")
@client_required
def cancel(order_id):
    """中断ページ。注文の取り消しか、支払いのやり直しを選ぶ。"""
    order = _own_order_or_404(order_id)
    return render_template("orders/cancel.html", order=order)


@bp.post("/orders/<int:order_id>/cancel")
@client_required
def cancel_post(order_id):
    order = _own_order_or_404(order_id)
    if service.cancel_unpaid(order):
        flash("注文を取り消しました。", "success")
    else:
        flash("この注文は、取り消せません。", "error")
    return redirect(url_for("orders.services"))


@bp.post("/orders/<int:order_id>/pay")
@client_required
def pay_again(order_id):
    order = _own_order_or_404(order_id)
    try:
        url = service.start_checkout(order)
    except service.OrderError as e:
        flash(str(e), "error")
        return redirect(url_for("orders.services"))
    return redirect(url, code=303)


@bp.post("/stripe/webhook")
@csrf.exempt
def webhook():
    """Stripe からの通知。署名が正しいものだけを受け付ける。"""
    try:
        event = payments.construct_event(request.get_data(), request.headers.get("Stripe-Signature"))
    except payments.PaymentError:
        current_app.logger.error("Stripe の Webhook の設定（STRIPE_WEBHOOK_SECRET）が足りません")
        return "", 400
    except (ValueError, payments.SignatureError):
        return "", 400
    try:
        result = service.handle_event(event)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("Stripe の通知の処理に失敗しました（イベント=%s）", event.get("id"))
        db.session.rollback()
        return "", 500  # Stripe が、あとで再送する
    current_app.logger.info("Stripe の通知を処理しました（イベント=%s, 結果=%s）", event.get("id"), result)
    return "", 200
