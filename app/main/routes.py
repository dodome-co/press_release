from flask import render_template
from flask_login import login_required

from ..auth.decorators import admin_required
from . import bp


@bp.get("/")
def index():
    return render_template("index.html")


# 以下は、アクセス制御（F2）の確認用の仮の画面。後の段階で本物に置き換える。
@bp.get("/mypage/")
@login_required
def mypage():
    return render_template("placeholder.html", title="マイページ")


@bp.get("/admin/")
@admin_required
def admin_index():
    return render_template("admin/index.html")
