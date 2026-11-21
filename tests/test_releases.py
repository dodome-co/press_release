import io
from datetime import date, datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import Release, ReleasePhoto, User, utcnow
from app.releases import service

from .conftest import login, make_user, png_bytes

FULL = {
    "title": "新サービス「テスト」開始",
    "event_date": date(2026, 12, 1),
    "event_start_time": time(14, 0),
    "event_end_time": time(16, 0),
    "location": "東京都千代田区1-1-1 本社",
    "service_description": "テスト用のサービスです。",
    "feature_1": "速い",
    "feature_2": "",
    "feature_3": "",
    "application_method": "お問い合わせ先までメールでお申し込みください。",
    "application_deadline_at": datetime(2026, 11, 28, 17, 0),
    "source_what": "新サービスの体験会を取材してほしい",
    "source_why": "中小企業の広報の負担が大きいため",
    "source_message": "誰でも簡単に作れることを伝えたい",
}


def form_data(values: dict) -> dict:
    """モデル用の値（日付型など）を、ブラウザが送る文字列にする。"""
    out = {}
    for k, v in values.items():
        if isinstance(v, datetime):
            out[k] = v.strftime("%Y-%m-%dT%H:%M")
        elif isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, time):
            out[k] = v.strftime("%H:%M")
        else:
            out[k] = v
    return out


@pytest.fixture()
def logged_in(client, client_user):
    login(client, "client@example.com")
    return client_user


def _new_release(client):
    res = client.post("/releases/new")
    assert res.status_code == 302
    return Release.query.order_by(Release.id.desc()).first()


# フォームに入力する項目（日付・時刻・場所）。それ以外は、AI とのチャットで入る（テストでは、直接入れる）
FORM_FIELDS = ("event_date", "event_start_time", "event_end_time", "location")


def _save(client, release, **overrides):
    """フォームで日付・時刻・場所を保存し、チャットで入る内容（特徴・要旨の元になる情報など）を直接入れる。"""
    values = {**FULL, **overrides}
    res = client.post(f"/releases/{release.id}/edit", data=form_data({k: values[k] for k in FORM_FIELDS if k in values}))
    for name, value in values.items():
        if name not in FORM_FIELDS:
            setattr(release, name, value)
    db.session.commit()
    return res


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
    assert client.post(f"/releases/{r.id}/edit", data=form_data(FULL)).status_code == 404
    assert client.post(f"/releases/{r.id}/submit").status_code == 404
    assert client.post(f"/releases/{r.id}/delete").status_code == 404
    assert client.post(f"/releases/{r.id}/photos", data={"photo": (io.BytesIO(png_bytes()), "a.png")},
                       content_type="multipart/form-data").status_code == 404


# ---- 作成・編集 ----
def test_create_draft_and_edit(client, logged_in):
    release = _new_release(client)
    assert release.status == "draft" and release.user_id == logged_in.id
    res = client.post(f"/releases/{release.id}/edit", data=form_data({k: FULL[k] for k in FORM_FIELDS}))
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.location == FULL["location"] and release.event_date == FULL["event_date"]
    assert release.event_start_time == FULL["event_start_time"]


def test_draft_can_be_saved_incomplete(client, logged_in):
    release = _new_release(client)
    res = client.post(f"/releases/{release.id}/edit", data={"location": "途中まで"})
    assert res.status_code == 302
    db.session.refresh(release)
    assert release.location == "途中まで"


def test_form_only_has_date_time_and_place(client, logged_in, app):
    """日時と場所だけがフォーム。ほかの内容は、AI とのチャットで集める。"""
    release = _new_release(client)
    body = client.get(f"/releases/{release.id}/edit").get_data(as_text=True)
    for name in ("event_date", "event_start_time", "event_end_time", "location"):
        assert f'name="{name}"' in body, name
    for gone in ("title", "service_description", "feature_1", "application_method", "application_deadline_at",
                 "source_what", "source_why", "source_message", "schedule_time_1"):
        assert f'name="{gone}"' not in body, gone
    for gone in ("取材のお申し込み", "当日のスケジュール"):
        assert gone not in body, gone
    assert "プレビューへ（提出画面に続きます）" in body


