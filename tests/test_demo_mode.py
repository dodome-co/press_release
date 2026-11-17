import pytest

from app import create_app
from app.config import DemoConfig, ProductionConfig
from app.extensions import db
from app.mail import send_mail
from app.models import MailLog

from .conftest import login


def _demo_settings(monkeypatch, **overrides):
    values = {"SECRET_KEY": "x" * 20, "BASE_URL": "http://203.0.113.10", "TRUSTED_HOSTS": ["203.0.113.10"],
              "STRIPE_SECRET_KEY": "sk_test_abc", "STRIPE_WEBHOOK_SECRET": "whsec_x", "MAIL_BACKEND": "database"}
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(DemoConfig, name, value)


# ---- デモ用の設定 ----
def test_demo_config_starts_with_test_key_over_http(monkeypatch):
    _demo_settings(monkeypatch)
    app = create_app("demo")
    assert app.config["DEMO_MODE"] is True and app.config["DEBUG"] is False
    assert app.config["SESSION_COOKIE_SECURE"] is False  # HTTP で公開するため
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True and app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["PROXY_FIX"] is True


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "", "pk_test_abc"])
def test_demo_refuses_anything_but_a_test_secret_key(monkeypatch, key):
    _demo_settings(monkeypatch, STRIPE_SECRET_KEY=key)
    with pytest.raises(RuntimeError, match="テスト用のキー"):
        create_app("demo")


def test_demo_accepts_restricted_test_key(monkeypatch):
    _demo_settings(monkeypatch, STRIPE_SECRET_KEY="rk_test_abc")
    create_app("demo")


@pytest.mark.parametrize("name,value,message", [
    ("SECRET_KEY", "dev-only-change-me", "SECRET_KEY"),
    ("BASE_URL", "", "BASE_URL"),
    ("BASE_URL", "ftp://x", "BASE_URL"),
    ("TRUSTED_HOSTS", None, "TRUSTED_HOSTS"),
    ("STRIPE_WEBHOOK_SECRET", "", "STRIPE_WEBHOOK_SECRET"),
    ("MAIL_BACKEND", "smtp", "MAIL_BACKEND"),
])
def test_demo_requires_safe_settings(monkeypatch, name, value, message):
    _demo_settings(monkeypatch, **{name: value})
    with pytest.raises(RuntimeError, match=message):
        create_app("demo")


def test_demo_allows_both_console_and_database_mail(monkeypatch):
    for backend in ("console", "database"):
        _demo_settings(monkeypatch, MAIL_BACKEND=backend)
        create_app("demo")


def test_demo_http_base_url_is_used_in_links(app, client, client_user, monkeypatch):
    app.config["BASE_URL"] = "http://203.0.113.10"
    client.post("/password/reset", data={"email": "client@example.com"})
    assert "http://203.0.113.10/password/reset/" in app.extensions["mail_outbox"][-1]["body"]


def test_production_refuses_database_mail(monkeypatch):
    for name, value in (("SECRET_KEY", "x"), ("BASE_URL", "https://e.com"), ("TRUSTED_HOSTS", ["e.com"]),
                        ("STRIPE_SECRET_KEY", "sk_live_x"), ("STRIPE_WEBHOOK_SECRET", "whsec_x"),
                        ("MAIL_BACKEND", "database")):
        monkeypatch.setattr(ProductionConfig, name, value)
    with pytest.raises(RuntimeError, match="MAIL_BACKEND"):
        create_app("production")


# ---- デモ環境の表示 ----
def test_banner_is_hidden_outside_demo(client):
    assert "ビジネスプランの発表用のデモ" not in client.get("/").get_data(as_text=True)


def test_banner_is_shown_on_every_kind_of_page_in_demo(app, client, client_user):
    app.config["DEMO_MODE"] = True
    for path in ("/", "/login", "/register"):
        body = client.get(path).get_data(as_text=True)
        assert "ビジネスプランの発表用のデモです" in body and "個人情報は入力しないでください" in body, path
        assert "4242 4242 4242 4242" in body
    login(client, "client@example.com")
    assert "ビジネスプランの発表用のデモです" in client.get("/mypage/").get_data(as_text=True)


