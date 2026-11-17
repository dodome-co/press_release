import pytest

from app.extensions import db
from app.models import Release, ServiceProfile, User, utcnow

from .conftest import login, make_user
from .test_releases import FULL


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client_user


def _save(client, body):
    return client.post("/mypage/service", data={"body": body})


# ================= クライアント（F10） =================
def test_page_needs_login_and_client_role(client, admin_user):
    assert client.get("/mypage/service").status_code == 302
    login(client, "admin@example.com")
    assert client.get("/mypage/service").status_code == 403


def test_empty_form_at_first(client, logged_in):
    res = client.get("/mypage/service")
    assert res.status_code == 200 and "自社サービスの紹介" in res.get_data(as_text=True)
    assert ServiceProfile.query.count() == 0


def test_save_and_edit_repeatedly(client, logged_in):
    assert _save(client, "最初の紹介です。").status_code == 302
    profile = ServiceProfile.query.one()
    assert (profile.user_id, profile.body, profile.input_method) == (logged_in.id, "最初の紹介です。", "text")
    first_created = profile.created_at
    _save(client, "書き直しました。")
    assert ServiceProfile.query.count() == 1  # 1人に1つ
    db.session.refresh(profile)
    assert profile.body == "書き直しました。" and profile.created_at == first_created
    assert "書き直しました。" in client.get("/mypage/service").get_data(as_text=True)


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\x01\x02"])
def test_saving_empty_text_removes_the_profile(client, logged_in, empty):
    _save(client, "書いた内容")
    assert ServiceProfile.query.count() == 1
    _save(client, empty)
    assert ServiceProfile.query.count() == 0


def test_empty_save_with_no_profile_creates_nothing(client, logged_in):
    assert _save(client, "").status_code == 302
    assert ServiceProfile.query.count() == 0


def test_length_limit(client, logged_in):
    assert _save(client, "あ" * 2000).status_code == 302
    res = _save(client, "あ" * 2001)
    assert res.status_code == 200 and "2000文字以内" in res.get_data(as_text=True)
    assert len(ServiceProfile.query.one().body) == 2000  # 長すぎる入力では、変わらない


def test_limit_is_configurable(client, logged_in, app):
    app.config["SERVICE_PROFILE_MAX_LENGTH"] = 10
    assert _save(client, "あ" * 11).status_code == 200
    assert _save(client, "あ" * 10).status_code == 302


def test_control_characters_are_removed_and_newlines_kept(client, logged_in):
    _save(client, "一行目\x01\r\n二行目\x00")
    assert ServiceProfile.query.one().body == "一行目\n二行目"


def test_html_is_escaped_on_the_form(client, logged_in):
    _save(client, "<script>alert(1)</script>")
    body = client.get("/mypage/service").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_each_client_only_sees_and_changes_their_own(app, client, logged_in):
    other = make_user(email="other@example.com")
    db.session.add(ServiceProfile(user_id=other.id, body="他社の紹介"))
    db.session.commit()
    assert "他社の紹介" not in client.get("/mypage/service").get_data(as_text=True)
    _save(client, "自社の紹介")
    assert ServiceProfile.query.filter_by(user_id=other.id).one().body == "他社の紹介"


def test_mypage_links_to_it(client, logged_in):
    assert "/mypage/service" in client.get("/mypage/").get_data(as_text=True)


def test_requires_csrf_token(app, client, logged_in):
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/mypage/service", data={"body": "x"}).status_code == 400
    assert ServiceProfile.query.count() == 0


# ================= 管理者（F17） =================
def test_admin_pages_need_admin(client, client_user):
    db.session.add(ServiceProfile(user_id=client_user.id, body="紹介"))
    db.session.commit()
    assert client.get("/admin/services").status_code == 302
    login(client, "client@example.com")
    assert client.get("/admin/services").status_code == 403
    assert client.get(f"/admin/services/{client_user.id}").status_code == 403


def test_admin_list_and_detail(admin_client, client_user):
    db.session.add(ServiceProfile(user_id=client_user.id, body="私たちは、盛岡の和菓子店です。\n二行目の説明です。"))
    db.session.commit()
    body = admin_client.get("/admin/services").get_data(as_text=True)
    assert "株式会社テスト" in body and "client@example.com" in body and "私たちは、盛岡の和菓子店です。" in body
    detail = admin_client.get(f"/admin/services/{client_user.id}").get_data(as_text=True)
    assert "二行目の説明です。" in detail and "white-space:pre-wrap" in detail


