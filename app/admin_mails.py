"""管理者の画面：送信予定だったメールの一覧（デモ用。MAIL_BACKEND=database のとき）。"""
from flask import Blueprint, abort, current_app, render_template, request

from .auth.decorators import admin_required
from .extensions import db
from .models import MailLog

bp = Blueprint("admin_mails", __name__, url_prefix="/admin/mails")


@bp.get("")
@admin_required
def index():
    q = (request.args.get("q") or "").strip()
    query = MailLog.query
    if q:
        like = f"%{q}%"
        query = query.filter(MailLog.to_address.ilike(like) | MailLog.subject.ilike(like))
    mails = query.order_by(MailLog.id.desc()).limit(200).all()
    return render_template(
        "admin/mails.html", mails=mails, q=q, enabled=current_app.config["MAIL_BACKEND"] == "database"
    )


@bp.get("/<int:mail_id>")
@admin_required
def detail(mail_id):
    mail = db.session.get(MailLog, mail_id)
    if mail is None:
        abort(404)
    return render_template("admin/mail_detail.html", mail=mail)
