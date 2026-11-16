"""セキュリティ点検（認証まわり）で見つかった問題への対策のテスト。"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from app.config import ProductionConfig
from app.extensions import db
from app.models import User

from .conftest import login, make_user

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _forget_cached_user():
    """テストでは app_context が全リクエストで共有され、ログイン中のユーザーが g に残る。
    本番ではリクエストごとに作り直されるので、その状況に合わせて消す。"""
    from flask import g

    g.pop("_login_user", None)


def _reset_link(app, client, email="client@example.com", headers=None):
    client.post("/password/reset", data={"email": email}, headers=headers or {})
    return app.extensions["mail_outbox"][-1]["body"]


# ---- 1. 再設定リンクの Host 汚染 ----
def test_reset_link_uses_base_url_not_host_header(app, client, client_user):
    app.config["BASE_URL"] = "https://real.example.com"
    body = _reset_link(app, client, headers={"Host": "evil.example"})
    assert "https://real.example.com/password/reset/" in body
    assert "evil.example" not in body


def test_untrusted_host_is_rejected(app, client):
    app.config["TRUSTED_HOSTS"] = ["real.example.com"]
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "real.example.com"}).status_code == 200


# ---- 2. 再設定後に、ログイン中のセッションが残らない ----
def test_reset_invalidates_existing_sessions(app, client, client_user):
    other_device = app.test_client()
    assert login(other_device, "client@example.com").status_code == 302
    assert other_device.get("/mypage/").status_code == 200

    body = _reset_link(app, client)
    token = body.split("/password/reset/")[1].split()[0]
    client.post(f"/password/reset/{token}", data={"password": "newpassword99", "password_confirm": "newpassword99"})

    _forget_cached_user()
    res = other_device.get("/mypage/")
    assert res.status_code == 302 and "/login" in res.headers["Location"]  # 追い出される


def test_session_with_wrong_nonce_is_rejected(app, client, client_user):
    login(client, "client@example.com")
    with client.session_transaction() as sess:
        sess["_user_id"] = f"{client_user.id}:wrongnonce"
    _forget_cached_user()
    assert client.get("/mypage/").status_code == 302


def test_session_with_old_format_is_rejected(app, client, client_user):
    login(client, "client@example.com")
    with client.session_transaction() as sess:
        sess["_user_id"] = str(client_user.id)  # 世代のない古い形式
    _forget_cached_user()
    assert client.get("/mypage/").status_code == 302


# ---- 3. 本番でのメール ----
def test_mail_with_unimplemented_backend_raises(app):
    from app.mail import send_mail

    app.config["MAIL_BACKEND"] = "ses"
    with pytest.raises(NotImplementedError):
        send_mail("a@example.com", "件名", "本文")


def test_production_refuses_console_mail(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "real-secret")
    monkeypatch.setattr(ProductionConfig, "BASE_URL", "https://example.com")
    monkeypatch.setattr(ProductionConfig, "TRUSTED_HOSTS", ["example.com"])
    monkeypatch.setattr(ProductionConfig, "MAIL_BACKEND", "console")
    with pytest.raises(RuntimeError, match="MAIL_BACKEND"):
        create_app("production")


# ---- 4・5. 設定の読み込みと既定の環境 ----
def _run_python(code, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "SECRET_KEY", "DATABASE_URL")}
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, env=env, capture_output=True, text=True)


def test_dotenv_values_are_loaded(tmp_path):
    (tmp_path / ".env").write_text("APP_ENV=development\nSECRET_KEY=from-dotenv\nDATABASE_URL=sqlite:///x.db\n")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from app import create_app; a=create_app();"
        "print(a.config['SECRET_KEY'], a.config['SQLALCHEMY_DATABASE_URI'])" % str(PROJECT_ROOT)
    )
    res = _run_python(code, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "from-dotenv sqlite:///x.db" in res.stdout


def test_default_environment_is_production(tmp_path):
    code = "import sys; sys.path.insert(0, %r); from app import create_app; create_app()" % str(PROJECT_ROOT)
    res = _run_python(code, tmp_path)  # APP_ENV も .env もない
    assert res.returncode != 0
    assert "本番の設定が足りません" in res.stderr


# ---- 6. ログインのタイミング差 ----
def test_login_checks_password_even_for_unknown_email(client, client_user):
    with patch("app.auth.routes.check_password_hash", return_value=False) as checker:
        client.post("/login", data={"email": "nobody@example.com", "password": "whatever123"})
    assert checker.call_count == 1  # 未登録でも、パスワード検証と同じ処理を1回行う


# ---- 7. 入力サイズの上限 ----
def test_too_long_password_rejected_on_register(client):
    from .test_auth import REGISTER_DATA

    long_pw = "a" * 129
    res = client.post("/register", data={**REGISTER_DATA, "password": long_pw, "password_confirm": long_pw})
    assert res.status_code == 200
    assert User.query.count() == 0


def test_request_size_limit(app, client):
    assert app.config["MAX_CONTENT_LENGTH"] == 16 * 1024 * 1024
    res = client.post("/login", data=b"x" * (16 * 1024 * 1024 + 1), content_type="application/octet-stream")
    assert res.status_code == 413


# ---- 8. 管理者作成のパスワード ----
def test_create_admin_rejects_too_long_password(app):
    res = app.test_cli_runner().invoke(args=["create-admin", "--email", "boss@example.com", "--password", "a" * 129])
    assert res.exit_code != 0


# ---- 9. セキュリティヘッダー ----
def test_security_headers(client):
    res = client.get("/")
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "same-origin"


def test_auth_pages_are_not_cached(client, client_user):
    assert client.get("/password/reset").headers["Cache-Control"] == "no-store"
    login(client, "client@example.com")
    assert client.get("/mypage/").headers["Cache-Control"] == "no-store"


# ---- 10. 再設定トークンの同時使用 ----
def test_token_cannot_be_used_twice_even_if_checked_before_use(app, client, client_user):
    body = _reset_link(app, client)
    token = body.split("/password/reset/")[1].split()[0]
    data = {"password": "newpassword99", "password_confirm": "newpassword99"}

    # 1回目の確認（find_valid）が終わった後、使用済みにされた状況を再現する
    from app.models import PasswordResetToken, utcnow

    real_find = PasswordResetToken.find_valid

    def find_then_consume(tok):
        row = real_find(tok)
        if row is not None:
            PasswordResetToken.query.filter_by(id=row.id).update({"used_at": utcnow()})
            db.session.commit()
        return row

    with patch.object(PasswordResetToken, "find_valid", side_effect=find_then_consume):
        res = client.post(f"/password/reset/{token}", data=data)
    assert res.status_code == 302 and "/login" not in res.headers["Location"]
    assert login(client, "client@example.com", "newpassword99").status_code == 200  # パスワードは変わっていない


# ---- 11. 同時登録 ----
def test_concurrent_registration_with_same_email_does_not_500(app, client):
    from .test_auth import REGISTER_DATA

    make_user(email="new@example.com")
    # フォームの重複チェックをすり抜けて、DB の一意制約で失敗する状況を再現する
    with patch("app.auth.forms.RegisterForm.validate_email", lambda self, field: None):
        res = client.post("/register", data=REGISTER_DATA)
    assert res.status_code == 200
    assert User.query.count() == 1