def test_admin_list_shortens_long_text(admin_client, client_user):
    db.session.add(ServiceProfile(user_id=client_user.id, body="あ" * 100))
    db.session.commit()
    body = admin_client.get("/admin/services").get_data(as_text=True)
    assert "あ" * 60 + "…" in body and "あ" * 61 not in body


def test_admin_search(admin_client, client_user):
    other = make_user(email="other@example.com", company_name="別の会社")
    db.session.add_all([ServiceProfile(user_id=client_user.id, body="和菓子を作っています"),
                        ServiceProfile(user_id=other.id, body="ソフトウェアを作っています")])
    db.session.commit()
    body = admin_client.get("/admin/services?q=和菓子").get_data(as_text=True)
    assert "株式会社テスト" in body and "別の会社" not in body
    body = admin_client.get("/admin/services?q=別の会社").get_data(as_text=True)
    assert "別の会社" in body and "株式会社テスト" not in body
    assert admin_client.get("/admin/services?q=%25_").status_code == 200  # 記号が入っていても壊れない


def test_admin_list_orders_newest_first(admin_client, client_user):
    from datetime import timedelta

    other = make_user(email="other@example.com", company_name="新しい会社")
    old = ServiceProfile(user_id=client_user.id, body="古い", updated_at=utcnow() - timedelta(days=3))
    new = ServiceProfile(user_id=other.id, body="新しい", updated_at=utcnow())
    db.session.add_all([old, new])
    db.session.commit()
    body = admin_client.get("/admin/services").get_data(as_text=True)
    assert body.index("新しい会社") < body.index("株式会社テスト")


def test_admin_detail_escapes_html(admin_client, client_user):
    db.session.add(ServiceProfile(user_id=client_user.id, body="<img src=x onerror=alert(1)>"))
    db.session.commit()
    body = admin_client.get(f"/admin/services/{client_user.id}").get_data(as_text=True)
    assert "<img src=x" not in body and "&lt;img" in body


def test_admin_detail_404_without_profile_or_user(admin_client, client_user):
    assert admin_client.get(f"/admin/services/{client_user.id}").status_code == 404
    assert admin_client.get("/admin/services/9999").status_code == 404


def test_member_detail_page_shows_the_profile(admin_client, client_user):
    db.session.add(ServiceProfile(user_id=client_user.id, body="会員の詳細に出る紹介"))
    db.session.commit()
    assert "会員の詳細に出る紹介" in admin_client.get(f"/admin/users/{client_user.id}").get_data(as_text=True)


def test_member_detail_page_says_when_not_entered(admin_client, client_user):
    assert "まだ入力されていません" in admin_client.get(f"/admin/users/{client_user.id}").get_data(as_text=True)


def test_release_review_page_links_to_the_profile(admin_client, client_user):
    release = Release(user_id=client_user.id, status="pending", first_submitted_at=utcnow(), submitted_at=utcnow(), **FULL)
    db.session.add(release)
    db.session.commit()
    assert "自社サービスの紹介」を見る" not in admin_client.get(f"/admin/releases/{release.id}").get_data(as_text=True)
    db.session.add(ServiceProfile(user_id=client_user.id, body="紹介"))
    db.session.commit()
    body = admin_client.get(f"/admin/releases/{release.id}").get_data(as_text=True)
    assert f"/admin/services/{client_user.id}" in body


def test_admin_index_links_to_it(admin_client):
    assert "/admin/services" in admin_client.get("/admin/").get_data(as_text=True)


# ================= 退会 =================
def test_profile_is_deleted_with_the_account(admin_client, app):
    user = make_user(email="leaving@example.com")
    db.session.add(ServiceProfile(user_id=user.id, body="退会で消えるはずの紹介"))
    db.session.commit()
    res = admin_client.post(f"/admin/users/{user.id}/delete", data={"confirm_email": user.email}, follow_redirects=True)
    assert "退会処理をしました" in res.get_data(as_text=True)
    assert ServiceProfile.query.count() == 0 and db.session.get(User, user.id) is None


def test_one_profile_per_user_is_enforced_by_the_database(client_user):
    from sqlalchemy.exc import IntegrityError

    db.session.add(ServiceProfile(user_id=client_user.id, body="1つ目"))
    db.session.commit()
    db.session.add(ServiceProfile(user_id=client_user.id, body="2つ目"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
