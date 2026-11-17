import os

from dotenv import load_dotenv

# 設定（config.py）が環境変数を読む前に .env を読み込む
load_dotenv()

from flask import Flask  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from .config import CONFIGS  # noqa: E402
from .extensions import csrf, db, login_manager, migrate  # noqa: E402


def create_app(config_name: str | None = None) -> Flask:
    config_name = config_name or os.environ.get("APP_ENV", "production")
    if config_name not in CONFIGS:
        raise RuntimeError(f"APP_ENV が正しくありません：{config_name}")
    config_class = CONFIGS[config_name]
    if hasattr(config_class, "validate"):
        config_class.validate()

    from .services_catalog import validate_price_overrides

    validate_price_overrides()

    app = Flask(__name__)
    app.config.from_object(config_class)

    if app.config["PROXY_FIX"]:
        # 実際のクライアント IP と http/https だけを信用する。Host は信用しない（偽装対策）
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)  # SQLite でも変更を反映できるように
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  マイグレーションがモデルを見つけられるように読み込む
    from .cli import create_admin
    from .security import register_security_headers

    app.cli.add_command(create_admin)
    register_security_headers(app)

    from .admin import bp as admin_bp
    from .auth import bp as auth_bp
    from .files import bp as files_bp
    from .guide import bp as guide_bp
    from .accounts import bp as admin_users_bp
    from .admin_orders import bp as admin_orders_bp
    from .admin_services import bp as admin_services_bp
    from .main import bp as main_bp
    from .mypage import bp as mypage_bp
    from .orders import bp as orders_bp
    from .profile import bp as profile_bp
    from .releases import bp as releases_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(releases_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(guide_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(admin_orders_bp)
    app.register_blueprint(admin_services_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(mypage_bp)

    STATUS_LABELS = {"draft": "下書き", "pending": "承認待ち", "approved": "承認済み", "rejected": "差し戻し"}
    ORDER_STATUS_LABELS = {"unpaid": "お支払い前", "paid": "お支払い済み", "canceled": "取り消し",
                           "refunded": "返金済み", "partially_refunded": "一部返金"}
    PROGRESS_LABELS = {"received": "受付", "in_progress": "着手", "delivered": "納品済み"}
    app.jinja_env.filters["order_status_label"] = lambda v: ORDER_STATUS_LABELS.get(v, v or "")
    app.jinja_env.filters["progress_label"] = lambda v: PROGRESS_LABELS.get(v, v or "—")
    app.jinja_env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
    app.jinja_env.filters["yen"] = lambda n: f"{n:,}円"
    app.jinja_env.filters["status_label"] = lambda status: STATUS_LABELS.get(status, status)
    return app


@login_manager.user_loader
def load_user(user_id):
    from .models import User

    return User.load_from_session_id(user_id)
