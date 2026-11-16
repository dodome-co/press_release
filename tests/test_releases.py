import io
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Release, ReleasePhoto, User, utcnow
from app.releases import service

from .conftest import login, make_user, png_bytes

FULL = {
    "title": "新サービス「テスト」開始",
    "event_at_text": "2026年12月1日（火）14:00〜16:00",
    "location": "東京都千代田区1-1-1 本社",
    "service_description": "テスト用のサービスです。",
    "feature_1": "速い",
    "feature_2": "",
    "feature_3": "",
    "application_method": "お問い合わせ先までメールでお申し込みください。",
    "application_deadline": "11月28日（金）17:00まで",
    "source_what": "新サービスの体験会を取材してほしい",
    "source_why": "中小企業の広報の負担が大きいため",
    "source_message": "誰でも簡単に作れることを伝えたい",
}


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client_user


def _new_release(client):
    res = client.post("/releases/new")
    assert res.status_code == 302
    return Release.query.order_by(Release.id.desc()).first()


def _save(client, release, **overrides):
    data = {**FULL, **overrides}
    return client.post(f"/releases/{release.id}/edit", data=data)


# ---- アクセス制御 ----
def test_releases_require_login(client):
    res = client.get("/releases/")
    assert res.status_code == 302 and "/login" in res.headers["Location"]


def test_admin_cannot_use_client_pages(client, admin_user):
    login(client, "admin@example.com")
    assert client.get("/releases/").status_code == 403
    assert client.post("/releases/new").status_code == 403


def test_cannot_see_or_edit_other_users_release(client, logged_in, app):
    other = make_user(email="other@example.com")
    r = Release(user_id=other.id, title="他人の書類")
    db.session.add(r)
    db.session.commit()
    assert client.get(f"/releases/{r.id}").status_code == 404
    assert client.get(f"/releases/{r.id}/edit").status_code == 404
    assert client.post(f"/releases/{r.id}/edit", data=FULL).status_code == 404
    assert client.post(f"/releases/{r.id}/submit").status_code == 404
    assert client.post(f"/releases/{r.id}/delete").status_code == 404
    assert client.post(f"/releases/{r.id}/photos", data={"photo": (io.BytesIO(png_bytes()), "a.png")},
                       content_type="multipart/form-data").status_code == 404


# ---- 作成・編集 ----
def test_create_draft_and_edit(client, logged_in):
    release = _new_release(client)
    assert release.status == "draft" and release.user_id == logged_in.id
    res = _save(client, release, feature_2="安い")
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.title == FULL["title"]
    assert release.features == ["速い", "安い"]


def test_draft_can_be_saved_incomplete(client, logged_in):
    release = _new_release(client)
    res = client.post(f"/releases/{release.id}/edit", data={"title": "途中まで"})
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.title == "途中まで"


def test_input_too_long_rejected(client, logged_in):
    release = _new_release(client)
    res = _save(client, release, source_what="あ" * 501)
    assert res.status_code == 200
    db.session.refresh(release)
    assert release.source_what == ""  # 保存されない


