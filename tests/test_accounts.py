import io
import logging
import uuid

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import payments
from app.extensions import db
from app.models import (
    Order,
    OrderRefund,
    PasswordResetToken,
    Release,
    ReleasePhoto,
    User,
    utcnow,
)
from app.uploads import resolve_path, save_document, save_image

from .conftest import login, make_user
from .test_releases import FULL


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def _image(subdir):
    return save_image(FileStorage(stream=io.BytesIO(_png()), filename="p.png"), subdir)


@pytest.fixture()
def admin_client(app, admin_user):
    c = app.test_client()
    login(c, "admin@example.com")
    return c


def _full_client(email="leaving@example.com"):
    """写真・Word・ロゴ・納品ファイル・注文を持つクライアントを作る。"""
    user = make_user(email=email, mobile_phone="090-0000-0000", department="広報部")
    user.logo_path = _image(f"users/{user.id}/logo").relative_path
    release = Release(user_id=user.id, status="approved", first_submitted_at=utcnow(), summary="要旨", **FULL)
    db.session.add(release)
    db.session.commit()
    photo = _image(f"releases/{release.id}/photos")
    db.session.add(ReleasePhoto(release_id=release.id, position=1, file_path=photo.relative_path,
                                original_filename="p.png", content_type="image/png", size_bytes=photo.size_bytes))
    word = resolve_path(f"releases/{release.id}/release.docx")
    word.parent.mkdir(parents=True, exist_ok=True)
    word.write_bytes(b"docx")
    release.file_path = f"releases/{release.id}/release.docx"
    db.session.add(PasswordResetToken(user_id=user.id, token_hash=uuid.uuid4().hex * 2, expires_at=utcnow()))
    article = Order(user_id=user.id, service_type="article_creation", service_name="記事作成", amount_yen=20000,
                    status="paid", progress="delivered", paid_at=utcnow(), delivered_at=utcnow(),
                    stripe_payment_intent_id=f"pi_article_{uuid.uuid4().hex}", refund_reason="個人名を含むメモ")
    doc = save_document(FileStorage(stream=io.BytesIO(b"hello"), filename="記事.txt"), "orders/0/delivery")
    video = Order(user_id=user.id, service_type="video_creation", service_name="動画作成", amount_yen=50000,
                  status="paid", progress="delivered", paid_at=utcnow(), delivered_at=utcnow(),
                  materials_url="https://drive.example.com/secret-folder", delivery_url="https://youtu.be/abc",
                  stripe_payment_intent_id=f"pi_video_{uuid.uuid4().hex}")
    db.session.add_all([article, video])
    db.session.commit()
    saved = save_document(FileStorage(stream=io.BytesIO(b"hello"), filename="記事.txt"), f"orders/{article.id}/delivery")
    article.delivery_file_path, article.delivery_filename = saved.relative_path, "記事.txt"
    db.session.add(OrderRefund(order_id=article.id, amount_yen=1000, reason="policy", status="succeeded",
                               note="山田さんの個人的な事情"))
    db.session.commit()
    return user, release, article, video


def _delete(admin_client, user, confirm=None):
    return admin_client.post(f"/admin/users/{user.id}/delete",
                             data={"confirm_email": confirm if confirm is not None else user.email},
                             follow_redirects=True)


