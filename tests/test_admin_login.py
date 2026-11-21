"""管理者の専用ログイン（/admin/login）。クライアントの入口（/login）とは、別にする。"""
import pytest

from app.auth.routes import ADMIN_HINT

from .conftest import make_user


def _post(client, path, email, password="password123", **query):
    return client.post(path, data={"email": email, "password": password}, query_string=query, follow_redirects=False)


def test_admin_login_page_exists_and_points_clients_elsewhere(client):
    res = client.get("/admin/login")
    body = res.get_data(as_text=True)
    assert res.status_code == 200 and "管理者ログイン" in body and "運営者（管理者）専用" in body
    assert 'href="/login"' in body  # クライアントの方は、こちらへ


def test_client_login_page_has_a_small_link_to_the_admin_login(client):
    body = client.get("/login").get_data(as_text=True)
    assert 'href="/admin/login"' in body and "管理者の方は、こちら" in body


def test_admin_can_log_in_and_lands_on_the_admin_page(client, admin_user):
    res = _post(client, "/admin/login", "admin@example.com")
    assert res.status_code == 302 and res.headers["Location"].endswith("/admin/")
    assert client.get("/admin/").status_code == 200


def test_client_account_cannot_log_in_at_the_admin_login(client, client_user):
    """パスワードが合っていても、クライアントは、管理者の入口から入れない。文言は「違います」とだけ（管理者かどうかを明かさない）。"""
    res = _post(client, "/admin/login", "client@example.com")
    assert res.status_code == 200
    assert "メールアドレスまたはパスワードが違います。" in res.get_data(as_text=True)
    assert client.get("/mypage/").status_code == 302  # ログインしていない


def test_error_message_is_the_same_for_wrong_password_and_for_a_client(client, client_user, admin_user):
    wrong = _post(client, "/admin/login", "admin@example.com", "ちがう").get_data(as_text=True)
    as_client = _post(client, "/admin/login", "client@example.com").get_data(as_text=True)
    unknown = _post(client, "/admin/login", "nobody@example.com").get_data(as_text=True)
    for body in (wrong, as_client, unknown):
        assert "メールアドレスまたはパスワードが違います。" in body and ADMIN_HINT not in body


def test_admin_account_is_sent_to_the_admin_login_from_the_client_login(client, admin_user):
    res = _post(client, "/login", "admin@example.com")
    assert res.status_code == 200 and ADMIN_HINT in res.get_data(as_text=True)
    assert client.get("/admin/").status_code == 302  # ログインしていない


def test_admin_hint_is_not_shown_for_a_wrong_password(client, admin_user):
    """管理者のメールアドレスだと、パスワードが合った人にだけ、案内する（管理者かどうかを、推測させない）。"""
    body = _post(client, "/login", "admin@example.com", "ちがう").get_data(as_text=True)
    assert ADMIN_HINT not in body and "メールアドレスまたはパスワードが違います。" in body


def test_client_still_logs_in_at_the_client_login(client, client_user):
    res = _post(client, "/login", "client@example.com")
    assert res.status_code == 302 and client.get("/mypage/").status_code == 200


def test_anonymous_admin_pages_go_to_the_admin_login(client):
    for path in ("/admin/", "/admin/releases", "/admin/orders"):
        res = client.get(path)
        assert res.status_code == 302 and res.headers["Location"].startswith("/admin/login?next=")


def test_anonymous_client_pages_still_go_to_the_client_login(client):
    res = client.get("/releases/")
    assert res.status_code == 302 and res.headers["Location"].startswith("/login?next=")


def test_admin_login_follows_a_safe_next_only(client, admin_user):
    res = _post(client, "/admin/login", "admin@example.com", next="/admin/releases")
    assert res.headers["Location"].endswith("/admin/releases")
    client.post("/logout")
    res = _post(client, "/admin/login", "admin@example.com", next="https://evil.example.com/")
    assert "evil.example.com" not in res.headers["Location"] and res.headers["Location"].endswith("/admin/")


def test_logged_in_users_are_moved_away_from_the_login_pages(app, admin_user, client_user):
    admin = app.test_client()
    _post(admin, "/admin/login", "admin@example.com")
    assert admin.get("/admin/login").headers["Location"].endswith("/admin/")
    assert admin.get("/login").headers["Location"].endswith("/admin/")
    client = app.test_client()
    _post(client, "/login", "client@example.com")
    assert client.get("/admin/login").status_code == 302 and client.get("/login").status_code == 302


def test_admin_login_has_the_rate_limit(app, client, admin_user):
    app.config.update(RATE_LIMIT_ENABLED=True, RATE_LOGIN_FAIL_MAX=3)
    for _ in range(3):
        assert _post(client, "/admin/login", "admin@example.com", "ちがう").status_code == 200
    res = _post(client, "/admin/login", "admin@example.com")  # 正しいパスワードでも、止まる
    assert res.status_code == 429


def test_client_password_guessing_on_the_admin_login_is_counted(app, client, client_user):
    app.config.update(RATE_LIMIT_ENABLED=True, RATE_LOGIN_FAIL_MAX=2)
    for _ in range(2):
        _post(client, "/admin/login", "client@example.com")  # 合っていても、失敗として数える
    assert _post(client, "/admin/login", "client@example.com").status_code == 429


def test_admin_login_requires_csrf_in_production_settings(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    res = client.post("/admin/login", data={"email": "a@example.com", "password": "x"})
    assert res.status_code in (200, 400) and "ログインしました" not in res.get_data(as_text=True)
