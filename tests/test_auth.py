from app.extensions import db
from app.models import PasswordResetToken, User, utcnow
from datetime import timedelta

from .conftest import login, make_user

REGISTER_DATA = {
    "email": "New@Example.com",
    "password": "password123",
    "password_confirm": "password123",
    "company_name": "株式会社新規",
    "postal_code": "１２３－４５６７",
    "address": "大阪府大阪市1-2-3",
    "contact_name": "佐藤花子",
    "phone": "０６-1234-5678",
    "mobile_phone": "",
}


# ---- 会員登録 ----
def test_register_success_creates_client(client, app):
    res = client.post("/register", data=REGISTER_DATA)
    assert res.status_code == 302
    user = User.query.filter_by(email="new@example.com").one()  # 小文字にそろう
    assert user.role == "client"
    assert user.postal_code == "123-4567"  # 全角→半角、ハイフン付きにそろう
    assert user.phone == "06-1234-5678"
    assert user.mobile_phone is None
    assert user.password_hash != "password123"  # 元のパスワードは保存しない


def test_register_logs_in_automatically(client):
    client.post("/register", data=REGISTER_DATA)
    assert client.get("/mypage/").status_code == 200


def test_register_cannot_become_admin(client):
    client.post("/register", data={**REGISTER_DATA, "role": "admin"})
    assert User.query.filter_by(email="new@example.com").one().role == "client"


def test_register_duplicate_email_rejected(client, client_user):
    res = client.post("/register", data={**REGISTER_DATA, "email": "CLIENT@example.com"})
    assert res.status_code == 200
    assert User.query.count() == 1


def test_register_short_password_rejected(client):
    data = {**REGISTER_DATA, "password": "short", "password_confirm": "short"}
    assert client.post("/register", data=data).status_code == 200
    assert User.query.count() == 0


def test_register_password_mismatch_rejected(client):
    data = {**REGISTER_DATA, "password_confirm": "different123"}
    assert client.post("/register", data=data).status_code == 200
    assert User.query.count() == 0


def test_register_bad_postal_code_rejected(client):
    assert client.post("/register", data={**REGISTER_DATA, "postal_code": "12345"}).status_code == 200
    assert User.query.count() == 0


def test_register_missing_required_rejected(client):
    data = {k: v for k, v in REGISTER_DATA.items() if k != "company_name"}
    assert client.post("/register", data=data).status_code == 200
    assert User.query.count() == 0


# ---- ログイン・ログアウト ----
def test_login_success_and_logout(client, client_user):
    assert login(client, "client@example.com").status_code == 302
    assert client.get("/mypage/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/mypage/").status_code == 302  # ログアウト後は入れない


def test_login_email_is_case_insensitive(client, client_user):
    assert login(client, "CLIENT@Example.com").status_code == 302
    assert client.get("/mypage/").status_code == 200


def test_login_wrong_password_same_message(client, client_user):
    wrong_pw = client.post("/login", data={"email": "client@example.com", "password": "x" * 10})
    unknown = client.post("/login", data={"email": "nobody@example.com", "password": "x" * 10})
    msg = "メールアドレスまたはパスワードが違います。"
    assert msg in wrong_pw.get_data(as_text=True)
    assert msg in unknown.get_data(as_text=True)  # 登録の有無が分からない


def test_login_redirects_to_safe_next(client, client_user):
    res = client.post("/login?next=/mypage/", data={"email": "client@example.com", "password": "password123"})
    assert res.headers["Location"].endswith("/mypage/")


def test_login_rejects_external_next(client, client_user):
    res = client.post(
        "/login?next=https://evil.example.com/", data={"email": "client@example.com", "password": "password123"}
    )
    assert "evil.example.com" not in res.headers["Location"]


def test_logout_requires_post(client, client_user):
    login(client, "client@example.com")
    assert client.get("/logout").status_code == 405


