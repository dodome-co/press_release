"""クライアントのマイページ：注文履歴と注文詳細、納品物のダウンロード。"""
from flask import Blueprint, abort, redirect, render_template, send_file, url_for
from flask_login import current_user, login_required

from . import uploads
from .auth.decorators import client_required
from .extensions import db
from .models import Order
from .orders import management

bp = Blueprint("mypage", __name__, url_prefix="/mypage")


def _own_order_or_404(order_id: int) -> Order:
    """自分の注文だけを返す。他人の注文は、存在の有無が分からないよう 404 にする。"""
    order = db.session.get(Order, order_id)
    if order is None or order.user_id != current_user.id:
        abort(404)
    return order


@bp.get("/")
@login_required
def index():
    if current_user.is_admin:
        return redirect(url_for("admin.index_page"))
    recent = (
        Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc(), Order.id.desc()).limit(5).all()
    )
    return render_template("mypage/index.html", recent_orders=recent)


@bp.get("/orders")
@client_required
def orders():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc(), Order.id.desc()).all()
    return render_template("mypage/orders.html", orders=orders)


@bp.get("/orders/<int:order_id>")
@client_required
def order_detail(order_id):
    order = _own_order_or_404(order_id)
    return render_template("mypage/order_detail.html", order=order, has_delivery=management.has_delivery(order))


@bp.get("/orders/<int:order_id>/delivery")
@client_required
def download_delivery(order_id):
    """納品された記事のファイルをダウンロードする（本人だけ）。"""
    order = _own_order_or_404(order_id)
    return send_delivery_file(order)


def send_delivery_file(order: Order):
    if not order.delivery_file_path:
        abort(404)
    mimetype = uploads.document_mimetype(order.delivery_file_path)
    if mimetype is None:
        abort(404)
    try:
        path = uploads.resolve_path(order.delivery_file_path)
    except uploads.UploadError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(
        path,
        as_attachment=True,  # ブラウザで直接開かせず、ダウンロードさせる
        download_name=order.delivery_filename or f"order_{order.id}",
        mimetype=mimetype,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response