# ---- 送信予定のメール（データベースに記録） ----
@pytest.fixture()
def database_mail(app):
    app.config["MAIL_BACKEND"] = "database"


def test_mail_is_recorded_instead_of_sent(app, database_mail):
    send_mail("a@example.com", "件名", "本文のリンク http://x/reset/abc")
    row = MailLog.query.one()
    assert (row.to_address, row.subject) == ("a@example.com", "件名") and "reset/abc" in row.body
    assert app.extensions.get("mail_outbox", []) == []  # メモリには貯めない


def test_log_output_does_not_contain_the_body(app, database_mail, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        send_mail("a@example.com", "件名", "秘密の再設定リンク https://x/reset/SECRET")
    assert "SECRET" not in caplog.text and "件名" in caplog.text


def test_long_fields_are_cut_to_fit(database_mail):
    send_mail("a" * 300 + "@example.com", "あ" * 300, "本文")
    row = MailLog.query.one()
    assert len(row.to_address) == 255 and len(row.subject) == 255


def test_real_flows_record_mails(app, client, client_user, database_mail):
    client.post("/password/reset", data={"email": "client@example.com"})
    row = MailLog.query.one()
    assert row.to_address == "client@example.com" and "/password/reset/" in row.body


def test_unimplemented_backends_still_raise(app):
    app.config["MAIL_BACKEND"] = "smtp"
    with pytest.raises(NotImplementedError):
        send_mail("a@example.com", "件名", "本文")


# ---- 管理者の画面 ----
@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


def test_mail_pages_need_admin(app, client, client_user, database_mail):
    send_mail("a@example.com", "件名", "本文")
    assert client.get("/admin/mails").status_code == 302
    login(client, "client@example.com")
    assert client.get("/admin/mails").status_code == 403
    assert client.get("/admin/mails/1").status_code == 403


def test_admin_sees_list_and_detail(admin_client, database_mail):
    send_mail("alice@example.com", "【プレスリリースサポート】書類が承認されました", "承認されました。\nhttp://x/releases/1")
    send_mail("bob@example.com", "別の件名", "別の本文")
    body = admin_client.get("/admin/mails").get_data(as_text=True)
    assert "alice@example.com" in body and "書類が承認されました" in body and "bob@example.com" in body
    assert body.index("bob@example.com") < body.index("alice@example.com")  # 新しい順
    detail = admin_client.get("/admin/mails/1").get_data(as_text=True)
    assert "承認されました。" in detail and "http://x/releases/1" in detail


def test_admin_search(admin_client, database_mail):
    send_mail("alice@example.com", "承認", "a")
    send_mail("bob@example.com", "返金", "b")
    body = admin_client.get("/admin/mails?q=返金").get_data(as_text=True)
    assert "bob@example.com" in body and "alice@example.com" not in body
    assert admin_client.get("/admin/mails?q=%25_").status_code == 200


def test_mail_body_is_escaped(admin_client, database_mail):
    send_mail("a@example.com", "<b>件名</b>", "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in admin_client.get("/admin/mails/1").get_data(as_text=True)
    assert "<b>件名</b>" not in admin_client.get("/admin/mails").get_data(as_text=True)


def test_unknown_mail_is_404(admin_client):
    assert admin_client.get("/admin/mails/999").status_code == 404


def test_page_warns_when_mails_are_not_recorded(admin_client, app):
    assert "メールを記録していません" in admin_client.get("/admin/mails").get_data(as_text=True)


def test_admin_menu_shows_link_only_with_database_mail(admin_client, app):
    assert "/admin/mails" not in admin_client.get("/admin/").get_data(as_text=True)
    app.config["MAIL_BACKEND"] = "database"
    assert "/admin/mails" in admin_client.get("/admin/").get_data(as_text=True)