# ---- アクセス制御 ----
def test_mypage_requires_login(client):
    res = client.get("/mypage/")
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_admin_page_requires_login(client):
    res = client.get("/admin/")
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_client_cannot_open_admin_page(client, client_user):
    login(client, "client@example.com")
    assert client.get("/admin/").status_code == 403


def test_admin_can_open_admin_page(client, admin_user):
    login(client, "admin@example.com")
    assert client.get("/admin/").status_code == 200


# ---- パスワード再設定 ----
def _request_reset(client, app, email):
    res = client.post("/password/reset", data={"email": email})
    outbox = app.extensions.get("mail_outbox", [])
    return res, outbox


def _token_from_mail(outbox):
    body = outbox[-1]["body"]
    return body.split("/password/reset/")[1].split()[0]


def test_reset_request_sends_mail_for_registered_email(client, app, client_user):
    res, outbox = _request_reset(client, app, "client@example.com")
    assert res.status_code == 200
    assert len(outbox) == 1 and outbox[0]["to"] == "client@example.com"


def test_reset_request_same_response_for_unknown_email(client, app, client_user):
    known, _ = _request_reset(client, app, "client@example.com")
    unknown, outbox = _request_reset(client, app, "nobody@example.com")
    assert known.get_data() == unknown.get_data()  # 登録の有無が分からない
    assert len(outbox) == 1  # 未登録には送らない（先の1通だけ）


def test_reset_token_stored_as_hash(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    row = PasswordResetToken.query.one()
    assert row.token_hash != token and len(row.token_hash) == 64


def test_reset_password_flow(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    res = client.post(
        f"/password/reset/{token}", data={"password": "newpassword99", "password_confirm": "newpassword99"}
    )
    assert res.status_code == 302
    assert login(client, "client@example.com", "newpassword99").status_code == 302
    assert client.get("/mypage/").status_code == 200


def test_old_password_stops_working_after_reset(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    client.post(f"/password/reset/{token}", data={"password": "newpassword99", "password_confirm": "newpassword99"})
    res = client.post("/login", data={"email": "client@example.com", "password": "password123"})
    assert res.status_code == 200  # ログインできず、画面に戻る


def test_reset_token_is_single_use(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    data = {"password": "newpassword99", "password_confirm": "newpassword99"}
    assert client.post(f"/password/reset/{token}", data=data).status_code == 302
    assert client.get(f"/password/reset/{token}").status_code == 302  # 使用済みは無効
    assert client.post(f"/password/reset/{token}", data=data).status_code == 302


def test_reset_token_expired(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    row = PasswordResetToken.query.one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    res = client.get(f"/password/reset/{token}")
    assert res.status_code == 302 and "/password/reset" in res.headers["Location"]


def test_reset_token_invalid(client):
    assert client.get("/password/reset/not-a-real-token").status_code == 302


def test_new_request_invalidates_old_token(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    first = _token_from_mail(outbox)
    _request_reset(client, app, "client@example.com")
    assert client.get(f"/password/reset/{first}").status_code == 302  # 古いリンクは無効


def test_reset_short_password_rejected(client, app, client_user):
    _, outbox = _request_reset(client, app, "client@example.com")
    token = _token_from_mail(outbox)
    res = client.post(f"/password/reset/{token}", data={"password": "short", "password_confirm": "short"})
    assert res.status_code == 200
    assert login(client, "client@example.com").status_code == 302  # 元のパスワードのまま


# ---- create-admin ----
def test_create_admin_command(app):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin", "--email", "Boss@Example.com", "--password", "password123"])
    assert res.exit_code == 0
    user = User.query.filter_by(email="boss@example.com").one()
    assert user.role == "admin" and user.check_password("password123")


def test_create_admin_rejects_duplicate(app):
    make_user(email="boss@example.com")
    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin", "--email", "boss@example.com", "--password", "password123"])
    assert res.exit_code != 0


def test_create_admin_rejects_short_password(app):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["create-admin", "--email", "boss@example.com", "--password", "short"])
    assert res.exit_code != 0
    assert User.query.count() == 0
