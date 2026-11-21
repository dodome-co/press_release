import io

import pytest
from docx import Document

from app import demo, payments
from app.extensions import db
from app.models import MailLog, Order, OrderRefund, Release, ReleasePhoto, ServiceProfile, StripeEvent, User
from app.uploads import resolve_path, upload_root

from .conftest import login, make_user


@pytest.fixture()
def demo_app(app):
    app.config["DEMO_MODE"] = True
    return app


def _seed():
    return demo.seed()


# ---- 動くのは、デモ環境だけ ----
def test_seed_refuses_outside_demo_mode(app):
    with pytest.raises(demo.DemoError, match="デモ環境"):
        demo.seed()
    with pytest.raises(demo.DemoError):
        demo.wipe()
    assert User.query.count() == 0


def test_cli_refuses_outside_demo_mode(app):
    runner = app.test_cli_runner()
    for args in (["seed-demo"], ["reset-demo", "--yes"]):
        res = runner.invoke(args=args)
        assert res.exit_code != 0 and "デモ環境" in res.output


def test_reset_needs_confirmation_flag(demo_app):
    make_user(email="keep@example.com")
    res = demo_app.test_cli_runner().invoke(args=["reset-demo"])
    assert res.exit_code != 0 and "--yes" in res.output
    assert User.query.filter_by(email="keep@example.com").count() == 1  # 何も消えていない


# ---- 作られるもの ----
def test_seed_creates_accounts_that_can_log_in(demo_app):
    accounts = _seed()
    assert [a["role"] for a in accounts] == ["管理者", "クライアント", "クライアント", "クライアント"]
    for account in accounts:
        c = demo_app.test_client()
        assert login(c, account["email"], demo.demo_password()).status_code == 302, account["email"]
    assert User.query.filter_by(email=demo.ADMIN_EMAIL).one().role == "admin"
    assert User.query.filter_by(role="client").count() == 3


def test_password_can_be_set_by_environment(demo_app, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "my-own-password-1")
    _seed()
    assert login(demo_app.test_client(), "demo-client1@example.com", "my-own-password-1").status_code == 302


def test_cli_prints_accounts_and_password(demo_app):
    res = demo_app.test_cli_runner().invoke(args=["seed-demo"])
    assert res.exit_code == 0
    for expected in ("demo-admin@example.com", "demo-client1@example.com", "株式会社いわて菓子工房", demo.demo_password()):
        assert expected in res.output


def test_client1_has_approved_release_with_word_and_photos(demo_app):
    _seed()
    user = User.query.filter_by(email="demo-client1@example.com").one()
    release = Release.query.filter_by(user_id=user.id).one()
    assert release.status == "approved" and release.summary and release.approved_by
    assert [p.position for p in release.photos] == [1, 2] and len(release.schedule_items) == 3
    word = resolve_path(release.file_path)
    assert word.is_file()
    text = "\n".join(p.text for p in Document(str(word)).paragraphs)
    assert "いわて栗ようかん" in text and "株式会社いわて菓子工房" in text and "岩手県" in text
    assert len(Document(str(word)).inline_shapes) == 3  # ロゴ＋写真2枚
    assert user.service_profile is not None and user.logo_path


def test_client1_orders_cover_different_states(demo_app):
    _seed()
    user = User.query.filter_by(email="demo-client1@example.com").one()
    orders = {o.service_type: o for o in Order.query.filter_by(user_id=user.id).all()}
    assert orders["distribution_proxy"].progress == "delivered" and orders["distribution_proxy"].amount_yen == 3000
    article = orders["article_creation"]
    assert article.progress == "delivered" and resolve_path(article.delivery_file_path).is_file()
    assert article.delivery_filename == "記事.docx"
    press = orders["interview_planning"]
    assert (press.amount_yen, press.service_option, press.progress) == (80000, "iwate", "in_progress")
    assert press.due_date is not None
    video = orders["video_creation"]
    assert video.status == "partially_refunded" and video.refunded_amount == 15000 and video.materials_url
    assert OrderRefund.query.filter_by(order_id=video.id).one().status == "succeeded"


