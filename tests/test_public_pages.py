import re

import pytest

from app.extensions import db
from app.models import Order, utcnow

from .conftest import login


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


# ================= トップページ =================
def test_top_page_is_public_and_has_the_story(client):
    res = client.get("/")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    for expected in ("プレスリリースサポート", "元地方紙の記者", "プレスリリースが届くまで", "1件目は無料",
                     "記者発表会の企画・当日の同席", "投げ込み交渉代行", "自社媒体などに掲載する記事制作"):
        assert expected in body, expected
    assert "準備中です" not in body


def test_top_page_has_the_main_message_and_the_logo(client):
    body = client.get("/").get_data(as_text=True)
    assert "東北のいいものを、地域のメディアに。" in body and "より多くの人に。" in body
    assert "東北のいいものを、地域のメディアに。</span><span>より多くの人に。" in body
    assert "地域に密着し、御社の広報戦略をサポートします。" in body
    assert "元地方紙の記者が、リリース作成やPR戦略をお手伝いします。" in body
    assert 'class="tagline"' in body and 'class="hero-logo"' in body and 'alt="ゆきわり。"' in body
    # 大きい文字の見出しが先、説明が、そのあと
    assert body.index('class="hero-logo"') < body.index('class="tagline"') < body.index("地域に密着し")


def test_top_page_no_longer_has_the_old_message(client):
    body = client.get("/").get_data(as_text=True)
    for old in ("より多くの人たちに", "地域発の商品を地域のメディアにつなぎます",
                "プレスリリースづくりと、記者クラブへの投げ込みを、サポートします"):
        assert old not in body, old


def test_main_message_is_big(client):
    from pathlib import Path

    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    size = float(re.search(r"section\.hero \.tagline \{[^}]*font-size: ([\d.]+)rem", css).group(1))
    assert size >= 2.0  # 大きめの文字
    assert size > float(re.search(r"section\.hero \.hero-lead \{ font-size: ([\d.]+)rem", css).group(1)) * 1.5


def test_top_page_buttons_are_styled_as_buttons(client):
    body = client.get("/").get_data(as_text=True)
    assert 'class="button" href="/register"' in body and 'class="button secondary" href="/login"' in body


def test_about_page_tells_the_story_of_yukiwari(client):
    body = client.get("/about").get_data(as_text=True)
    assert "「ゆきわり。」は地域密着型のPR会社です。" in body
    assert "東北生まれで元地方紙記者の姉と、現役の動画編集者の弟が運営しています。" in body
    assert "社名の由来は、ニンニク栽培・販売を営む母のオリジナルブランド「雪割XXXX」です。" in body
    assert "ロゴはオリジナルキャラクター「雪割XXXファミリー」を使っています。" in body
    assert body.index("お手伝いをします") < body.index("地域密着型") < body.index("姉と") < body.index("社名の由来")


def test_about_page_no_longer_has_the_old_wording(client):
    body = client.get("/about").get_data(as_text=True)
    for old in ("PR代行", "東北生まれの姉弟", "拝借しています", "御社の広報活動をサポートします"):
        assert old not in body, old


def test_business_is_pr_and_public_relations_support(client):
    assert "<th>主な事業</th><td>広報、PR活動支援</td>" in client.get("/about").get_data(as_text=True)


def test_company_name_defaults_to_yukiwari(client):
    body = client.get("/about").get_data(as_text=True)
    assert "<th>会社名</th><td>ゆきわり。</td>" in body


def test_top_page_does_not_list_the_release_slot_as_a_separate_service(client):
    body = client.get("/").get_data(as_text=True)
    assert "2件目以降のリリース作成（1件分）" not in body and "2件目以降は、1件ずつ追加できます" in body


