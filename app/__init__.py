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

    from .auth import bp as auth_bp
    from .main import bp as main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    return app


@login_manager.user_loader
def load_user(user_id):
    from .models import User

    return User.load_from_session_id(user_id)