def test_html_is_escaped_in_preview(client, logged_in):
    release = _new_release(client)
    _save(client, release, title="<script>alert(1)</script>")
    body = client.get(f"/releases/{release.id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ---- スケジュール ----
def test_schedule_saved_in_order_skipping_blank_rows(client, logged_in):
    release = _new_release(client)
    data = {**FULL, "schedule_time_1": "14:00", "schedule_content_1": "説明",
            "schedule_time_3": "15:00", "schedule_content_3": "デモ"}
    client.post(f"/releases/{release.id}/edit", data=data)
    db.session.refresh(release)
    assert [(i.position, i.time_text, i.content) for i in release.schedule_items] == [
        (1, "14:00", "説明"), (2, "15:00", "デモ")]


def test_schedule_requires_both_time_and_content(client, logged_in):
    release = _new_release(client)
    res = client.post(f"/releases/{release.id}/edit", data={**FULL, "schedule_time_1": "14:00"})
    assert res.status_code == 200
    assert "時間と内容の両方" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.schedule_items == []


def test_schedule_can_be_cleared(client, logged_in):
    release = _new_release(client)
    client.post(f"/releases/{release.id}/edit", data={**FULL, "schedule_time_1": "14:00", "schedule_content_1": "説明"})
    client.post(f"/releases/{release.id}/edit", data=FULL)
    db.session.refresh(release)
    assert release.schedule_items == []


# ---- 写真 ----
def _upload(client, release, data=None, name="a.png"):
    return client.post(f"/releases/{release.id}/photos",
                       data={"photo": (io.BytesIO(data or png_bytes()), name)},
                       content_type="multipart/form-data", follow_redirects=True)


def test_upload_photo_and_view_it(client, logged_in):
    release = _new_release(client)
    _upload(client, release)
    db.session.refresh(release)
    assert len(release.photos) == 1 and release.photos[0].position == 1
    res = client.get(f"/files/photos/{release.photos[0].id}")
    assert res.status_code == 200 and res.mimetype == "image/png"


def test_max_two_photos(client, logged_in):
    release = _new_release(client)
    for _ in range(3):
        res = _upload(client, release)
    assert "2枚までです" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert sorted(p.position for p in release.photos) == [1, 2]


def test_invalid_photo_rejected(client, logged_in):
    release = _new_release(client)
    res = _upload(client, release, data=b"not an image", name="evil.png")
    assert "画像として読み込めません" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.photos == []


def test_delete_photo_frees_position_and_file(client, logged_in, app):
    release = _new_release(client)
    _upload(client, release)
    _upload(client, release)
    db.session.refresh(release)
    first = release.photos[0]
    from app.uploads import resolve_path

    path = resolve_path(first.file_path)
    assert path.exists()
    client.post(f"/releases/{release.id}/photos/{first.id}/delete")
    assert not path.exists()
    _upload(client, release)
    db.session.refresh(release)
    assert sorted(p.position for p in release.photos) == [1, 2]


def test_cannot_delete_photo_of_another_release(client, logged_in):
    a = _new_release(client)
    _upload(client, a)
    db.session.refresh(a)
    photo = a.photos[0]
    # free limit は1件なので、2件目は直接作る
    b = Release(user_id=logged_in.id)
    db.session.add(b)
    db.session.commit()
    assert client.post(f"/releases/{b.id}/photos/{photo.id}/delete").status_code == 404


def test_other_user_cannot_fetch_photo_but_admin_can(client, logged_in, app):
    release = _new_release(client)
    _upload(client, release)
    db.session.refresh(release)
    photo_id = release.photos[0].id

    other = app.test_client()
    make_user(email="other@example.com")
    login(other, "other@example.com")
    assert other.get(f"/files/photos/{photo_id}").status_code == 404

    admin = app.test_client()
    make_user(email="admin@example.com", role="admin")
    login(admin, "admin@example.com")
    assert admin.get(f"/files/photos/{photo_id}").status_code == 404  # 下書きは、管理者にも見せない
    release.status = "pending"
    db.session.commit()
    assert admin.get(f"/files/photos/{photo_id}").status_code == 200  # 提出されたら見られる


def test_photo_requires_login(app, client, logged_in):
    release = _new_release(client)
    _upload(client, release)
    db.session.refresh(release)
    anonymous = app.test_client()
    assert anonymous.get(f"/files/photos/{release.photos[0].id}").status_code == 302


# ---- 提出・無料枠 ----
def test_submit_requires_all_fields(client, logged_in):
    release = _new_release(client)
    client.post(f"/releases/{release.id}/edit", data={"title": "だけ"})
    res = client.post(f"/releases/{release.id}/submit", follow_redirects=True)
    assert "入力してください" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.status == "draft" and release.first_submitted_at is None


def test_submit_success(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    res = client.post(f"/releases/{release.id}/submit")
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.status == "pending"
    assert release.first_submitted_at is not None and release.submitted_at is not None


def test_pending_release_cannot_be_edited_or_deleted_by_client(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    client.post(f"/releases/{release.id}/submit")
    res = _save(client, release, title="書き換え")
    assert res.status_code == 302 and "/releases/%d" % release.id in res.headers["Location"]
    db.session.refresh(release)
    assert release.title == FULL["title"]
    client.post(f"/releases/{release.id}/delete")
    assert db.session.get(Release, release.id) is not None
    res = _upload(client, release)
    assert len(db.session.get(Release, release.id).photos) == 0


def test_cannot_create_when_limit_reached(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    client.post(f"/releases/{release.id}/submit")
    res = client.post("/releases/new", follow_redirects=True)
    assert "枠を使い切りました" in res.get_data(as_text=True)
    assert Release.query.count() == 1


def test_cannot_bypass_limit_by_creating_multiple_drafts(client, logged_in):
    first = _new_release(client)
    second = _new_release(client)  # 提出前なら、下書きは作れる
    for r in (first, second):
        _save(client, r)
    assert client.post(f"/releases/{first.id}/submit").status_code == 302
    db.session.refresh(first)
    assert first.status == "pending"
    res = client.post(f"/releases/{second.id}/submit", follow_redirects=True)
    assert "枠を使い切りました" in res.get_data(as_text=True)
    db.session.refresh(second)
    assert second.status == "draft" and second.first_submitted_at is None


def test_resubmission_after_rejection_is_allowed_and_keeps_first_date(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    client.post(f"/releases/{release.id}/submit")
    db.session.refresh(release)
    first = release.first_submitted_at
    release.status = "rejected"
    release.reject_reason = "写真を追加してください"
    release.first_submitted_at = first - timedelta(days=1)
    first = release.first_submitted_at
    db.session.commit()
    _save(client, release, title="直しました")
    assert client.post(f"/releases/{release.id}/submit").status_code == 302
    db.session.refresh(release)
    assert release.status == "pending"
    assert release.first_submitted_at == first  # 初回の日時は変わらない
    assert service.count_first_submitted(logged_in.id) == 1  # 数は増えない


def test_rejected_release_can_be_edited_with_reason_shown(client, logged_in):
    release = _new_release(client)
    release.status = "rejected"
    release.reject_reason = "理由のテキスト"
    release.first_submitted_at = utcnow()
    db.session.commit()
    assert "理由のテキスト" in client.get(f"/releases/{release.id}/edit").get_data(as_text=True)


def test_delete_draft_removes_files(client, logged_in):
    release = _new_release(client)
    _upload(client, release)
    db.session.refresh(release)
    from app.uploads import resolve_path

    path = resolve_path(release.photos[0].file_path)
    client.post(f"/releases/{release.id}/delete")
    assert db.session.get(Release, release.id) is None
    assert ReleasePhoto.query.count() == 0
    assert not path.exists()


def test_deleted_draft_does_not_use_free_limit(client, logged_in):
    release = _new_release(client)
    client.post(f"/releases/{release.id}/delete")
    assert service.can_create(logged_in)


def test_view_shows_target_days_when_pending(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    client.post(f"/releases/{release.id}/submit")
    body = client.get(f"/releases/{release.id}").get_data(as_text=True)
    assert "2営業日以内" in body