def test_top_page_for_visitors_links_to_signup_and_login(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/register"' in body and 'href="/login"' in body
    assert "マイページ" not in body


def test_top_page_for_clients_and_admins(client, client_user, admin_user, app):
    login(client, "client@example.com")
    body = client.get("/").get_data(as_text=True)
    assert "プレスリリースを作る" in body and "無料で会員登録" not in body
    admin = app.test_client()
    login(admin, "admin@example.com")
    assert "管理者ページへ" in admin.get("/").get_data(as_text=True)


# ================= 運営者について（About Us） =================
def test_about_page_is_public(client):
    res = client.get("/about")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    for expected in ("運営者について", "東北のいいもの、新しいサービスを伝えるお手伝いをします", "地域密着型のPR会社",
                     "広報、PR活動支援", "代表者", "横川 鈴", "東北のとある村出身、岩手県在住", "計12年間勤務",
                     "青森・宮城・岩手県内の記者クラブ", "学芸員、防災士", "動画担当", "Kato Dream",
                     "東北のとある村出身、国外在住", "都内の映像制作会社に約1年間勤務し、その後独立",
                     "人気 YouTuber などの動画制作"):
        assert expected in body, expected


def test_ages_are_shown_as_30s_and_private_details_are_removed(client):
    body = client.get("/about").get_data(as_text=True)
    assert body.count("30代") == 2  # 代表者と動画担当の、どちらも
    for removed in ("旧姓", "鈴木", "琴実", "1992", "1995", "年生まれ", "TOEIC", "805", "Ayumu", "QuizKnock", "Quizknock",
                    "Nephrite", "２０１４"):
        assert removed not in body, removed


def test_birthplace_residence_and_graduation_years_are_blurred(client):
    body = client.get("/about").get_data(as_text=True)
    assert body.count("東北のとある村出身") == 2 and "岩手県在住" in body and "国外在住" in body
    for removed in ("福島", "盛岡市", "オランダ", "2014", "2019", "2020"):
        assert removed not in body, removed


def test_contact_details_come_from_settings_not_from_the_code(client, app):
    body = client.get("/about").get_data(as_text=True)
    assert body.count("（準備中）") == 2  # 設定がなければ、所在地とメールアドレスは、「準備中」
    app.config.update(COMPANY_ADDRESS="岩手県盛岡市テスト1-2-3", SUPPORT_EMAIL="info@example.com", OPERATOR_NAME="テスト社")
    body = client.get("/about").get_data(as_text=True)
    assert "岩手県盛岡市テスト1-2-3" in body and "info@example.com" in body and "テスト社" in body
    assert "VERBINDEN" not in body and "ゆきわり。」は" in body  # 設定の会社名と、紹介文の「ゆきわり。」は、別


def test_default_operator_name(app):
    assert app.config["OPERATOR_NAME"] == "ゆきわり。"


def test_about_escapes_settings(client, app):
    app.config["COMPANY_ADDRESS"] = "<script>alert(1)</script>"
    body = client.get("/about").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_no_personal_contact_details_in_the_repository():
    """個人のメールアドレスや、番地を、コードに書かない（設定で渡す）。"""
    from pathlib import Path

    text = Path("app/templates/about.html").read_text(encoding="utf-8")
    # 伏せ字の住所（XXXXXXX）やメールアドレスは、載せない。ブランド名の伏せ字（XXXX）は、運営者が指定した表記
    assert "@" not in text and "gmail" not in text and not re.search(r"X{5,}", text)


# ================= ログイン前のサービス一覧 =================
def test_services_for_visitors_show_everything_but_no_order_forms(client):
    body = client.get("/services").get_data(as_text=True)
    for expected in ("記者発表会の企画・当日の同席", "岩手県内：80,000円", "投げ込み交渉代行", "3,000円",
                     "10,000円", "30,000円", "キャンセル・返金について", "リリース作成以外のサービスは、面談が必須となります。"):
        assert expected in body, expected
    assert "申し込んで支払う" not in body and 'name="option"' not in body and "csrf_token" not in body


def test_services_for_visitors_have_signup_and_login_links(client):
    body = client.get("/services").get_data(as_text=True)
    assert "会員登録して申し込む" in body and "ログインして申し込む" in body
    assert "/login?next=/services" in body


def test_visitor_is_sent_back_to_services_after_login(client, client_user):
    res = client.post("/login?next=/services", data={"email": "client@example.com", "password": "password123"})
    assert res.headers["Location"].endswith("/services")


def test_release_slot_explanation_for_visitors(client):
    body = client.get("/services").get_data(as_text=True)
    assert "会員登録（無料）をすると、プレスリリースを1件、無料で作成できます" in body
    assert "/services/release_creation/order" not in body


def test_services_for_admins_are_read_only(admin_client):
    body = admin_client.get("/services").get_data(as_text=True)
    assert "記者発表会の企画・当日の同席" in body
    assert "申し込んで支払う" not in body and "会員登録して申し込む" not in body


def test_services_for_clients_still_have_order_forms(client, client_user):
    login(client, "client@example.com")
    body = client.get("/services").get_data(as_text=True)
    assert body.count("申し込んで支払う") == 4  # 無料の枠が残っている間は、リリース作成の追加以外の4つ
    assert "会員登録して申し込む" not in body


def test_ordering_still_requires_a_client_login(client, admin_user, app):
    for path in ("/services/distribution_proxy/order", "/services/release_creation/order"):
        assert client.post(path).status_code == 302
    login(client, "admin@example.com")
    assert client.post("/services/distribution_proxy/order").status_code == 403
    assert Order.query.count() == 0


# ================= メニュー =================
def test_menu_for_visitors(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/services"' in body and 'href="/about"' in body
    assert 'href="/guide"' not in body and 'href="/releases/"' not in body  # ログイン後の項目は、出さない


def test_menu_for_clients_and_admins(client, client_user, admin_user, app):
    login(client, "client@example.com")
    body = client.get("/").get_data(as_text=True)
    for path in ("/releases/", "/services", "/guide", "/mypage/profile", "/about", "/mypage/"):
        assert f'href="{path}"' in body, path
    admin = app.test_client()
    login(admin, "admin@example.com")
    body = admin.get("/").get_data(as_text=True)
    assert 'href="/about"' in body and 'href="/services"' in body and 'href="/admin/"' in body
    assert 'href="/guide"' not in body


def test_demo_banner_is_on_public_pages_too(client, app):
    app.config["DEMO_MODE"] = True
    for path in ("/", "/about", "/services"):
        assert "ビジネスプランの発表用のデモです" in client.get(path).get_data(as_text=True), path
