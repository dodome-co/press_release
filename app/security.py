"""セキュリティヘッダーと、外部に出すリンクの作り方。"""
from flask import current_app, request, url_for
from flask_login import current_user


def register_security_headers(app):
    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Frame-Options", "DENY")  # 他サイトへの埋め込みを禁止
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.is_secure or current_app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # ログイン後の画面や認証まわりの画面は、ブラウザに保存させない
        # ただし、見た目のファイル（CSS・ロゴ）は、みんな同じなので、ブラウザに保存させる
        if request.endpoint != "static" and (current_user.is_authenticated or request.blueprint == "auth"):
            response.headers["Cache-Control"] = "no-store"
        return response


def external_url(endpoint: str, **values) -> str:
    """メールなどに載せる絶対 URL を作る。

    BASE_URL が設定されていれば、リクエストの Host ヘッダー（偽装されうる）ではなく
    その値を使う。
    """
    base = current_app.config["BASE_URL"].rstrip("/")
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)
