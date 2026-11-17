"""管理者の画面：クライアントが入力した、自社サービスの紹介（仕様書 F17）。"""
from flask import Blueprint, abort, render_template, request
from sqlalchemy import or_

from .auth.decorators import admin_required
from .extensions import db
from .models import ServiceProfile, User

bp = Blueprint("admin_services", __name__, url_prefix="/admin/services")


@bp.get("")
@admin_required
def index():
    q = (request.args.get("q") or "").strip()
    query = ServiceProfile.query.join(User, ServiceProfile.user_id == User.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.company_name.ilike(like), User.email.ilike(like), ServiceProfile.body.ilike(like)))
    profiles = query.order_by(ServiceProfile.updated_at.desc()).limit(200).all()
    return render_template("admin/services.html", profiles=profiles, q=q)


@bp.get("/<int:user_id>")
@admin_required
def detail(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.service_profile is None:
        abort(404)
    return render_template("admin/service_detail.html", user=user, profile=user.service_profile)
