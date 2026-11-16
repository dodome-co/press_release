import io

from app.extensions import db
from app.models import User

from .conftest import login, png_bytes

PROFILE = {
    "company_name": "株式会社更新",
    "postal_code": "１０００００１",
    "address": "東京都千代田区千代田1-1",
    "department": "広報部",
    "contact_name": "鈴木一郎",
    "phone": "03-0000-1111",
    "mobile_phone": "090-1234-5678",
    "founded": "2015年4月",
    "capital": "1,000万円",
    "employees": "25名",
    "business_description": "ソフトウェアの企画・開発",
    "website_url": "https://example.com",
}


def test_profile_requires_login(client):
    assert client.get("/mypage/profile").status_code == 302


def test_admin_cannot_open_profile(client, admin_user):
    login(client, "admin@example.com")
    assert client.get("/mypage/profile").status_code == 403


def test_update_profile(client, client_user):
    login(client, "client@example.com")
    res = client.post("/mypage/profile", data=PROFILE)
    assert res.status_code == 302
    u = db.session.get(User, client_user.id)
    assert u.company_name == "株式会社更新" and u.postal_code == "100-0001"
    assert u.department == "広報部" and u.website_url == "https://example.com"
    assert u.phone == "03-0000-1111" and u.mobile_phone == "090-1234-5678"


def test_optional_fields_can_be_blank(client, client_user):
    login(client, "client@example.com")
    data = {**PROFILE, "department": "", "founded": "", "capital": "", "employees": "",
            "business_description": "", "website_url": "", "mobile_phone": ""}
    assert client.post("/mypage/profile", data=data).status_code == 302
    u = db.session.get(User, client_user.id)
    assert u.department is None and u.website_url is None and u.mobile_phone is None


def test_dangerous_url_scheme_rejected(client, client_user):
    login(client, "client@example.com")
    res = client.post("/mypage/profile", data={**PROFILE, "website_url": "javascript:alert(1)"})
    assert res.status_code == 200
    assert db.session.get(User, client_user.id).website_url is None


def test_required_profile_fields_enforced(client, client_user):
    login(client, "client@example.com")
    res = client.post("/mypage/profile", data={**PROFILE, "company_name": ""})
    assert res.status_code == 200
    assert db.session.get(User, client_user.id).company_name == "株式会社テスト"


def _upload_logo(client, data=None, name="logo.png"):
    return client.post("/mypage/profile/logo", data={"logo": (io.BytesIO(data or png_bytes()), name)},
                       content_type="multipart/form-data", follow_redirects=True)


def test_upload_logo_and_fetch(client, client_user):
    login(client, "client@example.com")
    _upload_logo(client)
    u = db.session.get(User, client_user.id)
    assert u.logo_path
    res = client.get(f"/files/logos/{u.id}")
    assert res.status_code == 200 and res.mimetype == "image/png"


def test_replacing_logo_deletes_old_file(client, client_user):
    from app.uploads import resolve_path

    login(client, "client@example.com")
    _upload_logo(client)
    old = db.session.get(User, client_user.id).logo_path
    old_path = resolve_path(old)
    _upload_logo(client)
    assert not old_path.exists()
    assert db.session.get(User, client_user.id).logo_path != old


def test_delete_logo(client, client_user):
    from app.uploads import resolve_path

    login(client, "client@example.com")
    _upload_logo(client)
    path = resolve_path(db.session.get(User, client_user.id).logo_path)
    client.post("/mypage/profile/logo/delete")
    assert db.session.get(User, client_user.id).logo_path is None
    assert not path.exists()


def test_invalid_logo_rejected(client, client_user):
    login(client, "client@example.com")
    res = _upload_logo(client, data=b"not an image", name="logo.png")
    assert "画像として読み込めません" in res.get_data(as_text=True)
    assert db.session.get(User, client_user.id).logo_path is None


def test_others_cannot_fetch_logo(app, client, client_user):
    from .conftest import make_user

    login(client, "client@example.com")
    _upload_logo(client)
    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/files/logos/{client_user.id}").status_code == 404
