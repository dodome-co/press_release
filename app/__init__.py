import os

from dotenv import load_dotenv
from flask import Flask

from .config import CONFIGS
from .extensions import csrf, db, migrate


def create_app(config_name: str | None = None) -> Flask:
    load_dotenv()
    config_name = config_name or os.environ.get("APP_ENV", "development")
    config_class = CONFIGS[config_name]
    if hasattr(config_class, "validate"):
        config_class.validate()

    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    from .main import bp as main_bp

    app.register_blueprint(main_bp)
    return app