def test_client2_has_a_release_waiting_for_approval(demo_app):
    _seed()
    user = User.query.filter_by(email="demo-client2@example.com").one()
    release = Release.query.filter_by(user_id=user.id).one()
    assert release.status == "pending" and release.file_path is None
    # AI がまとめた内容（要旨・サブタイトル・当日の対応者・タイトルの案）と、チャットのやりとりが入っている
    assert release.summary.endswith("ご案内申し上げます。") and release.summary_source == "ai_assisted"
    assert release.subtitle and len(release.speaker_list) == 2 and len(release.title_candidate_list) == 3
    assert [m.role for m in release.chat_messages] == ["ai", "client", "ai", "client", "ai"]
    assert len(release.photos) == 2
    paid_slot = Order.query.filter_by(user_id=user.id, service_type="release_creation").one()
    assert paid_slot.status == "paid" and paid_slot.progress is None


def test_client3_is_nearly_empty(demo_app):
    _seed()
    user = User.query.filter_by(email="demo-client3@example.com").one()
    assert [r.status for r in Release.query.filter_by(user_id=user.id)] == ["draft"]
    assert Order.query.filter_by(user_id=user.id).count() == 0 and user.service_profile is None and user.logo_path is None


def test_seeded_addresses_start_with_a_prefecture(demo_app):
    from app.prefectures import prefecture_of

    _seed()
    assert {prefecture_of(u.address) for u in User.query.filter_by(role="client")} == {"岩手県", "宮城県", "青森県"}


def test_seed_twice_does_not_duplicate(demo_app):
    _seed()
    counts = (User.query.count(), Release.query.count(), Order.query.count(), ServiceProfile.query.count())
    _seed()
    assert (User.query.count(), Release.query.count(), Order.query.count(), ServiceProfile.query.count()) == counts


# ---- すべての画面が、サンプルデータで開ける ----
def test_every_page_opens_with_demo_data(demo_app):
    _seed()
    admin = demo_app.test_client()
    login(admin, demo.ADMIN_EMAIL, demo.demo_password())
    paths = ["/admin/", "/admin/releases", "/admin/orders", "/admin/users", "/admin/services", "/admin/mails"]
    paths += [f"/admin/releases/{r.id}" for r in Release.query.filter(Release.status != "draft")]
    paths += [f"/admin/orders/{o.id}" for o in Order.query.all()]
    paths += [f"/admin/users/{u.id}" for u in User.query.filter_by(role="client")]
    paths += [f"/admin/services/{p.user_id}" for p in ServiceProfile.query.all()]
    for path in paths:
        assert admin.get(path).status_code == 200, path
    for spec in demo.CLIENTS:
        c = demo_app.test_client()
        login(c, spec["email"], demo.demo_password())
        user = User.query.filter_by(email=spec["email"]).one()
        client_paths = ["/mypage/", "/mypage/orders", "/mypage/profile", "/mypage/service", "/releases/", "/services", "/guide"]
        client_paths += [f"/releases/{r.id}" for r in Release.query.filter_by(user_id=user.id)]
        client_paths += [f"/mypage/orders/{o.id}" for o in Order.query.filter_by(user_id=user.id)]
        for path in client_paths:
            assert c.get(path).status_code == 200, (spec["email"], path)


def test_guide_shows_each_demo_clients_prefecture(demo_app):
    _seed()
    for email, prefecture in (("demo-client1@example.com", "岩手県"), ("demo-client2@example.com", "宮城県"),
                              ("demo-client3@example.com", "青森県")):
        c = demo_app.test_client()
        login(c, email, demo.demo_password())
        assert f"{prefecture}庁内の記者クラブ" in c.get("/guide").get_data(as_text=True)


def test_client_can_download_demo_deliverables(demo_app):
    _seed()
    user = User.query.filter_by(email="demo-client1@example.com").one()
    c = demo_app.test_client()
    login(c, user.email, demo.demo_password())
    release = Release.query.filter_by(user_id=user.id).one()
    assert c.get(f"/releases/{release.id}/download").status_code == 200
    article = Order.query.filter_by(user_id=user.id, service_type="article_creation").one()
    assert c.get(f"/mypage/orders/{article.id}/delivery").status_code == 200


def test_pending_demo_release_can_be_approved_by_the_admin(demo_app):
    _seed()
    release = Release.query.filter_by(status="pending").one()
    admin = demo_app.test_client()
    login(admin, demo.ADMIN_EMAIL, demo.demo_password())
    res = admin.post(f"/admin/releases/{release.id}", data={
        "title": release.title, "event_date": release.event_date.isoformat(), "event_start_time": "14:00", "event_end_time": "15:30", "location": release.location,
        "service_description": release.service_description, "feature_1": release.feature_1,
        "application_method": release.application_method, "application_deadline_at": "2026-12-03T17:00",
        "summary": "デモの要旨です。"})
    assert res.status_code == 302
    assert admin.post(f"/admin/releases/{release.id}/approve").status_code == 302
    db.session.refresh(release)
    assert release.status == "approved" and resolve_path(release.file_path).is_file()


