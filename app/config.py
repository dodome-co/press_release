"""設定。開発/本番の切り替えは環境変数 APP_ENV で行う。

APP_ENV が未設定のときは production（安全側）として扱う。開発では .env に
APP_ENV=development と書く（.env.example を参照）。
"""
import os


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///dev.sqlite3")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # セッション Cookie の安全設定
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # リクエスト全体の大きさの上限（写真・ロゴのアップロードに備えて 16MB）
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # アップロードしたファイルの保存先（開発は instance/uploads。本番は VM のディスク上の専用フォルダ）
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "")
    UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 画像1枚あたり 5MB
    # 書類の写真は横長だけ（横÷縦がこの値以上）。ロゴには適用しない
    PHOTO_MIN_ASPECT_RATIO = float(os.environ.get("PHOTO_MIN_ASPECT_RATIO", "1.2"))
    UPLOAD_MAX_PIXELS = 16_000_000  # 極端に大きな画像でメモリを使い切るのを防ぐ（縦×横。約4000×4000）

    # 下書きとして同時に持てる書類の数（提出するまでは無料枠に数えないため、増えすぎを防ぐ）
    DRAFT_LIMIT = int(os.environ.get("DRAFT_LIMIT", "3"))
    REJECT_REASON_MAX_LENGTH = 1000
    # タイトル（「【取材のお願い】」を除く）の長さ。全角換算。Word のタイトルが2行にならない長さ
    TITLE_MAX_WIDTH = int(os.environ.get("TITLE_MAX_WIDTH", "22"))

    # 書類の入力の長さ（仕様書 F3）
    SERVICE_DESCRIPTION_MAX_LENGTH = int(os.environ.get("SERVICE_DESCRIPTION_MAX_LENGTH", "1000"))

    # 認証（仕様書 F1・F1b）
    PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", "8"))
    PASSWORD_MAX_LENGTH = 128
    PASSWORD_RESET_EXPIRES_MINUTES = int(os.environ.get("PASSWORD_RESET_EXPIRES_MINUTES", "60"))

    # メールに載せるリンクの土台となる URL（例：https://example.com）。
    # 設定すると、リクエストの Host ヘッダーではなくこの値でリンクを作る。
    BASE_URL = os.environ.get("BASE_URL", "")
    # 許可するホスト名（カンマ区切り）。設定すると、それ以外の Host のリクエストは拒否される。
    TRUSTED_HOSTS = _csv("TRUSTED_HOSTS") or None
    # Nginx など、リバースプロキシの後ろで動かすとき 1 にする（実際の IP と http/https を読む）
    PROXY_FIX = os.environ.get("PROXY_FIX", "0") == "1"

    # メール送信方法。console=送らずログに出す（開発・テスト専用）。本番用は未実装（仕様書 9章）
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")

    # 仕様書「設定値」：コードに書き込まず設定で変えられるようにする
    FREE_RELEASE_LIMIT = int(os.environ.get("FREE_RELEASE_LIMIT", "1"))
    INPUT_MAX_LENGTH = int(os.environ.get("INPUT_MAX_LENGTH", "500"))
    APPROVAL_TARGET_BUSINESS_DAYS = int(os.environ.get("APPROVAL_TARGET_BUSINESS_DAYS", "2"))


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True  # HTTPS のときだけ Cookie を送る
    PROXY_FIX = os.environ.get("PROXY_FIX", "1") == "1"  # 本番は Nginx の後ろで動かす想定

    @classmethod
    def validate(cls):
        problems = []
        if cls.SECRET_KEY == "dev-only-change-me":
            problems.append("SECRET_KEY を設定してください")
        if not cls.BASE_URL.startswith("https://"):
            problems.append("BASE_URL を https:// から始まる形で設定してください")
        if not cls.TRUSTED_HOSTS:
            problems.append("TRUSTED_HOSTS に本番のドメインを設定してください")
        if cls.MAIL_BACKEND == "console":
            problems.append("本番用の MAIL_BACKEND を設定してください（console は開発専用）")
        if problems:
            raise RuntimeError("本番の設定が足りません：" + " / ".join(problems))


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
