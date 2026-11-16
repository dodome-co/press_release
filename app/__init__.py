import os

from dotenv import load_dotenv
from flask import Flask

from .config import CONFIGS
from .extensions import csrf, db, login_manager, migrate


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()
    config_name = config_name or os.environ.get("APP_ENV", "development")
    config_class = CONFIGS[config_name]
    if hasattr(config_class, "validate"):
        config_class.validate()

    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)  # SQLite でも変更を反映できるように
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  マイグレーションがモデルを見つけられるように読み込む
    from .cli import create_admin

    app.cli.add_command(create_admin)

    from .auth import bp as auth_bp
    from .main import bp as main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    return app


@login_manager.user_loader
def load_user(user_id):
    from .models import User

    return db.session.get(User, int(user_id))