# ---- デモの返金（本物の Stripe は呼ばない） ----
def test_demo_orders_can_be_refunded_without_stripe(demo_app):
    _seed()
    press = Order.query.filter_by(service_type="interview_planning").one()
    admin = demo_app.test_client()
    login(admin, demo.ADMIN_EMAIL, demo.demo_password())
    res = admin.post(f"/admin/orders/{press.id}/refund", data={"amount_yen": "40000", "reason": "policy"},
                     follow_redirects=True)
    assert "40,000円を返金しました" in res.get_data(as_text=True)
    db.session.refresh(press)
    assert press.refunded_amount == 40000 and press.status == "partially_refunded"
    assert OrderRefund.query.filter_by(order_id=press.id, reason="policy").one().stripe_refund_id.startswith("re_demo_")


def test_fake_demo_payment_is_not_simulated_outside_demo_mode(app):
    app.config["STRIPE_SECRET_KEY"] = ""
    with pytest.raises(payments.PaymentError):
        payments.create_refund("pi_demo_x", 100, "key")  # デモ環境でなければ、本物の Stripe の経路（キーがなく、失敗する）


def test_demo_simulation_only_applies_to_pi_demo_prefix(demo_app, monkeypatch):
    import stripe

    demo_app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    called = []
    monkeypatch.setattr(stripe.Refund, "create", lambda **kw: called.append(kw) or {"id": "re_real", "status": "succeeded"})
    assert payments.create_refund("pi_demo_abc", 100, "k")["id"].startswith("re_demo_") and called == []
    assert payments.create_refund("pi_real_abc", 100, "k")["id"] == "re_real" and len(called) == 1


# ---- 作り直し ----
def test_reset_removes_everything_but_admins_and_recreates_samples(demo_app):
    from werkzeug.datastructures import FileStorage

    from app.uploads import save_image

    make_user(email="boss@example.com", role="admin")
    _seed()
    visitor = make_user(email="visitor@example.com")  # デモの来場者が登録した会員
    logo = save_image(FileStorage(stream=io.BytesIO(_png_bytes()), filename="l.png"), f"users/{visitor.id}/logo")
    visitor.logo_path = logo.relative_path
    visitor_release = Release(user_id=visitor.id, title="来場者の書類")
    db.session.add(visitor_release)
    db.session.commit()
    photo = save_image(FileStorage(stream=io.BytesIO(_png_bytes(400, 300)), filename="p.png"),
                       f"releases/{visitor_release.id}/photos")
    db.session.add(MailLog(to_address="x@example.com", subject="s", body="b"))
    db.session.add(StripeEvent(event_id="evt_1", event_type="x"))
    db.session.commit()
    logo_file, photo_file = resolve_path(logo.relative_path), resolve_path(photo.relative_path)
    assert logo_file.is_file() and photo_file.is_file()

    res = demo_app.test_cli_runner().invoke(args=["reset-demo", "--yes"])
    assert res.exit_code == 0 and "消したもの" in res.output
    assert User.query.filter_by(email="visitor@example.com").count() == 0
    assert not logo_file.exists() and not photo_file.exists()  # 来場者のファイルも、消える
    assert User.query.filter_by(email="boss@example.com").count() == 1  # 管理者は残る
    assert MailLog.query.count() == 0 and StripeEvent.query.count() == 0
    assert User.query.filter_by(role="client").count() == 3  # サンプルを作り直した
    assert Release.query.count() == 3


def _png_bytes(w=60, h=40):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (w, h), (1, 2, 3)).save(buffer, "PNG")
    return buffer.getvalue()


def test_reset_leaves_no_orphan_files(demo_app):
    _seed()
    demo.wipe()
    assert [p for p in upload_root().rglob("*") if p.is_file()] == []
    assert Order.query.count() == 0 and Release.query.count() == 0 and ReleasePhoto.query.count() == 0


def test_seeded_titles_fit_on_one_line_in_word(demo_app):
    from app.textutil import display_width

    _seed()
    limit = demo_app.config["TITLE_MAX_WIDTH"]
    assert all(display_width(r.title) <= limit for r in Release.query.filter(Release.status != "draft"))
