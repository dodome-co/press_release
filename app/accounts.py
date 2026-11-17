"""退会処理（管理者）：クライアントのアカウントと個人情報を削除する。

クライアントからの問い合わせを受けて、管理者が行う（画面から本人が退会する機能は作らない）。
削除するもの：会員情報、書類（Word・写真）、ロゴ、納品ファイル、パスワード再設定の記録。
残すもの：注文・支払いの記録のうち、個人情報を含まないもの（金額・日付・サービス名・状態など）。
"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_

from . import uploads
from .auth.decorators import admin_required
from .extensions import db
from .models import (
    ORDER_PAID,
    ORDER_PARTIALLY_REFUNDED,
    ORDER_UNPAID,
    PROGRESS_DELIVERED,
    REFUND_PENDING,
    RELEASE_PENDING,
    ROLE_CLIENT,
    Order,
    OrderRefund,
    Release,
    User,
)
from .orders import management
from .releases import service as release_service
from .orders import service as order_service

bp = Blueprint("admin_users", __name__, url_prefix="/admin/users")


class AccountError(Exception):
    """管理者に見せてよいメッセージを持つエラー。"""


def unused_purchased_slots(user: User) -> int:
    """購入したのに、まだ使っていない「リリース作成の追加」の枠の数。"""
    purchased = release_service.count_purchased_slots(user.id)
    used_beyond_free = max(0, release_service.count_first_submitted(user.id) - release_service.free_limit())
    return max(0, purchased - used_beyond_free)


def deletion_blockers(user: User) -> list[str]:
    """削除できない理由の一覧（空なら、削除できる）。"""
    blockers = []
    if user.is_admin:
        blockers.append("管理者のアカウントは、この画面からは削除できません。")
    pending = Release.query.filter_by(user_id=user.id, status=RELEASE_PENDING).count()
    if pending:
        blockers.append(f"承認待ちの書類が{pending}件あります。承認か差し戻しをしてください。")
    unused = unused_purchased_slots(user)
    if unused:
        blockers.append(
            f"使っていない、購入済みの書類の枠が{unused}つあります。先に返金してください（注文の画面から全額を返金できます）。"
        )
    for order in Order.query.filter_by(user_id=user.id).all():
        if order.disputed_at is not None:
            blockers.append(
                f"カード会社への異議申し立て中の注文があります（注文番号 {order.id}）。"
                "反証のための記録が必要になることがあるため、解決するまで、削除できません。"
            )
        if order.status in (ORDER_PAID, ORDER_PARTIALLY_REFUNDED) and management.has_delivery(order) \
                and order.progress != PROGRESS_DELIVERED:
            blockers.append(f"納品前の注文があります（注文番号 {order.id}：{order.service_name}）。納品するか、返金してください。")
        if OrderRefund.query.filter_by(order_id=order.id, status=REFUND_PENDING).count():
            blockers.append(f"返金の処理中の注文があります（注文番号 {order.id}）。処理が終わるまでお待ちください。")
    return blockers


def delete_account(user: User, admin: User, confirm_email: str) -> None:
    if user.id == admin.id:
        raise AccountError("自分自身のアカウントは、削除できません。")
    if User.normalize_email(confirm_email or "") != user.email:
        raise AccountError("確認のメールアドレスが、削除する会員のものと一致しません。")
    blockers = deletion_blockers(user)
    if blockers:
        raise AccountError("削除できません。" + " ".join(blockers))

    user_id = user.id
    # 1. お支払い前の注文は取り消す（支払い画面も無効にして、あとから支払われないようにする）
    for order in Order.query.filter_by(user_id=user_id, status=ORDER_UNPAID).all():
        order_service.cancel_unpaid(order)

    # 2. 消すファイルの場所を、先に集めておく
    release_ids = [r.id for r in Release.query.filter_by(user_id=user_id).all()]
    orders = Order.query.filter_by(user_id=user_id).all()
    order_ids = [o.id for o in orders]
    directories = [f"releases/{rid}" for rid in release_ids] + [f"users/{user_id}"] + [f"orders/{oid}" for oid in order_ids]
    files = [user.logo_path] + [o.delivery_file_path for o in orders]

    # 3. 注文から、個人情報を消す（金額・日付・サービス名・状態などは残す）。ユーザーとの関連は、削除時に外れる
    for order in orders:
        order.materials_url = None
        order.delivery_url = None
        order.delivery_file_path = None
        order.delivery_filename = None
        order.refund_reason = None
        for refund in order.refunds:
            refund.note = None
    db.session.flush()

    # 4. ユーザーを削除する（書類・写真・パスワード再設定の記録も、一緒に消える）
    db.session.delete(user)
    db.session.commit()

    # 5. ファイルを消す（DB の削除が済んでから。失敗しても、削除自体は取り消さない）
    for path in files:
        uploads.delete_file(path)
    for directory in directories:
        if not uploads.delete_directory(directory):
            current_app.logger.warning("退会処理のあと、フォルダを削除できませんでした（%s）。手作業で削除してください", directory)
    current_app.logger.info("退会処理をしました（会員ID=%s, 実行した管理者ID=%s, 注文%s件を匿名化）", user_id, admin.id, len(orders))


@bp.get("")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    query = User.query.filter_by(role=ROLE_CLIENT)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.email.ilike(like), User.company_name.ilike(like), User.contact_name.ilike(like)))
    return render_template("admin/users.html", users=query.order_by(User.created_at.desc()).limit(200).all(), q=q)


@bp.get("/<int:user_id>")
@admin_required
def detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return render_template(
        "admin/user_detail.html",
        user=user,
        blockers=deletion_blockers(user),
        release_count=Release.query.filter_by(user_id=user.id).count(),
        orders=Order.query.filter_by(user_id=user.id).order_by(Order.id.desc()).all(),
    )


@bp.post("/<int:user_id>/delete")
@admin_required
def delete(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    try:
        delete_account(user, current_user, request.form.get("confirm_email", ""))
    except AccountError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("admin_users.detail", user_id=user_id))
    flash("退会処理をしました。会員の情報とファイルを削除し、注文は個人情報を消して残しました。", "success")
    return redirect(url_for("admin_users.users"))
