import pytest

from app.extensions import db
from app.prefectures import PREFECTURES, prefecture_of

from .conftest import login, make_user


def test_there_are_47_prefectures():
    assert len(PREFECTURES) == 47 and len(set(PREFECTURES)) == 47


@pytest.mark.parametrize("name", PREFECTURES)
def test_every_prefecture_is_recognized(name):
    assert prefecture_of(f"{name}盛岡市内丸10-1") == name
    assert prefecture_of(name) == name


@pytest.mark.parametrize("address,expected", [
    ("東京都千代田区1-1-1", "東京都"),
    ("京都府京都市中京区", "京都府"),  # 「東京都」と「京都府」を取り違えない
    ("  岩手県盛岡市  ", "岩手県"),
    ("岩手県盛岡市", "岩手県"),
    ("〒020-0023 岩手県盛岡市内丸10-1", "岩手県"),  # 郵便番号が付いていても読める
    ("〒0200023　岩手県盛岡市", "岩手県"),
    ("岩手県盛岡市", "岩手県"),
    ("北海道札幌市中央区", "北海道"),
    ("大阪府大阪市北区", "大阪府"),
])
def test_prefecture_from_address(address, expected):
    assert prefecture_of(address) == expected


@pytest.mark.parametrize("address", [None, "", "   ", "盛岡市内丸10-1", "株式会社テスト 岩手県", "岩手", "Iwate", "x岩手県"])
def test_unknown_when_address_does_not_start_with_prefecture(address):
    assert prefecture_of(address) is None


def _guide(client, user_address, app=None):
    user = make_user(email="guide@example.com", address=user_address)
    login(client, "guide@example.com")
    return client.get("/guide")


def test_guide_shows_the_clients_prefecture(client):
    body = _guide(client, "岩手県盛岡市内丸10-1").get_data(as_text=True)
    assert "岩手県庁内の記者クラブ" in body and "岩手県庁の代表電話" in body
    assert "会社の所在地の都道府県を、自動で表示できませんでした" not in body


@pytest.mark.parametrize("address,club,office", [
    ("東京都千代田区1-1", "東京都庁内の記者クラブ", "東京都庁の代表電話"),
    ("北海道札幌市中央区", "北海道庁内の記者クラブ", "北海道庁の代表電話"),
    ("大阪府大阪市北区", "大阪府庁内の記者クラブ", "大阪府庁の代表電話"),
    ("京都府京都市", "京都府庁内の記者クラブ", "京都府庁の代表電話"),
    ("沖縄県那覇市", "沖縄県庁内の記者クラブ", "沖縄県庁の代表電話"),
])
def test_guide_wording_for_each_kind_of_prefecture(client, address, club, office):
    body = _guide(client, address).get_data(as_text=True)
    assert club in body and office in body


def test_guide_falls_back_when_address_has_no_prefecture(client):
    body = _guide(client, "盛岡市内丸10-1").get_data(as_text=True)
    assert "お住まいの都道府県庁内の記者クラブ" in body and "お住まいの都道府県庁の代表電話" in body
    assert "自動で表示できませんでした" in body and "/mypage/profile" in body


def test_guide_has_all_steps_and_methods(client):
    body = _guide(client, "岩手県盛岡市").get_data(as_text=True)
    for expected in ("プレスリリースの準備は整いましたか？", "記者クラブにつないでもらいましょう",
                     "幹事社を通じて", "「投げ込みをしたい」と伝えましょう", "2通り",
                     "必要な部数を、記者クラブに持ち込みます", "FAX またはメールで",
                     "県庁所在市や大きな市には、地元の記者クラブがある場合があります",
                     "投げ込み」とは"):
        assert expected in body, expected


def test_guide_never_echoes_the_address(client):
    body = _guide(client, "岩手県<script>alert(1)</script>盛岡市").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body and "盛岡市" not in body  # 一覧にある都道府県の名前だけを出す
    assert "岩手県庁内の記者クラブ" in body


def test_guide_needs_login_and_client_role(client, admin_user):
    assert client.get("/guide").status_code == 302
    login(client, "admin@example.com")
    assert client.get("/guide").status_code == 403


def test_nav_has_guide_link_for_clients_only(client, client_user, admin_user):
    login(client, "client@example.com")
    assert 'href="/guide"' in client.get("/mypage/").get_data(as_text=True)
    other = client.application.test_client()
    login(other, "admin@example.com")
    assert 'href="/guide"' not in other.get("/admin/").get_data(as_text=True)


def test_guide_links_to_services_page(client):
    body = _guide(client, "岩手県盛岡市").get_data(as_text=True)
    assert "/services" in body and "ご自身での投げ込みが難しいときは" in body


def test_approved_release_page_links_to_guide(app, client, client_user):
    from app.models import Release, utcnow

    from .test_releases import FULL

    login(client, "client@example.com")
    r = Release(user_id=client_user.id, status="approved", first_submitted_at=utcnow(), summary="x", **FULL)
    db.session.add(r)
    db.session.commit()
    assert "投げ込みのしかた" in client.get(f"/releases/{r.id}").get_data(as_text=True)


# ---- 住所は、都道府県から入力する ----
@pytest.mark.parametrize("address", ["盛岡市内丸10-1", "住所", "x", "株式会社 岩手県", "岩手", "Iwate"])
def test_register_requires_address_to_start_with_prefecture(client, address):
    from .test_auth import REGISTER_DATA
    from app.models import User

    res = client.post("/register", data={**REGISTER_DATA, "address": address})
    assert res.status_code == 200 and "住所は、都道府県から入力してください" in res.get_data(as_text=True)
    assert User.query.count() == 0


def test_register_accepts_address_with_prefecture(client):
    from .test_auth import REGISTER_DATA
    from app.models import User

    for i, address in enumerate(("岩手県盛岡市内丸10-1", "〒020-0023 岩手県盛岡市内丸10-1", "東京都千代田区1-1-1")):
        client.post("/logout")
        data = {**REGISTER_DATA, "address": address, "email": f"user{i}@example.com"}
        assert client.post("/register", data=data).status_code == 302, address
    assert User.query.count() == 3


def test_profile_requires_address_to_start_with_prefecture(client, client_user):
    from .test_profile import PROFILE

    login(client, "client@example.com")
    res = client.post("/mypage/profile", data={**PROFILE, "address": "盛岡市内丸10-1"})
    assert res.status_code == 200 and "住所は、都道府県から入力してください" in res.get_data(as_text=True)
    db.session.refresh(client_user)
    assert client_user.address == "東京都千代田区1-1-1"  # 変わらない
    assert client.post("/mypage/profile", data={**PROFILE, "address": "岩手県盛岡市内丸10-1"}).status_code == 302


def test_register_and_profile_pages_show_address_hint(client, client_user):
    assert "都道府県から入力" in client.get("/register").get_data(as_text=True)
    login(client, "client@example.com")
    assert "都道府県から入力" in client.get("/mypage/profile").get_data(as_text=True)