def test_fields_removed_from_the_form_are_ignored(client, logged_in):
    """古い画面から送られても、チャットで集める内容や、なくした項目は、書き換えない。"""
    release = _new_release(client)
    client.post(f"/releases/{release.id}/edit", data={
        "location": "会場", "title": "勝手なタイトル", "source_what": "勝手に書き換え", "feature_1": "勝手な特徴",
        "application_method": "申し込み", "schedule_time_1": "14:00", "schedule_content_1": "説明"})
    db.session.refresh(release)
    assert (release.title, release.source_what, release.feature_1, release.application_method) == ("", "", "", "")
    assert release.schedule_items == []


def test_location_too_long_rejected(client, logged_in):
    release = _new_release(client)
    res = client.post(f"/releases/{release.id}/edit", data={"location": "あ" * 301})
    assert res.status_code == 200
    db.session.refresh(release)
    assert release.location == ""  # 保存されない


def test_html_is_escaped_in_preview(client, logged_in):
    release = _new_release(client)
    _save(client, release, title="<script>alert(1)</script>", subtitle="<b>x</b>", speakers="<i>社長</i>",
          location="<img src=x>")
    body = client.get(f"/releases/{release.id}").get_data(as_text=True)
    for raw in ("<script>alert(1)</script>", "<b>x</b>", "<i>社長</i>", "<img src=x>"):
        assert raw not in body, raw
    assert "&lt;script&gt;" in body


def test_preview_shows_what_ai_made_and_hides_removed_parts(client, logged_in):
    release = _new_release(client)
    _save(client, release, title="新サービス", subtitle="地域の農家と連携", summary="AI がまとめた要旨です。",
          speakers="社長　岩手　盛子\n生産者　花巻　賢治")
    body = client.get(f"/releases/{release.id}").get_data(as_text=True)
    for expected in ("新サービス", "～地域の農家と連携～", "AI がまとめた要旨です。", "当日の対応者", "社長\u3000岩手\u3000盛子".replace("\u3000", "　"),
                     "生産者　花巻　賢治", "特徴"):
        assert expected in body, expected
    for gone in ("取材のお申し込み", "当日のスケジュール", "サービスの内容"):
        assert gone not in body, gone


def test_preview_without_title_says_admin_decides(client, logged_in):
    release = _new_release(client)
    body = client.get(f"/releases/{release.id}").get_data(as_text=True)
    assert "タイトルは、管理者が決めます" in body


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
    res = client.post(f"/releases/{release.id}/edit", data={"location": "書き換え"})
    assert res.status_code == 302 and "/releases/%d" % release.id in res.headers["Location"]
    db.session.refresh(release)
    assert release.location == FULL["location"]
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


# ---- 日付・時刻の型 ----
def test_date_columns_use_date_types(app):
    cols = Release.__table__.c
    from sqlalchemy import Date, DateTime, Time

    assert isinstance(cols.event_date.type, Date) and not isinstance(cols.event_date.type, DateTime)
    assert isinstance(cols.event_start_time.type, Time) and isinstance(cols.event_end_time.type, Time)
    assert isinstance(cols.application_deadline_at.type, DateTime)
    assert "event_at_text" not in cols and "application_deadline" not in cols


def test_dates_are_saved_as_real_dates_and_shown_in_japanese(client, logged_in):
    release = _new_release(client)
    _save(client, release)
    db.session.refresh(release)
    assert release.event_date == date(2026, 12, 1) and release.event_start_time == time(14, 0)
    assert release.application_deadline_at == datetime(2026, 11, 28, 17, 0)
    assert release.event_at_display == "2026年12月1日（火） 14:00〜16:00"
    assert release.application_deadline_display == "2026年11月28日（土） 17:00まで"
    assert "2026年12月1日（火） 14:00〜16:00" in client.get(f"/releases/{release.id}").get_data(as_text=True)


