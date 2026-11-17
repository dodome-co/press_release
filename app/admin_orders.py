"""管理者の画面：注文の一覧・詳細・納期・進捗・納品・返金。"""
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from . import payments
from .auth.decorators import admin_required
from .extensions import db
from .models import (
    ORDER_CANCELED,
    ORDER_PAID,
    ORDER_PARTIALLY_REFUNDED,
    ORDER_REFUNDED,
    ORDER_UNPAID,
    Order,
)
from .mypage import send_delivery_file
from .orders import management, notifications, refunds
from .services_catalog import all_services
from .uploads import UploadError

bp = Blueprint("admin_orders", __name__, url_prefix="/admin/orders")

ORDER_STATUSES = (ORDER_UNPAID, ORDER_PAID, ORDER_PARTIALLY_REFUNDED, ORDER_REFUNDED, ORDER_CANCELED)
PROGRESSES = ("received", "in_progress", "delivered")


def _order_or_404(order_id: int) -> Order:
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    return order


def _back(order: Order):
    return redirect(url_for("admin_orders.detail", order_id=order.id))


def _run(action, success_message: str) -> bool:
    """管理の操作を実行し、規則に反したときはメッセージにする。"""
    try:
        action()
    except (management.ManagementError, UploadError) as e:
        flash(str(e), "error")
        return False
    flash(success_message, "success")
    return True


@bp.get("")
@admin_required
def orders():
    status = request.args.get("status", "")
    progress = request.args.get("progress", "")
    service = request.args.get("service", "")
    query = Order.query
    if status in ORDER_STATUSES:
        query = query.filter_by(status=status)
    if progress in PROGRESSES:
        query = query.filter_by(progress=progress)
    if service in {s.key for s in all_services()}:
        query = query.filter_by(service_type=service)
    orders = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(500).all()
    return render_template(
        "admin/orders.html",
        orders=orders,
        status=status,
        progress=progress,
        service=service,
        statuses=ORDER_STATUSES,
        progresses=PROGRESSES,
        services=all_services(),
    )


@bp.get("/<int:order_id>")
@admin_required
def detail(order_id):
    order = _order_or_404(order_id)
    return render_template(
        "admin/order_detail.html",
        order=order,
        has_delivery=management.has_delivery(order),
        workable=order.status in management.WORKABLE,
        refundable=order.status in refunds.REFUNDABLE and bool(order.stripe_payment_intent_id),
        limit=refunds.refund_limit(order),
        limit_no_media=refunds.refund_limit(order, "no_media"),
        reason_labels=refunds.REASON_LABELS,
    )


@bp.post("/<int:order_id>/due-date")
@admin_required
def set_due_date(order_id):
    order = _order_or_404(order_id)
    raw = (request.form.get("due_date") or "").strip()
    try:
        due = date.fromisoformat(raw) if raw else None
    except ValueError:
        flash("納期日は、日付で入力してください。", "error")
        return _back(order)
    _run(lambda: management.set_due_date(order, due), "納期日を保存しました。")
    return _back(order)


@bp.post("/<int:order_id>/start")
@admin_required
def start_work(order_id):
    order = _order_or_404(order_id)
    _run(lambda: management.start_work(order), "進捗を「着手」にしました。")
    return _back(order)


@bp.post("/<int:order_id>/revisions")
@admin_required
def set_revisions(order_id):
    order = _order_or_404(order_id)
    raw = (request.form.get("free_revisions_used") or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        flash("修正の回数は、0以上の整数で入力してください。", "error")
        return _back(order)
    _run(lambda: management.set_free_revisions_used(order, int(raw)), "修正の回数を保存しました。")
    return _back(order)


@bp.post("/<int:order_id>/deliver-article")
@admin_required
def deliver_article(order_id):
    order = _order_or_404(order_id)
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("納品するファイルを選択してください。", "error")
        return _back(order)
    if _run(lambda: management.deliver_article(order, file), "納品しました。"):
        notifications.notify_delivered(order)
    return _back(order)


@bp.post("/<int:order_id>/deliver-video")
@admin_required
def deliver_video(order_id):
    order = _order_or_404(order_id)
    if _run(lambda: management.deliver_video(order, request.form.get("delivery_url", "")), "納品しました。"):
        notifications.notify_delivered(order)
    return _back(order)


@bp.post("/<int:order_id>/mark-delivered")
@admin_required
def mark_delivered(order_id):
    order = _order_or_404(order_id)
    if _run(lambda: management.mark_delivered(order), "納品済みにしました。"):
        notifications.notify_delivered(order)
    return _back(order)


@bp.post("/<int:order_id>/refund")
@admin_required
def refund(order_id):
    order = _order_or_404(order_id)
    raw = (request.form.get("amount_yen") or "").strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        flash("返金額は、1円以上の整数（カンマなし）で入力してください。", "error")
        return _back(order)
    try:
        result = refunds.create_refund(
            order, current_user, int(raw), request.form.get("reason", ""), request.form.get("note")
        )
    except refunds.RefundError as e:
        flash(str(e), "error")
        return _back(order)
    flash(f"{result.amount_yen:,}円を返金しました。", "success")
    notifications.notify_refunded(order, result.amount_yen)
    return _back(order)


@bp.post("/<int:order_id>/resolve-refunds")
@admin_required
def resolve_refunds(order_id):
    """「処理中」のまま残った返金を、Stripe に問い合わせて確定する。"""
    order = _order_or_404(order_id)
    try:
        summary = refunds.resolve_pending(order)
    except payments.PaymentError:
        flash("Stripe に問い合わせられませんでした。しばらくしてから、もう一度お試しください。", "error")
        return _back(order)
    parts = [f"成功 {summary['succeeded']}件", f"失敗（返金額を戻しました）{summary['failed']}件",
             f"まだ処理中 {summary['still-pending']}件", f"作ってから間もないため、確認しなかったもの {summary['too-new']}件"]
    flash("処理中の返金を確認しました。" + "、".join(parts) + "。", "success")
    return _back(order)


@bp.get("/<int:order_id>/delivery")
@admin_required
def download_delivery(order_id):
    return send_delivery_file(_order_or_404(order_id))
