from flask import current_app, render_template

from ..services_catalog import all_services, support_contact
from . import bp


@bp.get("/")
def index():
    """トップページ。ログインしていなくても見られる。"""
    return render_template("index.html", services=all_services())


@bp.get("/about")
def about():
    """運営者の紹介（About Us）。ログインしていなくても見られる。"""
    config = current_app.config
    return render_template(
        "about.html",
        operator_name=config["OPERATOR_NAME"],
        address=config["COMPANY_ADDRESS"] or "（準備中）",
        email=support_contact(),
    )