# ---- アクセス制御・一覧 ----
def test_pages_need_admin(client, client_user):
    assert client.get("/admin/users").status_code == 302
    login(client, "client@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get(f"/admin/users/{client_user.id}").status_code == 403
    assert client.post(f"/admin/users/{client_user.id}/delete", data={"confirm_email": client_user.email}).status_code == 403
    assert db.session.get(User, client_user.id) is not None


def test_list_shows_clients_only_and_searches(admin_client, client_user, admin_user):
    make_user(email="other@example.com", company_name="別の会社")
    body = admin_client.get("/admin/users").get_data(as_text=True)
    assert "client@example.com" in body and "other@example.com" in body and "admin@example.com" not in body
    body = admin_client.get("/admin/users?q=別の").get_data(as_text=True)
    assert "other@example.com" in body and "client@example.com" not in body
    assert admin_client.get("/admin/users?q=%25_").status_code == 200  # 記号が入っていても壊れない


def test_unknown_user_is_404(admin_client):
    assert admin_client.get("/admin/users/9999").status_code == 404
    assert admin_client.post("/admin/users/9999/delete", data={"confirm_email": "x"}).status_code == 404


# ---- 削除できる場合 ----
def test_account_is_deleted_with_all_personal_data_and_files(admin_client, app):
    user, release, article, video = _full_client()
    user_id, release_id = user.id, release.id
    logo = resolve_path(user.logo_path)
    photo_path = resolve_path(release.photos[0].file_path)
    word = resolve_path(release.file_path)
    delivery = resolve_path(article.delivery_file_path)
    assert all(p.exists() for p in (logo, photo_path, word, delivery))
    article_id, video_id = article.id, video.id

    res = _delete(admin_client, user)
    assert "退会処理をしました" in res.get_data(as_text=True)

    assert db.session.get(User, user_id) is None
    assert Release.query.filter_by(id=release_id).count() == 0 and ReleasePhoto.query.count() == 0
    assert PasswordResetToken.query.count() == 0
    assert not any(p.exists() for p in (logo, photo_path, word, delivery))
    for directory in (f"releases/{release_id}", f"users/{user_id}", f"orders/{article_id}"):
        assert not resolve_path(directory).exists(), directory


def test_orders_are_kept_but_anonymized(admin_client, app):
    user, release, article, video = _full_client()
    article_id, video_id = article.id, video.id
    _delete(admin_client, user)
    kept_article, kept_video = db.session.get(Order, article_id), db.session.get(Order, video_id)
    for order in (kept_article, kept_video):
        assert order is not None and order.user_id is None
        assert order.materials_url is None and order.delivery_url is None
        assert order.delivery_file_path is None and order.delivery_filename is None and order.refund_reason is None
    # 金額・サービス名・状態・日付は残る
    assert (kept_article.amount_yen, kept_article.service_name, kept_article.status) == (20000, "記事作成", "paid")
    assert (kept_video.amount_yen, kept_video.service_name) == (50000, "動画作成")
    assert kept_article.paid_at is not None and kept_article.delivered_at is not None
    assert kept_article.stripe_payment_intent_id.startswith("pi_article_")
    # 返金の履歴は残るが、メモは消える
    refund = OrderRefund.query.one()
    assert refund.amount_yen == 1000 and refund.note is None


def test_no_personal_data_remains_in_database(admin_client, app):
    user, *_ = _full_client()
    _delete(admin_client, user)
    from sqlalchemy import text

    for table in ("users", "releases", "orders", "order_refunds", "release_photos", "password_reset_tokens"):
        rows = db.session.execute(text(f"SELECT * FROM {table}")).fetchall()
        flat = " ".join(str(v) for row in rows for v in row)
        for secret in ("leaving@example.com", "株式会社テスト", "090-0000-0000", "secret-folder", "山田さん", "個人名"):
            if table == "users":
                continue  # 管理者の行は残る（会員のものは、下で確認）
            assert secret not in flat, (table, secret)
    assert User.query.filter_by(email="leaving@example.com").count() == 0


def test_user_cannot_log_in_or_use_old_session_after_deletion(app, admin_client):
    user, *_ = _full_client()
    c = app.test_client()
    assert login(c, user.email).status_code == 302
    _delete(admin_client, user)
    assert c.get("/mypage/orders").status_code == 302  # 古いセッションも使えない
    assert "メールアドレスまたはパスワードが違います" in c.post(
        "/login", data={"email": "leaving@example.com", "password": "password123"}).get_data(as_text=True)


def test_unpaid_orders_are_canceled_and_sessions_expired(app, admin_client, monkeypatch):
    expired = []
    monkeypatch.setattr(payments, "expire_checkout_session", lambda sid: expired.append(sid))
    user = make_user(email="leaving@example.com")
    order = Order(user_id=user.id, service_type="interview_planning", service_name="企画支援", amount_yen=60000,
                  status="unpaid", stripe_session_id="cs_1")
    db.session.add(order)
    db.session.commit()
    from app.models import OrderCheckoutSession

    db.session.add(OrderCheckoutSession(order_id=order.id, stripe_session_id="cs_1"))
    db.session.commit()
    order_id = order.id
    _delete(admin_client, user)
    kept = db.session.get(Order, order_id)
    assert kept.status == "canceled" and kept.user_id is None
    assert expired == ["cs_1"]


def test_used_slot_and_refunded_orders_do_not_block(admin_client):
    user = make_user(email="leaving@example.com")
    # 無料の枠＋購入した枠の、両方を使い切っている
    for _ in range(2):
        db.session.add(Release(user_id=user.id, first_submitted_at=utcnow(), status="approved", **FULL))
    db.session.add(Order(user_id=user.id, service_type="release_creation", service_name="枠", amount_yen=5000,
                         status="paid", progress=None, paid_at=utcnow()))
    db.session.add(Order(user_id=user.id, service_type="distribution_proxy", service_name="投げ込み", amount_yen=10000,
                         status="refunded", progress="received", refunded_amount=10000))
    db.session.commit()
    assert "退会処理をしました" in _delete(admin_client, user).get_data(as_text=True)


def test_unused_purchased_slot_blocks_deletion_until_refunded(admin_client):
    user = make_user(email="leaving@example.com")
    db.session.add(Release(user_id=user.id, first_submitted_at=utcnow(), status="approved", **FULL))  # 無料の枠は使用済み
    slot = Order(user_id=user.id, service_type="release_creation", service_name="枠", amount_yen=5000,
                 status="paid", progress=None, paid_at=utcnow())
    db.session.add(slot)
    db.session.commit()
    res = _delete(admin_client, user)
    assert "使っていない、購入済みの書類の枠が1つあります" in res.get_data(as_text=True)
    assert db.session.get(User, user.id) is not None
    slot.status, slot.refunded_amount = "refunded", 5000  # 返金したら、削除できる
    db.session.commit()
    assert "退会処理をしました" in _delete(admin_client, user).get_data(as_text=True)


def test_disputed_order_blocks_deletion(admin_client):
    user, release, article, video = _full_client()
    article.disputed_at = utcnow()
    db.session.commit()
    res = _delete(admin_client, user)
    assert "異議申し立て中の注文があります" in res.get_data(as_text=True)
    assert resolve_path(release.file_path).exists()  # 反証の記録を、消さない


def test_other_users_data_is_untouched(admin_client):
    leaving, *_ = _full_client("leaving@example.com")
    staying, release, article, video = _full_client("staying@example.com")
    _delete(admin_client, leaving)
    assert db.session.get(User, staying.id) is not None
    assert resolve_path(staying.logo_path).exists() and resolve_path(release.file_path).exists()
    assert resolve_path(article.delivery_file_path).exists()
    assert db.session.get(Order, video.id).user_id == staying.id and db.session.get(Order, video.id).materials_url


# ---- 削除できない場合 ----
def test_confirmation_email_must_match(admin_client):
    user, *_ = _full_client()
    for wrong in ("", "other@example.com", "LEAVING@example.com.evil"):
        res = _delete(admin_client, user, confirm=wrong)
        assert "一致しません" in res.get_data(as_text=True)
    assert db.session.get(User, user.id) is not None


def test_confirmation_email_is_case_insensitive(admin_client):
    user, *_ = _full_client()
    assert "退会処理をしました" in _delete(admin_client, user, confirm="  LEAVING@Example.com ").get_data(as_text=True)


def test_pending_release_blocks_deletion(admin_client):
    user, release, *_ = _full_client()
    release.status = "pending"
    db.session.commit()
    res = _delete(admin_client, user)
    assert "承認待ちの書類が1件" in res.get_data(as_text=True)
    assert db.session.get(User, user.id) is not None


def test_unfinished_paid_order_blocks_deletion(admin_client):
    user, release, article, video = _full_client()
    video.progress = "in_progress"
    db.session.commit()
    res = _delete(admin_client, user)
    assert "納品前の注文があります" in res.get_data(as_text=True)
    assert resolve_path(user.logo_path).exists()


def test_pending_refund_blocks_deletion(admin_client):
    user, release, article, video = _full_client()
    db.session.add(OrderRefund(order_id=article.id, amount_yen=100, reason="policy", status="pending"))
    db.session.commit()
    assert "返金の処理中" in _delete(admin_client, user).get_data(as_text=True)


def test_blockers_are_shown_on_detail_page_without_delete_form(admin_client):
    user, release, *_ = _full_client()
    release.status = "pending"
    db.session.commit()
    body = admin_client.get(f"/admin/users/{user.id}").get_data(as_text=True)
    assert "いまは、削除できません" in body and 'name="confirm_email"' not in body
    release.status = "approved"
    db.session.commit()
    assert 'name="confirm_email"' in admin_client.get(f"/admin/users/{user.id}").get_data(as_text=True)


def test_admin_accounts_cannot_be_deleted(app, admin_client, admin_user):
    other_admin = make_user(email="admin2@example.com", role="admin")
    res = _delete(admin_client, other_admin)
    assert "管理者のアカウントは" in res.get_data(as_text=True)
    assert db.session.get(User, other_admin.id) is not None


def test_cannot_delete_yourself(admin_client, admin_user):
    res = _delete(admin_client, admin_user)
    assert "自分自身" in res.get_data(as_text=True)
    assert db.session.get(User, admin_user.id) is not None


# ---- 失敗したとき ----
def test_files_are_kept_if_database_deletion_fails(admin_client, monkeypatch):
    user, release, article, video = _full_client()
    logo = resolve_path(user.logo_path)
    user_id = user.id
    real_commit = db.session.commit
    state = {"armed": True}

    def failing_commit():
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("db down")
        return real_commit()

    monkeypatch.setattr(db.session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        from app import accounts

        accounts.delete_account(db.session.get(User, user_id), db.session.get(User, 1), "leaving@example.com")
    db.session.rollback()
    assert logo.exists() and db.session.get(User, user_id) is not None  # ファイルは、DB の削除が済んでから消す


def test_log_has_ids_but_no_personal_information(admin_client, caplog):
    user, *_ = _full_client()
    with caplog.at_level(logging.INFO):
        _delete(admin_client, user)
    text = caplog.text
    assert "退会処理をしました" in text
    for secret in ("leaving@example.com", "株式会社テスト", "山田太郎"):
        assert secret not in text
