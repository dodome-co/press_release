from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "ログインしてください。"
login_manager.login_message_category = "error"


@login_manager.unauthorized_handler
def _unauthorized():
    """ログインしていない人が、ログインの要る画面を開いたとき。管理者の画面なら、管理者の専用ログインへ。"""
    from flask import flash, redirect, request, url_for

    flash(login_manager.login_message, login_manager.login_message_category)
    target = "auth.admin_login" if request.path.startswith("/admin") else "auth.login"
    next_url = request.full_path.rstrip("?")
    return redirect(url_for(target, next=next_url))
