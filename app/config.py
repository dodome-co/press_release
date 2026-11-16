"""設定。開発/本番の切り替えは環境変数 APP_ENV で行う。"""
import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///dev.sqlite3")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # セッション Cookie の安全設定
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # 認証（仕様書 F1・F1b）
    PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "8"))
    PASSWORD_RESET_EXPIRES_MINUTES = int(os.environ.get("PASSWORD_RESET_EXPIRES_MINUTES", "60"))

    # 仕様書「設定値」：コードに書き込まず設定で変えられるようにする
    FREE_RELEASE_LIMIT = int(os.environ.get("FREE_RELEASE_LIMIT", "1"))
    INPUT_MAX_LENGTH = int(os.environ.get("INPUT_MAX_LENGTH", "500"))
    APPROVAL_TARGET_BUSINESS_DAYS = int(os.environ.get("APPROVAL_TARGET_BUSINESS_DAYS", "2"))


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True  # HTTPS のときだけ Cookie を送る

    @classmethod
    def validate(cls):
        if cls.SECRET_KEY == "dev-only-change-me":
            raise RuntimeError("本番では SECRET_KEY を必ず設定してください")


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