def test_end_time_is_optional(client, logged_in):
    release = _new_release(client)
    _save(client, release, event_end_time="")
    db.session.refresh(release)
    assert release.event_end_time is None and release.event_at_display == "2026年12月1日（火） 14:00〜"


@pytest.mark.parametrize("overrides, message", [
    ({"event_date": "令和8年12月1日"}, "日付"),
    ({"event_date": "2026-02-30"}, "日付"),
    ({"event_start_time": "25:00"}, "時刻"),
    ({"event_end_time": "13:00"}, "開始時刻より後"),
    ({"event_start_time": "", "event_end_time": "16:00"}, "開始時刻も入力"),
    ({"event_date": ""}, "日付も入力"),
])
def test_invalid_dates_are_rejected_and_not_saved(client, logged_in, overrides, message):
    release = _new_release(client)
    res = _save(client, release, **overrides)
    assert res.status_code == 200 and message in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.event_date is None


def test_date_fields_are_required_for_submit(client, logged_in):
    release = _new_release(client)
    _save(client, release, event_date="", event_start_time="", event_end_time="")
    db.session.refresh(release)
    missing = service.missing_required(release)
    assert "取材会の日付" in missing and "開始時刻" in missing
    assert not any("申込" in m for m in missing)  # 取材の申し込みは、聞かない


# ---- 提出と承認に必要な項目（定義：タイトルは管理者が決める。取材の申し込みは、なくす） ----
def test_submit_needs_chat_results_but_not_title_or_application(client, logged_in):
    release = _new_release(client)
    _save(client, release, title="", application_method="", application_deadline_at=None)
    db.session.refresh(release)
    assert service.missing_required(release) == []
    assert client.post(f"/releases/{release.id}/submit").status_code == 302
    db.session.refresh(release)
    assert release.status == "pending"


def test_submit_is_refused_until_the_chat_has_finished(client, logged_in):
    release = _new_release(client)
    _save(client, release, feature_1="", source_what="", source_why="", source_message="")
    res = client.post(f"/releases/{release.id}/submit", follow_redirects=True)
    assert "AI とのチャットで入ります" in res.get_data(as_text=True)
    db.session.refresh(release)
    assert release.status == "draft"


def test_approval_needs_title_and_summary(client, logged_in, admin_user, app):
    release = _new_release(client)
    _save(client, release, title="", summary="要旨")
    client.post(f"/releases/{release.id}/submit")
    db.session.refresh(release)
    assert service.missing_for_approval(release) == ["タイトル"]
    with pytest.raises(service.ReleaseError, match="タイトル"):
        service.approve(release, admin_user)


# ---- マイページ：承認されたプレスリリースを表示し、ダウンロードできる ----
def test_mypage_lists_approved_releases_with_download(client, logged_in, admin_user, app):
    release = _new_release(client)
    _save(client, release, summary="要旨")
    client.post(f"/releases/{release.id}/submit")
    assert "承認されたプレスリリースは、まだありません" in client.get("/mypage/").get_data(as_text=True)  # 承認前
    admin = app.test_client()
    login(admin, "admin@example.com")
    admin.post(f"/admin/releases/{release.id}/approve")
    body = client.get("/mypage/").get_data(as_text=True)
    assert FULL["title"] in body and f"/releases/{release.id}/download" in body
    assert client.get(f"/releases/{release.id}/download").status_code == 200


def test_mypage_does_not_show_other_users_or_unapproved_releases(client, logged_in):
    other = make_user(email="other@example.com")
    db.session.add(Release(user_id=other.id, title="他人の書類", status="approved", summary="x"))
    mine = Release(user_id=logged_in.id, title="自分の承認待ち", status="pending")
    db.session.add(mine)
    db.session.commit()
    body = client.get("/mypage/").get_data(as_text=True)
    assert "他人の書類" not in body and "自分の承認待ち" not in body
